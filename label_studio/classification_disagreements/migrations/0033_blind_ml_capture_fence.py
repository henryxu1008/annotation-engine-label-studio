from django.db import migrations


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    backend = q(apps.get_model('ml', 'MLBackend')._meta.db_table)
    schema_editor.execute(f'UPDATE {epoch} SET enabled = FALSE WHERE id = 1')
    if vendor == 'postgresql':
        schema_editor.execute(f'''CREATE FUNCTION blind_ml_capture_invalidate() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN UPDATE {epoch} SET enabled = FALSE WHERE id = 1; RETURN NULL; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_ml_capture_change AFTER INSERT OR UPDATE OR DELETE ON {backend}
            FOR EACH ROW EXECUTE FUNCTION blind_ml_capture_invalidate()''')
    else:
        for suffix, operation in (('insert', 'INSERT'), ('update', 'UPDATE'), ('delete', 'DELETE')):
            schema_editor.execute(f'''CREATE TRIGGER blind_ml_capture_{suffix} AFTER {operation} ON {backend}
                BEGIN UPDATE {epoch} SET enabled = FALSE WHERE id = 1; END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'postgresql':
        backend = schema_editor.quote_name(apps.get_model('ml', 'MLBackend')._meta.db_table)
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_ml_capture_change ON {backend}')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_ml_capture_invalidate()')
    elif vendor == 'sqlite':
        for suffix in ('insert', 'update', 'delete'):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_ml_capture_{suffix}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0032_blind_text_length_exposure'),
                    ('ml', '0007_auto_20240314_1957')]
    operations = [migrations.RunPython(install, uninstall)]
