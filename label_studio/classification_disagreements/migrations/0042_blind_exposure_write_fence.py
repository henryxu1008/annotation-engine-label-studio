from django.db import migrations


def install(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    q = schema_editor.quote_name
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    assignment = q(apps.get_model('tasks', 'TaskAssignment')._meta.db_table)
    task = q(apps.get_model('tasks', 'Task')._meta.db_table)
    # Assignment and task-data triggers record exposure after the write. A
    # shared capture lock there does not conflict with participant readers.
    # Reserve the epoch before recording so a completed exposure cannot race
    # an in-flight read that has already checked its eligibility.
    schema_editor.execute(f'''CREATE FUNCTION blind_exposure_write_fence() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          PERFORM id FROM {epoch} WHERE id = 1 AND enabled = TRUE FOR UPDATE;
          RETURN NEW;
        END $$''')
    schema_editor.execute(f'''CREATE TRIGGER blind_assignment_write_fence
        BEFORE INSERT OR UPDATE OF assignee_id, task_id ON {assignment}
        FOR EACH ROW EXECUTE FUNCTION blind_exposure_write_fence()''')
    schema_editor.execute(f'''CREATE TRIGGER blind_task_data_write_fence
        BEFORE UPDATE OF data ON {task}
        FOR EACH ROW EXECUTE FUNCTION blind_exposure_write_fence()''')


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    q = schema_editor.quote_name
    for trigger, model, app in (
        ('blind_assignment_write_fence', 'TaskAssignment', 'tasks'),
        ('blind_task_data_write_fence', 'Task', 'tasks'),
    ):
        table = q(apps.get_model(app, model)._meta.db_table)
        schema_editor.execute(f'DROP TRIGGER IF EXISTS {trigger} ON {table}')
    schema_editor.execute('DROP FUNCTION IF EXISTS blind_exposure_write_fence()')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0041_blind_evidence_revocation_fence')]
    operations = [migrations.RunPython(install, uninstall)]
