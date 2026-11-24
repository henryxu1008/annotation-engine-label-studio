"""A live, read-only lifecycle view for both mixed outcomes."""

from django.db import migrations, models


def install(apps, schema_editor):
    if schema_editor.connection.vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    def table(app, name):
        return q(apps.get_model(app, name)._meta.db_table)

    view = q('classification_blind_mix_live_lifecycle')
    decision = table('classification_disagreements', 'BlindMixDecision')
    shared = table('classification_disagreements', 'BlindMixSharedEvidence')
    legacy = table('classification_disagreements', 'BlindMixEvidence')
    completion = table('classification_disagreements', 'BlindMixCompletion')
    completion_state = table('classification_disagreements', 'BlindCompletionReadState')
    assignment = table('tasks', 'TaskAssignment')
    task = table('tasks', 'Task')
    submission = table('tasks', 'Submission')
    active = f'''EXISTS (SELECT 1 FROM {assignment} a JOIN {task} t ON t.id = a.task_id
      WHERE a.id = d.source_assignment_id AND a.id = d.source_id_frozen
        AND a.project_id = d.project_id AND a.assignee_id = d.source_assignee_id_frozen
        AND a.task_id = d.source_task_id_frozen AND t.project_id = d.project_id
        AND a.status IN ('assigned', 'in_progress'))'''
    retained = f'''EXISTS (SELECT 1 FROM {legacy} e WHERE e.decision_id_frozen = d.id
      AND e.project_id_frozen = d.project_id AND e.source_id_frozen = d.source_id_frozen
      AND e.source_task_id_frozen = d.source_task_id_frozen
      AND e.source_assignee_id_frozen = d.source_assignee_id_frozen
      AND e.chosen_id_frozen = d.chosen_id_frozen)'''
    completed = f'''EXISTS (SELECT 1 FROM {completion} c WHERE c.decision_id_frozen = d.id
      AND c.project_id_frozen = d.project_id AND c.source_id_frozen = d.source_id_frozen
      AND c.chosen_id_frozen = d.chosen_id_frozen
      AND EXISTS (SELECT 1 FROM {assignment} a JOIN {task} t ON t.id = a.task_id
        WHERE a.id = c.source_id_frozen AND a.project_id = c.project_id_frozen
          AND a.assignee_id = d.source_assignee_id_frozen AND a.status = 'cancelled'
          AND a.version = c.source_version_after AND a.task_id = d.source_task_id_frozen
          AND t.project_id = c.project_id_frozen)
      AND EXISTS (SELECT 1 FROM {submission} s WHERE s.id = c.submission_id_frozen
        AND s.assignment_id = c.chosen_id_frozen AND s.submitted_by_id = d.source_assignee_id_frozen)
      AND EXISTS (SELECT 1 FROM {completion_state} r WHERE r.submission_id = c.submission_id_frozen
        AND r.revoked = FALSE))'''
    schema_editor.execute(f'''CREATE VIEW {view} AS
      SELECT d.id AS decision_id, d.project_id AS project_id_frozen, d.source_id_frozen,
        d.source_task_id_frozen, d.source_assignee_id_frozen, d.chosen_id_frozen,
        d.reference_id_frozen, d.request_hash, d.selected FROM {decision} d
      JOIN {shared} h ON h.decision_id_frozen = d.id AND h.project_id_frozen = d.project_id
        AND h.source_id_frozen = d.source_id_frozen AND h.source_task_id_frozen = d.source_task_id_frozen
        AND h.source_assignee_id_frozen = d.source_assignee_id_frozen
        AND h.chosen_id_frozen = d.chosen_id_frozen AND h.selected = d.selected
      WHERE d.source_assignment_id = d.source_id_frozen AND d.chosen_assignment_id = d.chosen_id_frozen
        AND ((d.selected = FALSE AND d.reference_id_frozen IS NULL
              AND d.source_id_frozen = d.chosen_id_frozen AND {active})
          OR (d.selected = TRUE AND {retained} AND ({active} OR {completed})))''')


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor in ('postgresql', 'sqlite'):
        schema_editor.execute('DROP VIEW ' + schema_editor.quote_name('classification_blind_mix_live_lifecycle'))


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0064_shared_mix_evidence')]
    operations = [
        migrations.CreateModel(name='BlindMixLiveLifecycle', fields=[
            ('decision_id', models.PositiveBigIntegerField(primary_key=True, serialize=False)),
            ('project_id_frozen', models.PositiveBigIntegerField()),
            ('source_id_frozen', models.PositiveBigIntegerField()),
            ('source_task_id_frozen', models.PositiveBigIntegerField()),
            ('source_assignee_id_frozen', models.PositiveBigIntegerField()),
            ('chosen_id_frozen', models.PositiveBigIntegerField()),
            ('reference_id_frozen', models.PositiveBigIntegerField(null=True)),
            ('request_hash', models.CharField(max_length=64)),
            ('selected', models.BooleanField()),
        ], options={'managed': False, 'db_table': 'classification_blind_mix_live_lifecycle'}),
        migrations.RunPython(install, uninstall),
    ]
