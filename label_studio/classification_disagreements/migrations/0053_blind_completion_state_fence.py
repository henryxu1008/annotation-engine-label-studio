from django.db import migrations


def _guard(apps, schema_editor, *, fence):
    if schema_editor.connection.vendor != 'postgresql':
        # SQLite's read fence already reserves the database for writing.
        return
    q = schema_editor.quote_name
    state = q(apps.get_model('classification_disagreements', 'BlindCompletionReadState')._meta.db_table)
    completion = q(apps.get_model('classification_disagreements', 'BlindMixCompletion')._meta.db_table)
    # PostgreSQL acquires the state row lock before a row-level trigger. A
    # direct UPDATE that then waits on the epoch would invert the lock order
    # of submission changes. Only the nested submission trigger may update it.
    lock = ("IF pg_trigger_depth() = 1 THEN RAISE EXCEPTION "
            "'completion state must be revoked through its submission'; END IF;") if fence else ''
    schema_editor.execute(f'''CREATE OR REPLACE FUNCTION blind_completion_state_guard() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'completion state is retained'; END IF;
        IF TG_OP = 'INSERT' THEN
          IF NEW.revoked OR EXISTS (SELECT 1 FROM {state} WHERE submission_id = NEW.submission_id)
              OR NOT EXISTS (SELECT 1 FROM {completion} WHERE submission_id_frozen = NEW.submission_id)
          THEN RAISE EXCEPTION 'invalid completion state'; END IF;
        ELSE
          IF NEW.submission_id <> OLD.submission_id OR (OLD.revoked AND NOT NEW.revoked)
          THEN RAISE EXCEPTION 'completion revocation is irreversible'; END IF;
          {lock}
        END IF;
        RETURN NEW;
      END $$''')


def install(apps, schema_editor):
    _guard(apps, schema_editor, fence=True)


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        state = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindCompletionReadState')._meta.db_table)
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(f'SELECT 1 FROM {state} LIMIT 1')
            if cursor.fetchone():
                raise RuntimeError('The completion state fence cannot be removed while retained state exists.')
    _guard(apps, schema_editor, fence=False)


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0052_blind_completion_read_state')]
    operations = [migrations.RunPython(install, uninstall)]
