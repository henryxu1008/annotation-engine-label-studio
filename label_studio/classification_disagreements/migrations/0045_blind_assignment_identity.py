from django.db import migrations


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    assignment = q(apps.get_model('tasks', 'TaskAssignment')._meta.db_table)
    handle = q(apps.get_model('classification_disagreements', 'BlindTaskHandle')._meta.db_table)
    if vendor == 'postgresql':
        schema_editor.execute(f'''CREATE FUNCTION blind_assignment_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF EXISTS (SELECT 1 FROM {handle} WHERE assignment_id_frozen = NEW.id) THEN
                RAISE EXCEPTION 'handled assignment identity cannot be reused';
              END IF;
              RETURN NEW;
            END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_assignment_identity_guard AFTER INSERT ON {assignment}
            FOR EACH ROW EXECUTE FUNCTION blind_assignment_identity_guard()''')
    else:
        schema_editor.execute(f'''CREATE TRIGGER blind_assignment_identity_guard AFTER INSERT ON {assignment}
            WHEN EXISTS (SELECT 1 FROM {handle} WHERE assignment_id_frozen = NEW.id)
            BEGIN SELECT RAISE(ABORT, 'handled assignment identity cannot be reused'); END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'postgresql':
        assignment = schema_editor.quote_name(apps.get_model('tasks', 'TaskAssignment')._meta.db_table)
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_assignment_identity_guard ON {assignment}')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_assignment_identity_guard()')
    elif vendor == 'sqlite':
        schema_editor.execute('DROP TRIGGER IF EXISTS blind_assignment_identity_guard')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0044_blind_evidence_identity')]
    operations = [migrations.RunPython(install, uninstall)]
