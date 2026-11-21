from django.db import migrations


MODELS = ('S3ImportStorage', 'GCSImportStorage', 'AzureBlobImportStorage', 'RedisImportStorage',
          'LocalFilesImportStorage')


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    if vendor == 'postgresql':
        schema_editor.execute(f'''CREATE FUNCTION blind_storage_capture_invalidate() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN UPDATE {epoch} SET enabled = FALSE WHERE id = 1; RETURN NULL; END $$''')
    for index, model_name in enumerate(MODELS):
        table = q(apps.get_model('io_storages', model_name)._meta.db_table)
        if vendor == 'postgresql':
            schema_editor.execute(f'''CREATE TRIGGER blind_storage_capture_{index}
                AFTER INSERT OR UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION blind_storage_capture_invalidate()''')
        else:
            for suffix, operation in (('insert', 'INSERT'), ('update', 'UPDATE'), ('delete', 'DELETE')):
                schema_editor.execute(f'''CREATE TRIGGER blind_storage_capture_{index}_{suffix}
                    AFTER {operation} ON {table} BEGIN UPDATE {epoch} SET enabled = FALSE WHERE id = 1; END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    for index, model_name in enumerate(MODELS):
        if vendor == 'postgresql':
            table = schema_editor.quote_name(apps.get_model('io_storages', model_name)._meta.db_table)
            schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_storage_capture_{index} ON {table}')
        else:
            for suffix in ('insert', 'update', 'delete'):
                schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_storage_capture_{index}_{suffix}')
    if vendor == 'postgresql':
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_storage_capture_invalidate()')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0029_blind_project_file_exposure'),
                    ('io_storages', '0022_normalize_localfiles_paths')]
    operations = [migrations.RunPython(install, uninstall)]
