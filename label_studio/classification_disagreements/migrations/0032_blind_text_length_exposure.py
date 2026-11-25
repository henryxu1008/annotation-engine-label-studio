from django.db import migrations, models


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    exposure = q(apps.get_model('classification_disagreements', 'BlindTextLengthExposure')._meta.db_table)
    ref = q(apps.get_model('classification_disagreements', 'BlindReferenceTextLength')._meta.db_table)
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    cert = q(apps.get_model('classification_disagreements', 'BlindCaptureCertificate')._meta.db_table)
    assignment = q(apps.get_model('tasks', 'TaskAssignment')._meta.db_table)
    task = q(apps.get_model('tasks', 'Task')._meta.db_table)
    if vendor == 'postgresql':
        schema_editor.execute(f'''
          CREATE FUNCTION blind_record_text_lengths(aid bigint, uid bigint, tid bigint) RETURNS void LANGUAGE plpgsql AS $$
          DECLARE gen bigint; payload jsonb;
          BEGIN
            SELECT e.generation INTO gen FROM {epoch} e JOIN {cert} c
              ON c.account_id = uid AND c.generation = e.generation
              WHERE e.id = 1 AND e.enabled = TRUE FOR SHARE OF e;
            IF gen IS NULL THEN RETURN; END IF;
            SELECT data INTO payload FROM {task} WHERE id = tid;
            IF payload IS NULL OR jsonb_typeof(payload) <> 'object' OR octet_length(payload::text) > 262144
               OR (SELECT count(*) FROM jsonb_object_keys(CASE WHEN jsonb_typeof(payload) = 'object'
                                                          THEN payload ELSE '{{}}'::jsonb END)) > 100 THEN
              INSERT INTO {exposure} (account_id, assignment_id, generation, byte_length)
                VALUES (uid, aid, gen, -1) ON CONFLICT DO NOTHING;
              RETURN;
            END IF;
            INSERT INTO {exposure} (account_id, assignment_id, generation, byte_length)
              SELECT uid, aid, gen, octet_length(convert_to(value #>> '{{}}', 'UTF8'))
              FROM jsonb_each(payload) WHERE jsonb_typeof(value) = 'string' ON CONFLICT DO NOTHING;
          END $$''')
        schema_editor.execute('''CREATE FUNCTION blind_assignment_text_capture() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN PERFORM blind_record_text_lengths(NEW.id, NEW.assignee_id, NEW.task_id); RETURN NEW; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_assignment_text_capture
          AFTER INSERT OR UPDATE OF assignee_id, task_id ON {assignment}
          FOR EACH ROW EXECUTE FUNCTION blind_assignment_text_capture()''')
        schema_editor.execute(f'''CREATE FUNCTION blind_task_text_capture() RETURNS trigger LANGUAGE plpgsql AS $$
          DECLARE a record; BEGIN
            IF NOT EXISTS (SELECT 1 FROM {epoch} WHERE id = 1 AND enabled = TRUE) THEN RETURN NEW; END IF;
            FOR a IN SELECT id, assignee_id FROM {assignment} WHERE task_id = NEW.id
                     AND status IN ('assigned', 'in_progress') LOOP
              PERFORM blind_record_text_lengths(a.id, a.assignee_id, NEW.id);
            END LOOP; RETURN NEW; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_task_text_capture AFTER UPDATE OF data ON {task}
          FOR EACH ROW EXECUTE FUNCTION blind_task_text_capture()''')
        schema_editor.execute('''CREATE FUNCTION blind_text_evidence_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN RAISE EXCEPTION 'blind text exposure is immutable'; END $$''')
        for name, table in (('blind_text_exposure_immutable', exposure), ('blind_reference_length_immutable', ref)):
            schema_editor.execute(f'''CREATE TRIGGER {name} BEFORE UPDATE OR DELETE ON {table}
              FOR EACH ROW EXECUTE FUNCTION blind_text_evidence_immutable()''')
    else:
        valid = "json_type(t.data) = 'object' AND length(CAST(t.data AS BLOB)) <= 262144 AND (SELECT count(*) FROM json_each(t.data)) <= 100"
        for suffix, event in (('insert', 'INSERT'), ('update', 'UPDATE OF assignee_id, task_id')):
            schema_editor.execute(f'''CREATE TRIGGER blind_assignment_text_{suffix} AFTER {event} ON {assignment}
              BEGIN
                INSERT OR IGNORE INTO {exposure} (account_id, assignment_id, generation, byte_length)
                  SELECT NEW.assignee_id, NEW.id, e.generation, -1 FROM {epoch} e
                  JOIN {cert} c ON c.account_id = NEW.assignee_id AND c.generation = e.generation
                  JOIN {task} t ON t.id = NEW.task_id WHERE e.id = 1 AND e.enabled = TRUE AND NOT ({valid});
                INSERT OR IGNORE INTO {exposure} (account_id, assignment_id, generation, byte_length)
                  SELECT NEW.assignee_id, NEW.id, e.generation, length(CAST(v.value AS BLOB)) FROM {epoch} e
                  JOIN {cert} c ON c.account_id = NEW.assignee_id AND c.generation = e.generation
                  JOIN {task} t ON t.id = NEW.task_id JOIN json_each(t.data) v
                  WHERE e.id = 1 AND e.enabled = TRUE AND ({valid}) AND v.type = 'text';
              END''')
        new_valid = valid.replace('t.data', 'NEW.data')
        schema_editor.execute(f'''CREATE TRIGGER blind_task_text_capture AFTER UPDATE OF data ON {task}
          BEGIN
            INSERT OR IGNORE INTO {exposure} (account_id, assignment_id, generation, byte_length)
              SELECT a.assignee_id, a.id, e.generation, -1 FROM {assignment} a
              JOIN {epoch} e ON e.id = 1 AND e.enabled = TRUE
              JOIN {cert} c ON c.account_id = a.assignee_id AND c.generation = e.generation
              WHERE a.task_id = NEW.id AND a.status IN ('assigned', 'in_progress') AND NOT ({new_valid});
            INSERT OR IGNORE INTO {exposure} (account_id, assignment_id, generation, byte_length)
              SELECT a.assignee_id, a.id, e.generation, length(CAST(v.value AS BLOB)) FROM {assignment} a
              JOIN {epoch} e ON e.id = 1 AND e.enabled = TRUE
              JOIN {cert} c ON c.account_id = a.assignee_id AND c.generation = e.generation JOIN json_each(NEW.data) v
              WHERE a.task_id = NEW.id AND a.status IN ('assigned', 'in_progress') AND ({new_valid}) AND v.type = 'text';
          END''')
        for prefix, table in (('blind_text_exposure', exposure), ('blind_reference_length', ref)):
            for operation in ('UPDATE', 'DELETE'):
                schema_editor.execute(f'''CREATE TRIGGER {prefix}_{operation.lower()}_guard BEFORE {operation} ON {table}
                  BEGIN SELECT RAISE(ABORT, 'blind text exposure is immutable'); END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    q = schema_editor.quote_name
    if vendor == 'postgresql':
        assignment = q(apps.get_model('tasks', 'TaskAssignment')._meta.db_table)
        task = q(apps.get_model('tasks', 'Task')._meta.db_table)
        exposure = q(apps.get_model('classification_disagreements', 'BlindTextLengthExposure')._meta.db_table)
        ref = q(apps.get_model('classification_disagreements', 'BlindReferenceTextLength')._meta.db_table)
        for name, table in (('blind_assignment_text_capture', assignment), ('blind_task_text_capture', task),
                            ('blind_text_exposure_immutable', exposure), ('blind_reference_length_immutable', ref)):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name} ON {table}')
        for name in ('blind_assignment_text_capture', 'blind_task_text_capture', 'blind_text_evidence_immutable'):
            schema_editor.execute(f'DROP FUNCTION IF EXISTS {name}()')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_record_text_lengths(bigint, bigint, bigint)')
    elif vendor == 'sqlite':
        for name in ('blind_assignment_text_insert', 'blind_assignment_text_update', 'blind_task_text_capture',
                     'blind_text_exposure_update_guard', 'blind_text_exposure_delete_guard',
                     'blind_reference_length_update_guard', 'blind_reference_length_delete_guard'):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0031_blind_mix_decision')]
    operations = [
        migrations.CreateModel(name='BlindTextLengthExposure', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('account_id', models.PositiveBigIntegerField()),
            ('assignment_id', models.PositiveBigIntegerField()),
            ('generation', models.PositiveBigIntegerField()),
            ('byte_length', models.IntegerField()),
        ], options={'constraints': [models.UniqueConstraint(
            fields=['account_id', 'assignment_id', 'generation', 'byte_length'], name='blind_text_length_unique',
        )]}),
        migrations.CreateModel(name='BlindReferenceTextLength', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('reference_id', models.PositiveBigIntegerField()),
            ('byte_length', models.PositiveIntegerField()),
        ], options={'constraints': [models.UniqueConstraint(
            fields=['reference_id', 'byte_length'], name='blind_reference_length_unique',
        )]}),
        migrations.RunPython(install, uninstall),
    ]
