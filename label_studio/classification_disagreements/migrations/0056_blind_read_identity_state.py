from django.db import migrations, models


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    state = q(apps.get_model('classification_disagreements', 'BlindReadIdentityState')._meta.db_table)
    binding = q(apps.get_model('classification_disagreements', 'BlindReadBinding')._meta.db_table)
    assignment = q(apps.get_model('tasks', 'TaskAssignment')._meta.db_table)
    # Preserve the existing live identity check at the upgrade boundary.
    # Earlier identity changes cannot be reconstructed; the retained state
    # only proves continuity from this migration onward.
    if vendor == 'postgresql':
        # Issuance locks the assignment before inserting its binding.
        schema_editor.execute(f'LOCK TABLE {assignment}, {binding} IN SHARE ROW EXCLUSIVE MODE')
    schema_editor.execute(f'''INSERT INTO {state} (assignment_id, revoked)
      SELECT b.assignment_id_frozen, NOT EXISTS (SELECT 1 FROM {assignment} a
        WHERE a.id = b.assignment_id_frozen AND a.task_id = b.task_id_frozen
          AND a.project_id = b.project_id_frozen AND a.assignee_id = b.actor_id_frozen
          AND a.status IN ('assigned', 'in_progress')) FROM {binding} b''')
    matching = (f'EXISTS (SELECT 1 FROM {assignment} a WHERE a.id = NEW.assignment_id_frozen '
                'AND a.task_id = NEW.task_id_frozen AND a.project_id = NEW.project_id_frozen '
                "AND a.assignee_id = NEW.actor_id_frozen AND a.status IN ('assigned', 'in_progress'))")
    if vendor == 'postgresql':
        schema_editor.execute(f'''CREATE FUNCTION blind_read_identity_state_guard() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'blind read identity state is retained'; END IF;
            IF pg_trigger_depth() = 1 THEN RAISE EXCEPTION 'blind read identity state is trigger managed'; END IF;
            IF TG_OP = 'UPDATE' AND (NEW.assignment_id <> OLD.assignment_id OR (OLD.revoked AND NOT NEW.revoked))
            THEN RAISE EXCEPTION 'blind read identity revocation is irreversible'; END IF;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_read_identity_state_guard BEFORE INSERT OR UPDATE OR DELETE ON {state}
          FOR EACH ROW EXECUTE FUNCTION blind_read_identity_state_guard()''')
        schema_editor.execute(f'''CREATE FUNCTION blind_read_identity_state_create() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN INSERT INTO {state} (assignment_id, revoked) VALUES (NEW.assignment_id_frozen, NOT {matching});
            RETURN NEW; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_read_identity_state_create AFTER INSERT ON {binding}
          FOR EACH ROW EXECUTE FUNCTION blind_read_identity_state_create()''')
        schema_editor.execute(f'''CREATE FUNCTION blind_read_identity_state_revoke() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF TG_OP = 'DELETE' THEN
              UPDATE {state} SET revoked = TRUE WHERE assignment_id = OLD.id AND NOT revoked;
              RETURN OLD;
            END IF;
            IF OLD.id IS DISTINCT FROM NEW.id OR OLD.task_id IS DISTINCT FROM NEW.task_id
                OR OLD.project_id IS DISTINCT FROM NEW.project_id OR OLD.assignee_id IS DISTINCT FROM NEW.assignee_id
                OR NEW.status NOT IN ('assigned', 'in_progress')
            THEN UPDATE {state} SET revoked = TRUE WHERE assignment_id = OLD.id AND NOT revoked; END IF;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_read_identity_state_revoke BEFORE UPDATE OR DELETE ON {assignment}
          FOR EACH ROW EXECUTE FUNCTION blind_read_identity_state_revoke()''')
    else:
        schema_editor.execute(f'''CREATE TRIGGER blind_read_identity_state_insert BEFORE INSERT ON {state}
          WHEN EXISTS (SELECT 1 FROM {state} WHERE assignment_id = NEW.assignment_id)
            OR NOT EXISTS (SELECT 1 FROM {binding} WHERE assignment_id_frozen = NEW.assignment_id)
          BEGIN SELECT RAISE(ABORT, 'invalid blind read identity state'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_read_identity_state_update BEFORE UPDATE ON {state}
          WHEN NEW.assignment_id IS NOT OLD.assignment_id OR (OLD.revoked AND NOT NEW.revoked)
          BEGIN SELECT RAISE(ABORT, 'blind read identity revocation is irreversible'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_read_identity_state_delete BEFORE DELETE ON {state}
          BEGIN SELECT RAISE(ABORT, 'blind read identity state is retained'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_read_identity_state_create AFTER INSERT ON {binding}
          BEGIN INSERT INTO {state} (assignment_id, revoked) VALUES (NEW.assignment_id_frozen, NOT {matching}); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_read_identity_state_revoke_delete BEFORE DELETE ON {assignment}
          BEGIN UPDATE {state} SET revoked = TRUE WHERE assignment_id = OLD.id AND NOT revoked; END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_read_identity_state_revoke_update BEFORE UPDATE OF id, task_id, project_id, assignee_id, status ON {assignment}
          WHEN OLD.id IS NOT NEW.id OR OLD.task_id IS NOT NEW.task_id OR OLD.project_id IS NOT NEW.project_id
            OR OLD.assignee_id IS NOT NEW.assignee_id OR NEW.status NOT IN ('assigned', 'in_progress')
          BEGIN UPDATE {state} SET revoked = TRUE WHERE assignment_id = OLD.id AND NOT revoked; END''')


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    state = q(apps.get_model('classification_disagreements', 'BlindReadIdentityState')._meta.db_table)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f'SELECT 1 FROM {state} LIMIT 1')
        if cursor.fetchone():
            raise RuntimeError('Retained blind read identity state cannot be discarded by rollback.')
    if schema_editor.connection.vendor == 'postgresql':
        for name, model, app in (
            ('blind_read_identity_state_guard', 'BlindReadIdentityState', 'classification_disagreements'),
            ('blind_read_identity_state_create', 'BlindReadBinding', 'classification_disagreements'),
            ('blind_read_identity_state_revoke', 'TaskAssignment', 'tasks'),
        ):
            table = q(apps.get_model(app, model)._meta.db_table)
            schema_editor.execute(f'DROP TRIGGER {name} ON {table}')
            schema_editor.execute(f'DROP FUNCTION {name}()')
    else:
        for name in ('blind_read_identity_state_insert', 'blind_read_identity_state_update',
                     'blind_read_identity_state_delete', 'blind_read_identity_state_create',
                     'blind_read_identity_state_revoke_delete', 'blind_read_identity_state_revoke_update'):
            schema_editor.execute(f'DROP TRIGGER {name}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0055_blind_text_read_index')]
    operations = [
        migrations.CreateModel(name='BlindReadIdentityState', fields=[
            ('assignment_id', models.PositiveBigIntegerField(primary_key=True, serialize=False)),
            ('revoked', models.BooleanField(default=False)),
        ]),
        migrations.RunPython(install, uninstall),
    ]
