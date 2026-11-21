from django.db import migrations


MODELS = (
    'BlindAssignmentExposure',
    'BlindProjectFileExposure',
    'BlindLabelExposure',
    'BlindTextLengthExposure',
    'BlindReferenceTextLength',
)


def install(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    for model in MODELS:
        table = schema_editor.quote_name(apps.get_model('classification_disagreements', model)._meta.db_table)
        schema_editor.execute(f'''CREATE TRIGGER blind_exposure_row_fence BEFORE INSERT ON {table}
            FOR EACH ROW EXECUTE FUNCTION blind_exposure_write_fence()''')
    reference = schema_editor.quote_name(
        apps.get_model('classification_disagreements', 'ClassificationGoldEvidence')._meta.db_table,
    )
    schema_editor.execute(f'''CREATE TRIGGER blind_reference_delete_fence BEFORE DELETE ON {reference}
        FOR EACH ROW WHEN (OLD.kind = 'reference') EXECUTE FUNCTION blind_evidence_revoke_fence()''')


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    for model in MODELS:
        table = schema_editor.quote_name(apps.get_model('classification_disagreements', model)._meta.db_table)
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_exposure_row_fence ON {table}')
    reference = schema_editor.quote_name(
        apps.get_model('classification_disagreements', 'ClassificationGoldEvidence')._meta.db_table,
    )
    schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_reference_delete_fence ON {reference}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0042_blind_exposure_write_fence')]
    operations = [migrations.RunPython(install, uninstall)]
