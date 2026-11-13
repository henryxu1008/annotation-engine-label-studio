from django.db import migrations, models


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    table = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindTaskHandle')._meta.db_table)
    if vendor == 'postgresql':
        schema_editor.execute('''CREATE FUNCTION blind_task_handle_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'blind task handle is immutable'; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_task_handle_immutable BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION blind_task_handle_immutable()''')
    else:
        for suffix, operation in (('update', 'UPDATE'), ('delete', 'DELETE')):
            schema_editor.execute(f'''CREATE TRIGGER blind_task_handle_{suffix} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT, 'blind task handle is immutable'); END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'postgresql':
        table = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindTaskHandle')._meta.db_table)
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_task_handle_immutable ON {table}')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_task_handle_immutable()')
    elif vendor == 'sqlite':
        for suffix in ('update', 'delete'):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_task_handle_{suffix}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0033_blind_ml_capture_fence')]
    operations = [
        migrations.CreateModel(name='BlindTaskHandle', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('assignment_id_frozen', models.PositiveBigIntegerField(unique=True)),
            ('task_id_frozen', models.PositiveBigIntegerField()),
            ('project_id_frozen', models.PositiveBigIntegerField()),
            ('actor_id_frozen', models.PositiveBigIntegerField()),
            ('task_handle', models.PositiveBigIntegerField(unique=True)),
            ('assignment_handle', models.PositiveBigIntegerField(unique=True)),
        ]),
        migrations.RunPython(install, uninstall),
    ]
