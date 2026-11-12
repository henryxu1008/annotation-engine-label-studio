from django.db import migrations, models


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    table = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindResourceHandle')._meta.db_table)
    if vendor == 'postgresql':
        schema_editor.execute('''CREATE FUNCTION blind_resource_handle_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'blind resource handle is immutable'; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_resource_handle_immutable BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION blind_resource_handle_immutable()''')
    elif vendor == 'sqlite':
        for suffix, operation in (('update', 'UPDATE'), ('delete', 'DELETE')):
            schema_editor.execute(f'''CREATE TRIGGER blind_resource_handle_{suffix} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT, 'blind resource handle is immutable'); END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'postgresql':
        table = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindResourceHandle')._meta.db_table)
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_resource_handle_immutable ON {table}')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_resource_handle_immutable()')
    elif vendor == 'sqlite':
        for suffix in ('update', 'delete'):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_resource_handle_{suffix}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0036_blind_source_reassignment_guard')]
    operations = [
        migrations.CreateModel(name='BlindResourceHandle', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('assignment_id_frozen', models.PositiveBigIntegerField()),
            ('task_id_frozen', models.PositiveBigIntegerField()),
            ('project_id_frozen', models.PositiveBigIntegerField()),
            ('actor_id_frozen', models.PositiveBigIntegerField()),
            ('kind', models.CharField(max_length=16)),
            ('object_id_frozen', models.PositiveBigIntegerField()),
            ('handle', models.PositiveBigIntegerField(unique=True)),
        ], options={'constraints': [
            models.UniqueConstraint(fields=('kind', 'object_id_frozen'), name='blind_resource_object_unique'),
            models.CheckConstraint(condition=models.Q(kind__in=['annotation', 'draft']), name='blind_resource_kind_valid'),
        ]}),
        migrations.RunPython(install, uninstall),
    ]
