from django.db import migrations, models


def disarm(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    assignment = q(apps.get_model('tasks', 'TaskAssignment')._meta.db_table)
    binding = q(apps.get_model('classification_disagreements', 'BlindReadBinding')._meta.db_table)
    schema_editor.execute(f'UPDATE {epoch} SET enabled = FALSE WHERE id = 1')
    if vendor == 'postgresql':
        schema_editor.execute(f'DROP TRIGGER blind_assignment_identity_guard ON {assignment}')
        schema_editor.execute(f'DROP TRIGGER blind_read_binding_immutable ON {binding}')
    else:
        for name in ('blind_assignment_identity_guard', 'blind_read_binding_update', 'blind_read_binding_delete'):
            schema_editor.execute(f'DROP TRIGGER {name}')


def arm(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    assignment = q(apps.get_model('tasks', 'TaskAssignment')._meta.db_table)
    handles = q(apps.get_model('classification_disagreements', 'BlindTaskHandle')._meta.db_table)
    binding = q(apps.get_model('classification_disagreements', 'BlindReadBinding')._meta.db_table)
    if vendor == 'postgresql':
        schema_editor.execute(f'''CREATE TRIGGER blind_assignment_identity_guard AFTER INSERT ON {assignment}
            FOR EACH ROW EXECUTE FUNCTION blind_assignment_identity_guard()''')
        schema_editor.execute(f'''CREATE TRIGGER blind_read_binding_immutable BEFORE UPDATE OR DELETE ON {binding}
            FOR EACH ROW EXECUTE FUNCTION blind_read_binding_immutable()''')
    else:
        schema_editor.execute(f'''CREATE TRIGGER blind_assignment_identity_guard AFTER INSERT ON {assignment}
            WHEN EXISTS (SELECT 1 FROM {handles} WHERE assignment_id_frozen = NEW.id)
              OR EXISTS (SELECT 1 FROM {binding} WHERE assignment_id_frozen = NEW.id)
            BEGIN SELECT RAISE(ABORT, 'bound assignment identity cannot be reused'); END''')
        for operation in ('UPDATE', 'DELETE'):
            schema_editor.execute(f'''CREATE TRIGGER blind_read_binding_{operation.lower()} BEFORE {operation} ON {binding}
                BEGIN SELECT RAISE(ABORT, 'blind read binding is immutable'); END''')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0046_blind_read_binding')]
    operations = [
        migrations.RunPython(disarm, arm),
        migrations.AlterField(model_name='blindreadbinding', name='decision_id_frozen',
                              field=models.PositiveBigIntegerField(null=True)),
        migrations.AlterField(model_name='blindreadbinding', name='source_id_frozen',
                              field=models.PositiveBigIntegerField(null=True)),
        migrations.AlterField(model_name='blindreadbinding', name='source_task_id_frozen',
                              field=models.PositiveBigIntegerField(null=True)),
        migrations.AlterField(model_name='blindreadbinding', name='decision_request_hash',
                              field=models.CharField(max_length=64, null=True)),
        migrations.AddConstraint(model_name='blindreadbinding', constraint=models.CheckConstraint(
            condition=(models.Q(decision_id_frozen__isnull=True, source_id_frozen__isnull=True,
                                source_task_id_frozen__isnull=True, decision_request_hash__isnull=True)
                       | models.Q(decision_id_frozen__isnull=False, source_id_frozen__isnull=False,
                                  source_task_id_frozen__isnull=False, decision_request_hash__isnull=False)),
            name='blind_read_binding_decision_valid',
        )),
        migrations.RunPython(arm, disarm),
    ]
