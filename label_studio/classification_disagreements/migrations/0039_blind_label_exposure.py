from django.db import migrations, models


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    table = q(apps.get_model('classification_disagreements', 'BlindLabelExposure')._meta.db_table)
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    # Earlier code could expose label metadata without recording access.
    schema_editor.execute(f'UPDATE {epoch} SET enabled = FALSE WHERE id = 1')
    if vendor == 'postgresql':
        schema_editor.execute('''CREATE FUNCTION blind_label_exposure_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'blind label exposure is immutable'; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_label_exposure_immutable BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION blind_label_exposure_immutable()''')
    else:
        for operation in ('UPDATE', 'DELETE'):
            schema_editor.execute(f'''CREATE TRIGGER blind_label_exposure_{operation.lower()}_guard
                BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'blind label exposure is immutable'); END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    schema_editor.execute(f'UPDATE {epoch} SET enabled = FALSE WHERE id = 1')
    if vendor == 'postgresql':
        table = q(apps.get_model('classification_disagreements', 'BlindLabelExposure')._meta.db_table)
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_label_exposure_immutable ON {table}')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_label_exposure_immutable()')
    else:
        for operation in ('update', 'delete'):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_label_exposure_{operation}_guard')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0038_blind_project_credential_fence')]
    operations = [
        migrations.CreateModel(name='BlindLabelExposure', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('account_id', models.PositiveBigIntegerField()),
            ('generation', models.PositiveBigIntegerField()),
        ], options={'constraints': [models.UniqueConstraint(fields=['account_id', 'generation'],
                                                             name='blind_label_exposure_unique')]}),
        migrations.RunPython(install, uninstall),
    ]
