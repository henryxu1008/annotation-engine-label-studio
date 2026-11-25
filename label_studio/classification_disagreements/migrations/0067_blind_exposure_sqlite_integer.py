"""Reject SQLite JSON integers which are stored as out-of-range reals."""

from django.db import migrations

OLD = "AND json_type(p.manifest, '$.source_task_id') = 'integer'"
NEW = OLD + " AND typeof(json_extract(p.manifest, '$.source_task_id')) = 'integer'"
VIEW = 'classification_blind_work_exposure_state'


def replace_definition(schema_editor, source, target):
    if schema_editor.connection.vendor != 'sqlite':
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT sql FROM sqlite_master WHERE type = 'view' AND name = %s", [VIEW])
        row = cursor.fetchone()
    if row is None or row[0].count(source) != 1:
        raise RuntimeError('Unexpected blind exposure view definition; refusing to change it.')
    definition = row[0].replace(source, target, 1)
    schema_editor.execute('DROP VIEW ' + schema_editor.quote_name(VIEW))
    schema_editor.execute(definition)


def install(apps, schema_editor):
    replace_definition(schema_editor, OLD, NEW)


def uninstall(apps, schema_editor):
    replace_definition(schema_editor, NEW, OLD)


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0066_blind_work_exposure_state')]
    operations = [migrations.RunPython(install, uninstall)]
