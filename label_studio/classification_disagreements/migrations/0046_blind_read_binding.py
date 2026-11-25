from django.db import migrations, models


def assignment_guard(apps, schema_editor, include_binding):
    vendor = schema_editor.connection.vendor
    q = schema_editor.quote_name
    assignment = q(apps.get_model('tasks', 'TaskAssignment')._meta.db_table)
    handles = q(apps.get_model('classification_disagreements', 'BlindTaskHandle')._meta.db_table)
    bindings = q(apps.get_model('classification_disagreements', 'BlindReadBinding')._meta.db_table)
    reused = f'EXISTS (SELECT 1 FROM {handles} WHERE assignment_id_frozen = NEW.id)'
    if include_binding:
        reused += f' OR EXISTS (SELECT 1 FROM {bindings} WHERE assignment_id_frozen = NEW.id)'
    if vendor == 'postgresql':
        schema_editor.execute(f'''CREATE OR REPLACE FUNCTION blind_assignment_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF {reused} THEN RAISE EXCEPTION 'bound assignment identity cannot be reused'; END IF;
              RETURN NEW; END $$''')
    elif vendor == 'sqlite':
        schema_editor.execute('DROP TRIGGER blind_assignment_identity_guard')
        schema_editor.execute(f'''CREATE TRIGGER blind_assignment_identity_guard AFTER INSERT ON {assignment}
            WHEN {reused} BEGIN SELECT RAISE(ABORT, 'bound assignment identity cannot be reused'); END''')


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    table = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindReadBinding')._meta.db_table)
    if vendor == 'postgresql':
        decision = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindMixDecision')._meta.db_table)
        epoch = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
        schema_editor.execute('''CREATE FUNCTION blind_read_binding_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'blind read binding is immutable'; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_read_binding_immutable BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION blind_read_binding_immutable()''')
        schema_editor.execute(f'''CREATE FUNCTION blind_read_decision_update_fence() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN PERFORM id FROM {epoch} WHERE id = 1 FOR UPDATE;
              RETURN NEW; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_read_decision_update_fence BEFORE UPDATE ON {decision}
            FOR EACH ROW EXECUTE FUNCTION blind_read_decision_update_fence()''')
        schema_editor.execute(f'''CREATE TRIGGER blind_read_decision_delete_fence BEFORE DELETE ON {decision}
            FOR EACH ROW EXECUTE FUNCTION blind_evidence_revoke_fence()''')
    else:
        for operation in ('UPDATE', 'DELETE'):
            schema_editor.execute(f'''CREATE TRIGGER blind_read_binding_{operation.lower()} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT, 'blind read binding is immutable'); END''')
    assignment_guard(apps, schema_editor, True)


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    assignment_guard(apps, schema_editor, False)
    table = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindReadBinding')._meta.db_table)
    if vendor == 'postgresql':
        decision = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindMixDecision')._meta.db_table)
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_read_decision_update_fence ON {decision}')
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_read_decision_delete_fence ON {decision}')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_read_decision_update_fence()')
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_read_binding_immutable ON {table}')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_read_binding_immutable()')
    else:
        for operation in ('update', 'delete'):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_read_binding_{operation}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0045_blind_assignment_identity')]
    operations = [
        migrations.CreateModel(name='BlindReadBinding', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('assignment_id_frozen', models.PositiveBigIntegerField(unique=True)),
            ('task_id_frozen', models.PositiveBigIntegerField()),
            ('project_id_frozen', models.PositiveBigIntegerField()),
            ('actor_id_frozen', models.PositiveBigIntegerField()),
            ('decision_id_frozen', models.PositiveBigIntegerField()),
            ('source_id_frozen', models.PositiveBigIntegerField()),
            ('source_task_id_frozen', models.PositiveBigIntegerField()),
            ('decision_request_hash', models.CharField(max_length=64)),
            ('purpose', models.CharField(max_length=12)),
            ('reference_id_frozen', models.PositiveBigIntegerField(null=True)),
            ('reference_hash', models.CharField(max_length=64, null=True)),
            ('proof_hash', models.CharField(max_length=64, null=True)),
            ('binding_hash', models.CharField(max_length=64)),
        ], options={'constraints': [models.CheckConstraint(
            condition=(models.Q(purpose='ordinary', reference_id_frozen__isnull=True,
                                reference_hash__isnull=True, proof_hash__isnull=True)
                       | models.Q(purpose='quality', reference_id_frozen__isnull=False,
                                  reference_hash__isnull=False, proof_hash__isnull=False)),
            name='blind_read_binding_purpose_valid',
        )]}),
        migrations.RunPython(install, uninstall),
    ]
