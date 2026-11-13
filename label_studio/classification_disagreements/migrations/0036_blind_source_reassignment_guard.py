from django.db import migrations


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    assignment = q(apps.get_model('tasks', 'TaskAssignment')._meta.db_table)
    decision = q(apps.get_model('classification_disagreements', 'BlindMixEvidence')._meta.db_table)
    project = q(apps.get_model('projects', 'Project')._meta.db_table)
    disallowed = f'''EXISTS (SELECT 1 FROM {decision} d WHERE (
        (d.source_id_frozen = NEW.id AND (d.source_task_id_frozen <> NEW.task_id
         OR d.source_assignee_id_frozen <> NEW.assignee_id OR d.project_id_frozen <> NEW.project_id))
        OR (NEW.status IN ('assigned', 'in_progress') AND d.source_task_id_frozen = NEW.task_id
            AND d.source_assignee_id_frozen = NEW.assignee_id AND d.source_id_frozen <> NEW.id)))'''
    reactivated = f'''OLD.status = 'cancelled' AND NEW.status IN ('assigned', 'in_progress') AND EXISTS (
        SELECT 1 FROM {decision} d WHERE d.source_id_frozen = OLD.id)'''
    if vendor == 'postgresql':
        schema_editor.execute(f'''CREATE FUNCTION blind_source_reassignment_guard() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN PERFORM id FROM {project} WHERE id = NEW.project_id FOR UPDATE;
          IF TG_OP = 'UPDATE' THEN IF {reactivated} THEN
          RAISE EXCEPTION 'blind source assignment cannot be reactivated'; END IF; END IF;
          IF {disallowed} THEN RAISE EXCEPTION 'blind source cannot be reassigned to its original actor';
          END IF; RETURN NEW; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_source_reassignment_guard BEFORE INSERT OR UPDATE ON {assignment}
          FOR EACH ROW EXECUTE FUNCTION blind_source_reassignment_guard()''')
    else:
        for suffix, operation in (('insert', 'INSERT'), ('update', 'UPDATE')):
            condition = disallowed if suffix == 'insert' else f'({disallowed}) OR ({reactivated})'
            schema_editor.execute(f'''CREATE TRIGGER blind_source_reassignment_{suffix} BEFORE {operation} ON {assignment}
              WHEN {condition} BEGIN SELECT RAISE(ABORT, 'blind source cannot be reassigned to its original actor'); END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'postgresql':
        assignment = schema_editor.quote_name(apps.get_model('tasks', 'TaskAssignment')._meta.db_table)
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_source_reassignment_guard ON {assignment}')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_source_reassignment_guard()')
    elif vendor == 'sqlite':
        for suffix in ('insert', 'update'):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_source_reassignment_{suffix}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0035_blind_mix_completion')]
    operations = [migrations.RunPython(install, uninstall)]
