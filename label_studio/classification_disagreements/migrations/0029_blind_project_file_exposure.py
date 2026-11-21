from django.db import migrations, models


def install(apps, schema_editor):
    q = schema_editor.quote_name
    table = q(apps.get_model('classification_disagreements', 'BlindProjectFileExposure')._meta.db_table)
    vendor = schema_editor.connection.vendor
    if vendor == 'postgresql':
        schema_editor.execute('''CREATE FUNCTION blind_project_file_exposure_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'blind file exposure is immutable'; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_project_file_exposure_immutable
            BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW
            EXECUTE FUNCTION blind_project_file_exposure_immutable()''')
    elif vendor == 'sqlite':
        for operation in ('UPDATE', 'DELETE'):
            schema_editor.execute(f'''CREATE TRIGGER blind_project_file_exposure_{operation.lower()}_guard
                BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'blind file exposure is immutable'); END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'postgresql':
        table = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindProjectFileExposure')._meta.db_table)
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_project_file_exposure_immutable ON {table}')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_project_file_exposure_immutable()')
    elif vendor == 'sqlite':
        for name in ('blind_project_file_exposure_update_guard', 'blind_project_file_exposure_delete_guard'):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0028_blind_dispatch_receipt')]
    operations = [
        migrations.CreateModel(name='BlindProjectFileExposure', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('account_id', models.PositiveBigIntegerField()),
            ('project_id', models.PositiveBigIntegerField()),
            ('generation', models.PositiveBigIntegerField()),
        ], options={'constraints': [models.UniqueConstraint(
            fields=['account_id', 'project_id', 'generation'], name='blind_project_file_exposure_unique',
        )]}),
        migrations.RunPython(install, uninstall),
    ]
