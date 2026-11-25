"""Project both purposes' live exposure evidence in one bounded read."""

from django.db import migrations, models


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name

    def table(name):
        return q(apps.get_model('classification_disagreements', name)._meta.db_table)

    view = q('classification_blind_work_exposure_state')
    binding = table('BlindReadBinding')
    epoch = table('BlindCaptureEpoch')
    account = table('BlindAccountExposureState')
    marker = table('BlindQualityAssignment')
    proof = table('BlindEligibilityProof')
    source = table('BlindAssignmentExposure')
    text = table('BlindTextLengthExposure')
    lengths = table('BlindReferenceTextLength')
    if vendor == 'postgresql':
        source_key = '''CASE WHEN octet_length(CAST(p.manifest AS text)) <= 16384
          AND jsonb_typeof(p.manifest->'source_task_id') = 'number' THEN
            CASE WHEN (p.manifest->>'source_task_id') ~ '^[1-9][0-9]{0,18}$' THEN
              CASE WHEN CAST(p.manifest->>'source_task_id' AS numeric) <= 9223372036854775807
                THEN p.manifest->>'source_task_id' ELSE NULL END
            ELSE NULL END ELSE NULL END'''
    else:
        source_key = '''CASE WHEN length(CAST(p.manifest AS BLOB)) <= 16384
          AND json_type(p.manifest, '$.source_task_id') = 'integer'
          AND json_extract(p.manifest, '$.source_task_id') > 0
          THEN CAST(json_extract(p.manifest, '$.source_task_id') AS TEXT) ELSE NULL END'''
    schema_editor.execute(f'''CREATE VIEW {view} AS
      SELECT x.assignment_id, x.binding_id, x.binding_hash, x.actor_id, x.generation, x.epoch_enabled,
        x.exposure_clean, x.marker_present, x.matching_marker_id, x.proof_present, x.proof_matches,
        x.source_task_key,
        CASE WHEN x.source_task_key IS NULL THEN FALSE ELSE EXISTS (
          SELECT 1 FROM {source} s WHERE s.account_id = x.actor_id AND s.generation = x.generation
            AND s.task_id = CAST(x.source_task_key AS bigint)) END AS source_exposed,
        EXISTS (SELECT 1 FROM {text} t WHERE t.account_id = x.actor_id AND t.generation = x.generation
          AND t.assignment_id <> x.assignment_id AND (t.byte_length = -1 OR EXISTS (
            SELECT 1 FROM {lengths} l WHERE l.reference_id = x.reference_id
              AND l.byte_length = t.byte_length))) AS text_exposed
      FROM (SELECT b.assignment_id_frozen AS assignment_id, b.id AS binding_id, b.binding_hash,
        b.actor_id_frozen AS actor_id, b.reference_id_frozen AS reference_id,
        e.generation, e.enabled AS epoch_enabled,
        EXISTS (SELECT 1 FROM {account} a WHERE a.account_id = b.actor_id_frozen
          AND a.generation = e.generation AND a.file_exposed = FALSE AND a.label_exposed = FALSE) AS exposure_clean,
        EXISTS (SELECT 1 FROM {marker} m WHERE m.task_id = b.task_id_frozen) AS marker_present,
        (SELECT m.id FROM {marker} m WHERE m.assignment_id = b.assignment_id_frozen
          AND m.task_id = b.task_id_frozen AND m.project_id = b.project_id_frozen
          AND m.assignee_id = b.actor_id_frozen AND m.reference_id = b.reference_id_frozen
          AND m.reference_hash = b.reference_hash LIMIT 1) AS matching_marker_id,
        EXISTS (SELECT 1 FROM {proof} a WHERE a.assignment_id = b.assignment_id_frozen) AS proof_present,
        CASE WHEN p.id IS NULL THEN FALSE ELSE TRUE END AS proof_matches,
        {source_key} AS source_task_key
      FROM {binding} b JOIN {epoch} e ON e.id = 1
      LEFT JOIN {proof} p ON p.assignment_id = b.assignment_id_frozen AND p.manifest_hash = b.proof_hash) x''')


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor in ('postgresql', 'sqlite'):
        schema_editor.execute('DROP VIEW ' + schema_editor.quote_name('classification_blind_work_exposure_state'))


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0065_blind_mix_live_lifecycle')]
    operations = [
        migrations.CreateModel(name='BlindWorkExposureState', fields=[
            ('assignment_id', models.PositiveBigIntegerField(primary_key=True, serialize=False)),
            ('binding_id', models.PositiveBigIntegerField()),
            ('binding_hash', models.CharField(max_length=64)),
            ('actor_id', models.PositiveBigIntegerField()),
            ('generation', models.PositiveBigIntegerField()),
            ('epoch_enabled', models.BooleanField()),
            ('exposure_clean', models.BooleanField()),
            ('marker_present', models.BooleanField()),
            ('matching_marker_id', models.PositiveBigIntegerField(null=True)),
            ('proof_present', models.BooleanField()),
            ('proof_matches', models.BooleanField()),
            ('source_task_key', models.CharField(max_length=128, null=True)),
            ('source_exposed', models.BooleanField()),
            ('text_exposed', models.BooleanField()),
        ], options={'managed': False, 'db_table': 'classification_blind_work_exposure_state'}),
        migrations.RunPython(install, uninstall),
    ]
