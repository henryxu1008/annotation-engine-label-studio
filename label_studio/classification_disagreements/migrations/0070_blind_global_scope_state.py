"""Expose current built-in external scope to both blind work purposes."""

from django.db import migrations, models


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    project = q(apps.get_model('projects', 'Project')._meta.db_table)
    backend = q(apps.get_model('ml', 'MLBackend')._meta.db_table)
    storage_names = (
        ('s3', 'S3ImportStorage'), ('gcs', 'GCSImportStorage'),
        ('azure', 'AzureBlobImportStorage'), ('redis', 'RedisImportStorage'),
    )
    facts = [
        ('credentials', f"EXISTS (SELECT 1 FROM {project} WHERE COALESCE(task_data_login, '') <> '' "
                        "OR COALESCE(task_data_password, '') <> '')"),
        ('model', f'EXISTS (SELECT 1 FROM {backend})'),
    ]
    for key, name in storage_names:
        table = q(apps.get_model('io_storages', name)._meta.db_table)
        facts.append((key, f'EXISTS (SELECT 1 FROM {table})'))
    function = 'jsonb_build_object' if vendor == 'postgresql' else 'json_object'
    arguments = ', '.join(f"'{key}', {expression}" for key, expression in facts)
    view = q('classification_blind_global_scope_state')
    schema_editor.execute(f'CREATE VIEW {view} AS SELECT 1 AS id, {function}({arguments}) AS facts')


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor in ('postgresql', 'sqlite'):
        schema_editor.execute('DROP VIEW ' + schema_editor.quote_name('classification_blind_global_scope_state'))


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0069_blind_storage_parent_fences')]
    operations = [
        migrations.CreateModel(name='BlindGlobalScopeState', fields=[
            ('id', models.PositiveSmallIntegerField(primary_key=True, serialize=False)),
            ('facts', models.JSONField()),
        ], options={'managed': False, 'db_table': 'classification_blind_global_scope_state'}),
        migrations.RunPython(install, uninstall),
    ]
