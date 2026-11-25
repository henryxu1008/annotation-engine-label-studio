from django.db import migrations


def install(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        # SQLite's capture-read path reserves the database for writing.
        return
    q = schema_editor.quote_name
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    completion = q(apps.get_model('classification_disagreements', 'BlindMixCompletion')._meta.db_table)
    submission = q(apps.get_model('tasks', 'Submission')._meta.db_table)
    schema_editor.execute(f'''CREATE FUNCTION blind_completion_submission_fence() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF EXISTS (SELECT 1 FROM {completion} WHERE submission_id_frozen = OLD.id) THEN
          PERFORM id FROM {epoch} WHERE id = 1 FOR UPDATE;
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
      END $$''')
    schema_editor.execute(f'''CREATE TRIGGER blind_completion_submission_fence BEFORE UPDATE OR DELETE ON {submission}
      FOR EACH ROW EXECUTE FUNCTION blind_completion_submission_fence()''')


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    submission = schema_editor.quote_name(apps.get_model('tasks', 'Submission')._meta.db_table)
    schema_editor.execute(f'DROP TRIGGER blind_completion_submission_fence ON {submission}')
    schema_editor.execute('DROP FUNCTION blind_completion_submission_fence()')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0049_blind_capture_identity')]
    operations = [migrations.RunPython(install, uninstall)]
