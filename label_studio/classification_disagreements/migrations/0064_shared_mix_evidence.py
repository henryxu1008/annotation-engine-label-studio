"""Capture both mix outcomes without changing legacy quality-source evidence."""

from django.db import migrations, models


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    shared = q(apps.get_model('classification_disagreements', 'BlindMixSharedEvidence')._meta.db_table)
    decision = q(apps.get_model('classification_disagreements', 'BlindMixDecision')._meta.db_table)
    assignment = q(apps.get_model('tasks', 'TaskAssignment')._meta.db_table)
    project = q(apps.get_model('projects', 'Project')._meta.db_table)
    if vendor == 'postgresql':
        # Admission takes project and assignment locks before a decision write.
        schema_editor.execute(f'LOCK TABLE {project}, {assignment}, {decision} IN SHARE ROW EXCLUSIVE MODE')
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f'''SELECT 1 FROM {decision} WHERE selected = FALSE AND
          (source_id_frozen <> chosen_id_frozen OR reference_id_frozen IS NOT NULL) LIMIT 1''')
        if cursor.fetchone():
            raise RuntimeError('Invalid ordinary mix history cannot be backfilled.')
    fields = ('decision_id_frozen, project_id_frozen, source_id_frozen, source_task_id_frozen, '
              'source_assignee_id_frozen, chosen_id_frozen, selected')
    values = ('id, project_id, source_id_frozen, source_task_id_frozen, '
              'source_assignee_id_frozen, chosen_id_frozen, selected')
    schema_editor.execute(f'INSERT INTO {shared} ({fields}) SELECT {values} FROM {decision}')
    new_values = ', '.join('NEW.' + name for name in values.split(', '))
    if vendor == 'postgresql':
        schema_editor.execute('''CREATE FUNCTION blind_shared_mix_evidence_guard() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN IF TG_OP <> 'INSERT' OR pg_trigger_depth() = 1 THEN
            RAISE EXCEPTION 'shared mix evidence is trigger managed and retained'; END IF; RETURN NEW; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_shared_mix_evidence_guard BEFORE INSERT OR UPDATE OR DELETE ON {shared}
          FOR EACH ROW EXECUTE FUNCTION blind_shared_mix_evidence_guard()''')
        schema_editor.execute(f'''CREATE FUNCTION blind_shared_mix_capture() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN INSERT INTO {shared} ({fields}) VALUES ({new_values}); RETURN NEW; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_shared_mix_capture AFTER INSERT ON {decision}
          FOR EACH ROW EXECUTE FUNCTION blind_shared_mix_capture()''')
    else:
        for operation in ('UPDATE', 'DELETE'):
            schema_editor.execute(f'''CREATE TRIGGER blind_shared_mix_{operation.lower()} BEFORE {operation} ON {shared}
              BEGIN SELECT RAISE(ABORT, 'shared mix evidence is retained'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_shared_mix_insert BEFORE INSERT ON {shared}
          WHEN NOT EXISTS (SELECT 1 FROM {decision} d WHERE d.id = NEW.decision_id_frozen
            AND d.project_id = NEW.project_id_frozen AND d.source_id_frozen = NEW.source_id_frozen
            AND d.source_task_id_frozen = NEW.source_task_id_frozen
            AND d.source_assignee_id_frozen = NEW.source_assignee_id_frozen
            AND d.chosen_id_frozen = NEW.chosen_id_frozen AND d.selected = NEW.selected)
          BEGIN SELECT RAISE(ABORT, 'shared mix evidence must match its decision'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_shared_mix_capture AFTER INSERT ON {decision}
          BEGIN INSERT INTO {shared} ({fields}) VALUES ({new_values}); END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    shared = q(apps.get_model('classification_disagreements', 'BlindMixSharedEvidence')._meta.db_table)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f'SELECT 1 FROM {shared} LIMIT 1')
        if cursor.fetchone():
            raise RuntimeError('Retained shared mix evidence cannot be discarded by rollback.')
    if vendor == 'postgresql':
        decision = q(apps.get_model('classification_disagreements', 'BlindMixDecision')._meta.db_table)
        schema_editor.execute(f'DROP TRIGGER blind_shared_mix_capture ON {decision}')
        schema_editor.execute('DROP FUNCTION blind_shared_mix_capture()')
        schema_editor.execute(f'DROP TRIGGER blind_shared_mix_evidence_guard ON {shared}')
        schema_editor.execute('DROP FUNCTION blind_shared_mix_evidence_guard()')
    else:
        for name in ('blind_shared_mix_capture', 'blind_shared_mix_insert', 'blind_shared_mix_update', 'blind_shared_mix_delete'):
            schema_editor.execute(f'DROP TRIGGER {name}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0063_blind_shared_exposure_writes')]
    operations = [
        migrations.CreateModel(name='BlindMixSharedEvidence', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('decision_id_frozen', models.PositiveBigIntegerField(unique=True)),
            ('project_id_frozen', models.PositiveBigIntegerField()),
            ('source_id_frozen', models.PositiveBigIntegerField()),
            ('source_task_id_frozen', models.PositiveBigIntegerField()),
            ('source_assignee_id_frozen', models.PositiveBigIntegerField()),
            ('chosen_id_frozen', models.PositiveBigIntegerField(unique=True)),
            ('selected', models.BooleanField()),
        ]),
        migrations.RunPython(install, uninstall),
    ]
