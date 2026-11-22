from django.db import migrations, models


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    state = q(apps.get_model('classification_disagreements', 'BlindCompletionReadState')._meta.db_table)
    completion = q(apps.get_model('classification_disagreements', 'BlindMixCompletion')._meta.db_table)
    submission = q(apps.get_model('tasks', 'Submission')._meta.db_table)
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    # Old IDs may already have been deleted and reused. Their history cannot
    # be proven, so migration never grants them a new live completion.
    if vendor == 'postgresql':
        schema_editor.execute(f'LOCK TABLE {submission}, {completion} IN SHARE ROW EXCLUSIVE MODE')
    schema_editor.execute(f'INSERT INTO {state} (submission_id, revoked) SELECT submission_id_frozen, TRUE FROM {completion}')
    if vendor == 'postgresql':
        schema_editor.execute(f'''CREATE FUNCTION blind_completion_state_guard() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'completion state is retained'; END IF;
            IF TG_OP = 'INSERT' THEN
              IF NEW.revoked OR EXISTS (SELECT 1 FROM {state} WHERE submission_id = NEW.submission_id)
                  OR NOT EXISTS (SELECT 1 FROM {completion} WHERE submission_id_frozen = NEW.submission_id)
              THEN RAISE EXCEPTION 'invalid completion state'; END IF;
            ELSIF NEW.submission_id <> OLD.submission_id OR (OLD.revoked AND NOT NEW.revoked)
            THEN RAISE EXCEPTION 'completion revocation is irreversible'; END IF;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_completion_state_guard BEFORE INSERT OR UPDATE OR DELETE ON {state}
          FOR EACH ROW EXECUTE FUNCTION blind_completion_state_guard()''')
        schema_editor.execute(f'''CREATE FUNCTION blind_completion_state_create() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN INSERT INTO {state} (submission_id, revoked) VALUES (NEW.submission_id_frozen, FALSE); RETURN NEW; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_completion_state_create AFTER INSERT ON {completion}
          FOR EACH ROW EXECUTE FUNCTION blind_completion_state_create()''')
        schema_editor.execute(f'''CREATE FUNCTION blind_completion_state_retire() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF TG_OP = 'DELETE' THEN
              IF EXISTS (SELECT 1 FROM {state} WHERE submission_id = OLD.id) THEN
                PERFORM id FROM {epoch} WHERE id = 1 FOR UPDATE;
              END IF;
              UPDATE {state} SET revoked = TRUE WHERE submission_id = OLD.id AND NOT revoked;
              RETURN OLD;
            END IF;
            IF OLD.id IS DISTINCT FROM NEW.id OR OLD.assignment_id IS DISTINCT FROM NEW.assignment_id
                OR OLD.submitted_by_id IS DISTINCT FROM NEW.submitted_by_id THEN
              IF EXISTS (SELECT 1 FROM {state} WHERE submission_id = OLD.id) THEN
                PERFORM id FROM {epoch} WHERE id = 1 FOR UPDATE;
              END IF;
              UPDATE {state} SET revoked = TRUE WHERE submission_id = OLD.id AND NOT revoked;
            END IF;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_completion_state_retire BEFORE UPDATE OR DELETE ON {submission}
          FOR EACH ROW EXECUTE FUNCTION blind_completion_state_retire()''')
    else:
        schema_editor.execute(f'''CREATE TRIGGER blind_completion_state_insert BEFORE INSERT ON {state}
          WHEN NEW.revoked OR EXISTS (SELECT 1 FROM {state} WHERE submission_id = NEW.submission_id)
               OR NOT EXISTS (SELECT 1 FROM {completion} WHERE submission_id_frozen = NEW.submission_id)
          BEGIN SELECT RAISE(ABORT, 'invalid completion state'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_completion_state_update BEFORE UPDATE ON {state}
          WHEN NEW.submission_id IS NOT OLD.submission_id OR (OLD.revoked AND NOT NEW.revoked)
          BEGIN SELECT RAISE(ABORT, 'completion revocation is irreversible'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_completion_state_delete BEFORE DELETE ON {state}
          BEGIN SELECT RAISE(ABORT, 'completion state is retained'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_completion_state_create AFTER INSERT ON {completion}
          BEGIN INSERT INTO {state} (submission_id, revoked) VALUES (NEW.submission_id_frozen, FALSE); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_completion_state_retire_delete BEFORE DELETE ON {submission}
          BEGIN UPDATE {state} SET revoked = TRUE WHERE submission_id = OLD.id AND NOT revoked; END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_completion_state_retire_update BEFORE UPDATE OF id, assignment_id, submitted_by_id ON {submission}
          WHEN OLD.id IS NOT NEW.id OR OLD.assignment_id IS NOT NEW.assignment_id OR OLD.submitted_by_id IS NOT NEW.submitted_by_id
          BEGIN UPDATE {state} SET revoked = TRUE WHERE submission_id = OLD.id AND NOT revoked; END''')
        # SQLite REPLACE can delete a conflicting row without firing DELETE
        # triggers. Reserve completed IDs before any replacement takes place.
        schema_editor.execute(f'''CREATE TRIGGER blind_completion_submission_insert BEFORE INSERT ON {submission}
          WHEN EXISTS (SELECT 1 FROM {state} WHERE submission_id = NEW.id)
          BEGIN SELECT RAISE(ABORT, 'completed submission identity is retained'); END''')


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor not in ('postgresql', 'sqlite'):
        return
    state = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindCompletionReadState')._meta.db_table)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f'SELECT 1 FROM {state} LIMIT 1')
        if cursor.fetchone():
            raise RuntimeError('Retained completion state cannot be discarded by rollback.')
    vendor = schema_editor.connection.vendor
    completion = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindMixCompletion')._meta.db_table)
    submission = schema_editor.quote_name(apps.get_model('tasks', 'Submission')._meta.db_table)
    if vendor == 'postgresql':
        for name, table in [('blind_completion_state_guard', state), ('blind_completion_state_create', completion),
                            ('blind_completion_state_retire', submission)]:
            schema_editor.execute(f'DROP TRIGGER {name} ON {table}')
            schema_editor.execute(f'DROP FUNCTION {name}()')
    else:
        for name in ('blind_completion_state_insert', 'blind_completion_state_update', 'blind_completion_state_delete',
                     'blind_completion_state_create', 'blind_completion_state_retire_delete',
                     'blind_completion_state_retire_update', 'blind_completion_submission_insert'):
            schema_editor.execute(f'DROP TRIGGER {name}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0051_blind_retired_evidence')]
    operations = [
        migrations.CreateModel(name='BlindCompletionReadState', fields=[
            ('submission_id', models.PositiveBigIntegerField(primary_key=True, serialize=False)),
            ('revoked', models.BooleanField(default=False)),
        ]),
        migrations.RunPython(install, uninstall),
    ]
