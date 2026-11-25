"""Fence concrete parent tables used by built-in import storages."""

from django.db import migrations

PARENTS = (
    ('GCSStorageMixin', 'GCSImportStorage'),
    ('AzureBlobStorageMixin', 'AzureBlobImportStorage'),
    ('RedisStorageMixin', 'RedisImportStorage'),
    ('LocalFilesMixin', 'LocalFilesImportStorage'),
)


def table_info(apps, schema_editor, parent_name, child_name):
    parent = apps.get_model('io_storages', parent_name)
    child = apps.get_model('io_storages', child_name)
    link = child._meta.get_ancestor_link(parent)
    if link is None:
        raise RuntimeError('Blind storage parent identity is unavailable.')
    q = schema_editor.quote_name
    return q(parent._meta.db_table), q(parent._meta.pk.column), q(child._meta.db_table), q(link.column)


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    # Previous captures could have missed parent-table changes. Existing
    # certificates must not remain usable across this migration.
    schema_editor.execute(f'UPDATE {epoch} SET enabled = FALSE WHERE id = 1')
    for index, (parent_name, child_name) in enumerate(PARENTS):
        parent, pk, child, link = table_info(apps, schema_editor, parent_name, child_name)
        exists = f'EXISTS (SELECT 1 FROM {child} WHERE {link} = OLD.{pk})'
        if vendor == 'postgresql':
            function = q(f'blind_storage_parent_fence_{index}')
            trigger = q(f'blind_storage_parent_fence_{index}')
            schema_editor.execute(f'''CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$
              BEGIN
                IF {exists} THEN UPDATE {epoch} SET enabled = FALSE WHERE id = 1; END IF;
                IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
              END $$''')
            schema_editor.execute(f'''CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {parent}
                FOR EACH ROW EXECUTE FUNCTION {function}()''')
        else:
            for suffix, operation in (('update', 'UPDATE'), ('delete', 'DELETE')):
                trigger = q(f'blind_storage_parent_fence_{index}_{suffix}')
                schema_editor.execute(f'''CREATE TRIGGER {trigger} BEFORE {operation} ON {parent}
                  WHEN {exists} BEGIN UPDATE {epoch} SET enabled = FALSE WHERE id = 1; END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    schema_editor.execute(f'UPDATE {epoch} SET enabled = FALSE WHERE id = 1')
    for index, (parent_name, child_name) in enumerate(PARENTS):
        parent, _, _, _ = table_info(apps, schema_editor, parent_name, child_name)
        if vendor == 'postgresql':
            trigger = q(f'blind_storage_parent_fence_{index}')
            schema_editor.execute(f'DROP TRIGGER {trigger} ON {parent}')
            schema_editor.execute(f'DROP FUNCTION {trigger}()')
        else:
            for suffix in ('update', 'delete'):
                schema_editor.execute('DROP TRIGGER ' + q(f'blind_storage_parent_fence_{index}_{suffix}'))


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0068_blind_work_reference_lengths')]
    operations = [migrations.RunPython(install, uninstall)]
