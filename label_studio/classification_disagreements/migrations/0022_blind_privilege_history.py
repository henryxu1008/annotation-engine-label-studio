from django.conf import settings
from django.db import migrations, models


HISTORY = 'classification_disagreements_blindprivilegehistory'


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    history = q(HISTORY)
    user = q(apps.get_model(settings.AUTH_USER_MODEL)._meta.db_table)
    project = q(apps.get_model('projects', 'Project')._meta.db_table)
    member = q(apps.get_model('projects', 'ProjectMember')._meta.db_table)
    organization = q(apps.get_model('organizations', 'Organization')._meta.db_table)

    if vendor == 'postgresql':
        schema_editor.execute(f'''
          CREATE FUNCTION blind_privilege_account() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF TG_OP = 'INSERT' THEN
              INSERT INTO {history} (account_id, elevated, had_assignment, first_assignment_id)
                VALUES (NEW.id, NEW.is_staff OR NEW.is_superuser, FALSE, NULL);
            ELSIF NEW.is_staff OR NEW.is_superuser THEN
              UPDATE {history} SET elevated = TRUE WHERE account_id = NEW.id;
            END IF;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_privilege_account
          AFTER INSERT OR UPDATE OF is_staff, is_superuser ON {user}
          FOR EACH ROW EXECUTE FUNCTION blind_privilege_account()''')
        schema_editor.execute(f'''
          CREATE FUNCTION blind_privilege_owner() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            UPDATE {history} SET elevated = TRUE WHERE account_id = NEW.created_by_id;
            RETURN NEW;
          END $$''')
        for name, table in (('blind_privilege_project', project), ('blind_privilege_organization', organization)):
            schema_editor.execute(f'''CREATE TRIGGER {name} AFTER INSERT OR UPDATE OF created_by_id ON {table}
              FOR EACH ROW EXECUTE FUNCTION blind_privilege_owner()''')
        schema_editor.execute(f'''
          CREATE FUNCTION blind_privilege_member() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF NEW.role <> 'annotator' THEN
              UPDATE {history} SET elevated = TRUE WHERE account_id = NEW.user_id;
            END IF;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_privilege_member
          AFTER INSERT OR UPDATE OF role, user_id ON {member}
          FOR EACH ROW EXECUTE FUNCTION blind_privilege_member()''')
        schema_editor.execute('''
          CREATE FUNCTION blind_privilege_forward() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF NEW.account_id <> OLD.account_id OR (OLD.elevated AND NOT NEW.elevated)
               OR (OLD.had_assignment AND NOT NEW.had_assignment) THEN
              RAISE EXCEPTION 'blind privilege history cannot be reset';
            END IF;
            IF OLD.first_assignment_id IS NOT NULL
               AND NEW.first_assignment_id IS DISTINCT FROM OLD.first_assignment_id THEN
              RAISE EXCEPTION 'blind privilege history cannot be reset';
            END IF;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_privilege_forward BEFORE UPDATE ON {history}
          FOR EACH ROW EXECUTE FUNCTION blind_privilege_forward()''')
    else:
        schema_editor.execute(f'''CREATE TRIGGER blind_privilege_account_insert AFTER INSERT ON {user}
          BEGIN INSERT INTO {history} (account_id, elevated, had_assignment, first_assignment_id)
            VALUES (NEW.id, NEW.is_staff OR NEW.is_superuser, FALSE, NULL); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_privilege_account_update
          AFTER UPDATE OF is_staff, is_superuser ON {user} WHEN NEW.is_staff OR NEW.is_superuser
          BEGIN UPDATE {history} SET elevated = TRUE WHERE account_id = NEW.id; END''')
        for name, table in (('blind_privilege_project', project), ('blind_privilege_organization', organization)):
            for suffix, op in (('insert', 'INSERT'), ('update', 'UPDATE OF created_by_id')):
                schema_editor.execute(f'''CREATE TRIGGER {name}_{suffix} AFTER {op} ON {table}
                  BEGIN UPDATE {history} SET elevated = TRUE WHERE account_id = NEW.created_by_id; END''')
        for suffix, op in (('insert', 'INSERT'), ('update', 'UPDATE OF role, user_id')):
            schema_editor.execute(f'''CREATE TRIGGER blind_privilege_member_{suffix} AFTER {op} ON {member}
              WHEN NEW.role <> 'annotator'
              BEGIN UPDATE {history} SET elevated = TRUE WHERE account_id = NEW.user_id; END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_privilege_forward BEFORE UPDATE ON {history}
          WHEN NEW.account_id IS NOT OLD.account_id OR (OLD.elevated AND NOT NEW.elevated)
               OR (OLD.had_assignment AND NOT NEW.had_assignment)
               OR (OLD.first_assignment_id IS NOT NULL AND NEW.first_assignment_id IS NOT OLD.first_assignment_id)
          BEGIN SELECT RAISE(ABORT, 'blind privilege history cannot be reset'); END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    if vendor == 'postgresql':
        q = schema_editor.quote_name
        triggers = (
            ('blind_privilege_account', apps.get_model(settings.AUTH_USER_MODEL)._meta.db_table),
            ('blind_privilege_project', apps.get_model('projects', 'Project')._meta.db_table),
            ('blind_privilege_organization', apps.get_model('organizations', 'Organization')._meta.db_table),
            ('blind_privilege_member', apps.get_model('projects', 'ProjectMember')._meta.db_table),
            ('blind_privilege_forward', HISTORY),
        )
        for name, table in triggers:
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name} ON {q(table)}')
        for name in ('blind_privilege_account', 'blind_privilege_owner', 'blind_privilege_member',
                     'blind_privilege_forward'):
            schema_editor.execute(f'DROP FUNCTION IF EXISTS {name}()')
    else:
        for name in ('blind_privilege_account_insert', 'blind_privilege_account_update',
                     'blind_privilege_project_insert', 'blind_privilege_project_update',
                     'blind_privilege_organization_insert', 'blind_privilege_organization_update',
                     'blind_privilege_member_insert', 'blind_privilege_member_update', 'blind_privilege_forward'):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}')


class Migration(migrations.Migration):
    dependencies = [
        ('classification_disagreements', '0021_blind_provenance_order'),
        ('projects', '0035_projectmember_role_and_unique'),
        ('organizations', '0006_alter_organizationmember_deleted_at'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(name='BlindPrivilegeHistory', fields=[
            ('account_id', models.PositiveBigIntegerField(primary_key=True, serialize=False)),
            ('elevated', models.BooleanField(default=False)),
            ('had_assignment', models.BooleanField(default=False)),
            ('first_assignment_id', models.PositiveBigIntegerField(null=True)),
        ], options={'constraints': [
            models.CheckConstraint(condition=(models.Q(had_assignment=False, first_assignment_id__isnull=True)
                | models.Q(had_assignment=True, first_assignment_id__isnull=False)),
                name='blind_first_assignment_consistent'),
        ]}),
        migrations.RunPython(install, uninstall),
    ]
