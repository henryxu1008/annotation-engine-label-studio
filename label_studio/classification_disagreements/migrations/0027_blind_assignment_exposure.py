from django.db import migrations, models


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    exposure = q(apps.get_model('classification_disagreements', 'BlindAssignmentExposure')._meta.db_table)
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    cert = q(apps.get_model('classification_disagreements', 'BlindCaptureCertificate')._meta.db_table)
    assignment = q(apps.get_model('tasks', 'TaskAssignment')._meta.db_table)
    if vendor == 'postgresql':
        schema_editor.execute(f'''
            CREATE FUNCTION blind_assignment_exposure_capture() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              INSERT INTO {exposure} (account_id, task_id, project_id, generation)
                SELECT NEW.assignee_id, NEW.task_id, NEW.project_id, e.generation
                FROM {epoch} e JOIN {cert} c ON c.account_id = NEW.assignee_id AND c.generation = e.generation
                WHERE e.id = 1 AND e.enabled = TRUE FOR SHARE OF e
              ON CONFLICT (account_id, task_id, generation) DO NOTHING;
              RETURN NEW;
            END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_assignment_exposure_capture
            AFTER INSERT OR UPDATE OF assignee_id, task_id ON {assignment}
            FOR EACH ROW EXECUTE FUNCTION blind_assignment_exposure_capture()''')
        schema_editor.execute('''CREATE FUNCTION blind_assignment_exposure_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'blind assignment exposure is immutable'; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_assignment_exposure_immutable
            BEFORE UPDATE OR DELETE ON {exposure}
            FOR EACH ROW EXECUTE FUNCTION blind_assignment_exposure_immutable()''')
    else:
        for suffix, event in (('insert', 'INSERT'), ('update', 'UPDATE OF assignee_id, task_id')):
            schema_editor.execute(f'''CREATE TRIGGER blind_assignment_exposure_{suffix} AFTER {event} ON {assignment}
              BEGIN INSERT OR IGNORE INTO {exposure} (account_id, task_id, project_id, generation)
                SELECT NEW.assignee_id, NEW.task_id, NEW.project_id, e.generation
                FROM {epoch} e JOIN {cert} c ON c.account_id = NEW.assignee_id AND c.generation = e.generation
                WHERE e.id = 1 AND e.enabled = TRUE; END''')
        for operation in ('UPDATE', 'DELETE'):
            schema_editor.execute(f'''CREATE TRIGGER blind_assignment_exposure_{operation.lower()}_guard
              BEFORE {operation} ON {exposure}
              BEGIN SELECT RAISE(ABORT, 'blind assignment exposure is immutable'); END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    q = schema_editor.quote_name
    if vendor == 'postgresql':
        assignment = q(apps.get_model('tasks', 'TaskAssignment')._meta.db_table)
        exposure = q(apps.get_model('classification_disagreements', 'BlindAssignmentExposure')._meta.db_table)
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_assignment_exposure_capture ON {assignment}')
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_assignment_exposure_immutable ON {exposure}')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_assignment_exposure_capture()')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_assignment_exposure_immutable()')
    elif vendor == 'sqlite':
        for name in ('blind_assignment_exposure_insert', 'blind_assignment_exposure_update',
                     'blind_assignment_exposure_update_guard', 'blind_assignment_exposure_delete_guard'):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0026_blind_score_outbox')]
    operations = [
        migrations.CreateModel(name='BlindAssignmentExposure', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('account_id', models.PositiveBigIntegerField()),
            ('task_id', models.PositiveBigIntegerField()),
            ('project_id', models.PositiveBigIntegerField()),
            ('generation', models.PositiveBigIntegerField()),
        ], options={'constraints': [models.UniqueConstraint(
            fields=['account_id', 'task_id', 'generation'], name='blind_assignment_exposure_unique',
        )]}),
        migrations.RunPython(install, uninstall),
    ]
