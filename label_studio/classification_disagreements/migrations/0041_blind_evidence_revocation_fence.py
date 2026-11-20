from django.db import migrations


def install(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    q = schema_editor.quote_name
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    # These live evidence rows can be deleted directly through the ORM or SQL.
    # Fence deletion even when it bypasses an application service; the lock is
    # held until the deleting transaction commits.
    schema_editor.execute(f'''CREATE FUNCTION blind_evidence_revoke_fence() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          PERFORM id FROM {epoch} WHERE id = 1 FOR UPDATE;
          RETURN OLD;
        END $$''')
    for model in ('BlindCaptureCertificate', 'BlindPrivilegeHistory', 'BlindProvenanceEvent'):
        table = q(apps.get_model('classification_disagreements', model)._meta.db_table)
        schema_editor.execute(f'''CREATE TRIGGER blind_evidence_revoke_fence BEFORE DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION blind_evidence_revoke_fence()''')


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    q = schema_editor.quote_name
    for model in ('BlindCaptureCertificate', 'BlindPrivilegeHistory', 'BlindProvenanceEvent'):
        table = q(apps.get_model('classification_disagreements', model)._meta.db_table)
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_evidence_revoke_fence ON {table}')
    schema_editor.execute('DROP FUNCTION IF EXISTS blind_evidence_revoke_fence()')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0040_blind_reused_account_id')]
    operations = [migrations.RunPython(install, uninstall)]
