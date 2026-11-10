"""Prevent concurrent captures from pinning the same known source twice."""
from django.db import migrations


def install(apps, schema_editor):
    table = 'classification_disagreements_bboxmediapin'
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(f"""CREATE UNIQUE INDEX bbox_unique_object_source ON {table}
            ((source->>'reference')) WHERE source->>'kind' IN ('gcs', 's3')""")
        schema_editor.execute(f"""CREATE UNIQUE INDEX bbox_unique_upload_source ON {table}
            ((source->>'upload_id')) WHERE source->>'kind' = 'managed-upload'""")
        schema_editor.execute(f"""CREATE UNIQUE INDEX bbox_unique_upload_reference ON {table}
            ((source->>'reference')) WHERE source->>'kind' = 'managed-upload'""")
        schema_editor.execute(f"""CREATE UNIQUE INDEX bbox_unique_upload_path ON {table}
            ((source->>'upload_name')) WHERE source->>'kind' = 'managed-upload'""")
    elif schema_editor.connection.vendor == 'sqlite':
        schema_editor.execute(f"""CREATE UNIQUE INDEX bbox_unique_object_source ON {table}
            (json_extract(source, '$.reference')) WHERE json_extract(source, '$.kind') IN ('gcs', 's3')""")
        schema_editor.execute(f"""CREATE UNIQUE INDEX bbox_unique_upload_source ON {table}
            (json_extract(source, '$.upload_id')) WHERE json_extract(source, '$.kind') = 'managed-upload'""")
        schema_editor.execute(f"""CREATE UNIQUE INDEX bbox_unique_upload_reference ON {table}
            (json_extract(source, '$.reference')) WHERE json_extract(source, '$.kind') = 'managed-upload'""")
        schema_editor.execute(f"""CREATE UNIQUE INDEX bbox_unique_upload_path ON {table}
            (json_extract(source, '$.upload_name')) WHERE json_extract(source, '$.kind') = 'managed-upload'""")


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor in ('postgresql', 'sqlite'):
        schema_editor.execute('DROP INDEX IF EXISTS bbox_unique_object_source')
        schema_editor.execute('DROP INDEX IF EXISTS bbox_unique_upload_source')
        schema_editor.execute('DROP INDEX IF EXISTS bbox_unique_upload_reference')
        schema_editor.execute('DROP INDEX IF EXISTS bbox_unique_upload_path')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0018_bbox_arbitration_guards')]
    operations = [migrations.RunPython(install, uninstall)]
