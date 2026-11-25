"""Read a bounded live length set for both ordinary and quality work."""

from django.db import migrations, models


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    binding = q(apps.get_model('classification_disagreements', 'BlindReadBinding')._meta.db_table)
    lengths = q(apps.get_model('classification_disagreements', 'BlindReferenceTextLength')._meta.db_table)
    view = q('classification_blind_work_reference_lengths')
    # Limit before aggregating, so corrupt or unbounded history cannot cause an
    # unbounded response. NULL means that callers must use the exact fallback.
    aggregate = ("COALESCE(jsonb_agg(v.byte_length ORDER BY v.byte_length), '[]'::jsonb)"
                 if vendor == 'postgresql' else "COALESCE(json_group_array(v.byte_length), '[]')")
    schema_editor.execute(f'''CREATE VIEW {view} AS
      SELECT b.assignment_id_frozen AS assignment_id,
        CASE WHEN (SELECT count(*) FROM (SELECT l.byte_length FROM {lengths} l
               WHERE l.reference_id = b.reference_id_frozen LIMIT 1025) bounded_count) > 1024
          THEN NULL ELSE (SELECT {aggregate} FROM (SELECT l.byte_length FROM {lengths} l
               WHERE l.reference_id = b.reference_id_frozen ORDER BY l.byte_length LIMIT 1024) v)
        END AS lengths
      FROM {binding} b''')


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor in ('postgresql', 'sqlite'):
        schema_editor.execute('DROP VIEW ' + schema_editor.quote_name('classification_blind_work_reference_lengths'))


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0067_blind_exposure_sqlite_integer')]
    operations = [
        migrations.CreateModel(name='BlindWorkReferenceLengths', fields=[
            ('assignment_id', models.PositiveBigIntegerField(primary_key=True, serialize=False)),
            ('lengths', models.JSONField(null=True)),
        ], options={'managed': False, 'db_table': 'classification_blind_work_reference_lengths'}),
        migrations.RunPython(install, uninstall),
    ]
