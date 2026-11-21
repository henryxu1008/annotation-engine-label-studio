from django.db import migrations, models


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    table = q(apps.get_model('classification_disagreements', 'BlindEvidenceIdentity')._meta.db_table)
    gold = q(apps.get_model('classification_disagreements', 'ClassificationGoldEvidence')._meta.db_table)
    marker = q(apps.get_model('classification_disagreements', 'BlindQualityAssignment')._meta.db_table)
    provenance = q(apps.get_model('classification_disagreements', 'BlindProvenanceEvent')._meta.db_table)
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    # Previously issued proofs did not have this continuity guarantee. Do not
    # reuse the capture generation across the migration.
    schema_editor.execute(f'UPDATE {epoch} SET enabled = FALSE WHERE id = 1')
    if vendor == 'postgresql':
        # Keep the backfill and the insertion trigger atomic with respect to
        # concurrent evidence and provenance changes.
        schema_editor.execute(f'LOCK TABLE {gold}, {marker}, {provenance} IN SHARE ROW EXCLUSIVE MODE')
    schema_editor.execute(f'''INSERT INTO {table} (evidence_id)
        SELECT id FROM {gold}
        UNION SELECT reference_id FROM {marker} WHERE reference_id IS NOT NULL
        UNION SELECT subject_id FROM {provenance} WHERE kind = 'reference' ''')
    if vendor == 'postgresql':
        schema_editor.execute(f'''CREATE FUNCTION blind_evidence_identity_register() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN INSERT INTO {table} (evidence_id) VALUES (NEW.id); RETURN NEW; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_evidence_identity_register AFTER INSERT ON {gold}
            FOR EACH ROW EXECUTE FUNCTION blind_evidence_identity_register()''')
        schema_editor.execute('''CREATE FUNCTION blind_evidence_identity_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'blind evidence identity is immutable'; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_evidence_identity_immutable BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION blind_evidence_identity_immutable()''')
    else:
        schema_editor.execute(f'''CREATE TRIGGER blind_evidence_identity_register AFTER INSERT ON {gold}
            BEGIN INSERT INTO {table} (evidence_id) VALUES (NEW.id); END''')
        for operation in ('UPDATE', 'DELETE'):
            schema_editor.execute(f'''CREATE TRIGGER blind_evidence_identity_{operation.lower()}
                BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT, 'blind evidence identity is immutable'); END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    gold = q(apps.get_model('classification_disagreements', 'ClassificationGoldEvidence')._meta.db_table)
    table = q(apps.get_model('classification_disagreements', 'BlindEvidenceIdentity')._meta.db_table)
    schema_editor.execute(f'UPDATE {epoch} SET enabled = FALSE WHERE id = 1')
    if vendor == 'postgresql':
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_evidence_identity_register ON {gold}')
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_evidence_identity_immutable ON {table}')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_evidence_identity_register()')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_evidence_identity_immutable()')
    else:
        for name in ('blind_evidence_identity_register', 'blind_evidence_identity_update',
                     'blind_evidence_identity_delete'):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0043_blind_exposure_row_fence')]
    operations = [
        migrations.CreateModel(name='BlindEvidenceIdentity', fields=[
            ('evidence_id', models.PositiveBigIntegerField(primary_key=True, serialize=False)),
        ]),
        migrations.RunPython(install, uninstall),
    ]
