"""Fence retained legacy mixed history for either work purpose."""

from importlib import import_module

from django.db import migrations


def install(apps, schema_editor):
    if schema_editor.connection.vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    def table(name):
        return q(apps.get_model('classification_disagreements', name)._meta.db_table)

    binding = table('BlindReadBinding')
    shared = table('BlindMixSharedEvidence')
    decision = table('BlindMixDecision')
    legacy = table('BlindMixEvidence')
    lifecycle = q('classification_blind_mix_live_lifecycle')
    view = q('classification_blind_bound_lifecycle')
    schema_editor.execute(f'DROP VIEW {view}')
    schema_editor.execute(f'''CREATE VIEW {view} AS
      SELECT b.id AS binding_id,
        CASE WHEN b.decision_id_frozen IS NULL THEN
          NOT EXISTS (SELECT 1 FROM {shared} h WHERE h.chosen_id_frozen = b.assignment_id_frozen)
          AND NOT EXISTS (SELECT 1 FROM {decision} d WHERE d.chosen_id_frozen = b.assignment_id_frozen)
          AND NOT EXISTS (SELECT 1 FROM {legacy} e WHERE e.chosen_id_frozen = b.assignment_id_frozen)
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
        import_module('classification_disagreements.migrations.0071_blind_bound_lifecycle').install(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0071_blind_bound_lifecycle')]
    operations = [migrations.RunPython(install, uninstall)]
