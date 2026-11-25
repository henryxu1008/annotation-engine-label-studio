"""Validate mixed and legacy work bindings through one live projection."""

from django.db import migrations, models


def install(apps, schema_editor):
    if schema_editor.connection.vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    binding = q(apps.get_model('classification_disagreements', 'BlindReadBinding')._meta.db_table)
    history = q(apps.get_model('classification_disagreements', 'BlindMixSharedEvidence')._meta.db_table)
    lifecycle = q('classification_blind_mix_live_lifecycle')
    view = q('classification_blind_bound_lifecycle')
    schema_editor.execute(f'''CREATE VIEW {view} AS
      SELECT b.id AS binding_id,
        CASE WHEN b.decision_id_frozen IS NULL THEN
          NOT EXISTS (SELECT 1 FROM {history} h WHERE h.chosen_id_frozen = b.assignment_id_frozen)
        ELSE EXISTS (SELECT 1 FROM {lifecycle} l
          WHERE l.decision_id = b.decision_id_frozen
            AND l.selected = CASE WHEN b.purpose = 'quality' THEN TRUE ELSE FALSE END
            AND l.project_id_frozen = b.project_id_frozen
            AND l.chosen_id_frozen = b.assignment_id_frozen
            AND l.source_id_frozen = b.source_id_frozen
            AND l.source_task_id_frozen = b.source_task_id_frozen
            AND l.source_assignee_id_frozen = b.actor_id_frozen
            AND (l.reference_id_frozen = b.reference_id_frozen
                 OR (l.reference_id_frozen IS NULL AND b.reference_id_frozen IS NULL))
            AND l.request_hash = b.decision_request_hash)
        END AS valid
      FROM {binding} b''')


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor in ('postgresql', 'sqlite'):
        schema_editor.execute('DROP VIEW ' + schema_editor.quote_name('classification_blind_bound_lifecycle'))


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0070_blind_global_scope_state')]
    operations = [
        migrations.CreateModel(name='BlindBoundLifecycle', fields=[
            ('binding_id', models.PositiveBigIntegerField(primary_key=True, serialize=False)),
            ('valid', models.BooleanField()),
        ], options={'managed': False, 'db_table': 'classification_blind_bound_lifecycle'}),
        migrations.RunPython(install, uninstall),
    ]
