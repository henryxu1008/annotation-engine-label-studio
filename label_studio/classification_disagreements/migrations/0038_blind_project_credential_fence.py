from django.db import migrations


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    project = q(apps.get_model('projects', 'Project')._meta.db_table)
    # Accounts certified under a version that disclosed credentials cannot be
    # assumed unexposed. Operators must rotate them and start a fresh epoch.
    schema_editor.execute(f'UPDATE {epoch} SET enabled = FALSE WHERE id = 1')
    if vendor == 'postgresql':
        schema_editor.execute(f'''CREATE FUNCTION blind_project_credential_invalidate() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    IF COALESCE(NEW.task_data_login, '') <> '' OR COALESCE(NEW.task_data_password, '') <> '' THEN
                        UPDATE {epoch} SET enabled = FALSE WHERE id = 1;
                    END IF;
                ELSIF TG_OP = 'DELETE' THEN
                    IF COALESCE(OLD.task_data_login, '') <> '' OR COALESCE(OLD.task_data_password, '') <> '' THEN
                        UPDATE {epoch} SET enabled = FALSE WHERE id = 1;
                    END IF;
                ELSIF NEW.task_data_login IS DISTINCT FROM OLD.task_data_login
                      OR NEW.task_data_password IS DISTINCT FROM OLD.task_data_password THEN
                    UPDATE {epoch} SET enabled = FALSE WHERE id = 1;
                END IF;
                RETURN NULL;
            END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_project_credential_change AFTER INSERT OR UPDATE OR DELETE ON {project}
            FOR EACH ROW EXECUTE FUNCTION blind_project_credential_invalidate()''')
    else:
        schema_editor.execute(f'''CREATE TRIGGER blind_project_credential_insert AFTER INSERT ON {project}
            WHEN COALESCE(NEW.task_data_login, '') <> '' OR COALESCE(NEW.task_data_password, '') <> ''
            BEGIN UPDATE {epoch} SET enabled = FALSE WHERE id = 1; END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_project_credential_delete AFTER DELETE ON {project}
            WHEN COALESCE(OLD.task_data_login, '') <> '' OR COALESCE(OLD.task_data_password, '') <> ''
            BEGIN UPDATE {epoch} SET enabled = FALSE WHERE id = 1; END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_project_credential_update AFTER UPDATE OF task_data_login, task_data_password ON {project}
            WHEN NEW.task_data_login IS NOT OLD.task_data_login OR NEW.task_data_password IS NOT OLD.task_data_password
            BEGIN UPDATE {epoch} SET enabled = FALSE WHERE id = 1; END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor in ('postgresql', 'sqlite'):
        epoch = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
        schema_editor.execute(f'UPDATE {epoch} SET enabled = FALSE WHERE id = 1')
    if vendor == 'postgresql':
        project = schema_editor.quote_name(apps.get_model('projects', 'Project')._meta.db_table)
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_project_credential_change ON {project}')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_project_credential_invalidate()')
    elif vendor == 'sqlite':
        for suffix in ('insert', 'update', 'delete'):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_project_credential_{suffix}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0037_blind_resource_handle')]
    operations = [migrations.RunPython(install, uninstall)]
