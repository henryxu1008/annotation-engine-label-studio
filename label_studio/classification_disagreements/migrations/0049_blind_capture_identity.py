from django.conf import settings
from django.db import migrations, models


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    identity = q(apps.get_model('classification_disagreements', 'BlindCaptureIdentity')._meta.db_table)
    event = q(apps.get_model('classification_disagreements', 'BlindProvenanceEvent')._meta.db_table)
    history = q(apps.get_model('classification_disagreements', 'BlindPrivilegeHistory')._meta.db_table)
    certificate = q(apps.get_model('classification_disagreements', 'BlindCaptureCertificate')._meta.db_table)
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    user = q(apps.get_model(settings.AUTH_USER_MODEL)._meta.db_table)

    # A missing certificate is not proof that an account was never observed.
    # Preserve every known account identity before enabling the new guards.
    schema_editor.execute(f'UPDATE {epoch} SET enabled = FALSE WHERE id = 1')
    if vendor == 'postgresql':
        schema_editor.execute(f'LOCK TABLE {event}, {history}, {certificate} IN SHARE ROW EXCLUSIVE MODE')
    schema_editor.execute(f'''INSERT INTO {identity} (account_id)
        SELECT subject_id FROM {event} WHERE kind = 'account'
        UNION SELECT account_id FROM {history}
        UNION SELECT account_id FROM {certificate}
        UNION SELECT id FROM {user}''')

    if vendor == 'postgresql':
        schema_editor.execute('''CREATE FUNCTION blind_capture_identity_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN RAISE EXCEPTION 'blind capture identity is immutable'; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_capture_identity_immutable BEFORE UPDATE OR DELETE ON {identity}
          FOR EACH ROW EXECUTE FUNCTION blind_capture_identity_immutable()''')
        schema_editor.execute(f'''CREATE FUNCTION blind_capture_identity_certificate() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF EXISTS (SELECT 1 FROM {event} WHERE kind = 'account' AND subject_id = NEW.account_id) THEN
              RAISE EXCEPTION 'capture certificate must be issued at account birth';
            END IF;
            INSERT INTO {identity} (account_id) VALUES (NEW.account_id);
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_capture_identity_certificate BEFORE INSERT ON {certificate}
          FOR EACH ROW EXECUTE FUNCTION blind_capture_identity_certificate()''')
        schema_editor.execute(f'''CREATE FUNCTION blind_capture_identity_event() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF NEW.kind = 'account' THEN
              INSERT INTO {identity} (account_id) VALUES (NEW.subject_id) ON CONFLICT (account_id) DO NOTHING;
            END IF;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_capture_identity_event AFTER INSERT ON {event}
          FOR EACH ROW EXECUTE FUNCTION blind_capture_identity_event()''')
        schema_editor.execute(f'''CREATE OR REPLACE FUNCTION blind_capture_birth() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            INSERT INTO {certificate} (account_id, generation)
              SELECT NEW.account_id, generation FROM {epoch} WHERE id = 1 AND enabled = TRUE
                AND NOT EXISTS (SELECT 1 FROM {identity} WHERE account_id = NEW.account_id) FOR SHARE
              ON CONFLICT (account_id) DO NOTHING;
            RETURN NEW;
          END $$''')
    else:
        for operation in ('UPDATE', 'DELETE'):
            schema_editor.execute(f'''CREATE TRIGGER blind_capture_identity_{operation.lower()} BEFORE {operation} ON {identity}
              BEGIN SELECT RAISE(ABORT, 'blind capture identity is immutable'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_capture_identity_certificate BEFORE INSERT ON {certificate}
          BEGIN
            SELECT CASE WHEN EXISTS (SELECT 1 FROM {event} WHERE kind = 'account' AND subject_id = NEW.account_id)
              OR EXISTS (SELECT 1 FROM {identity} WHERE account_id = NEW.account_id)
              THEN RAISE(ABORT, 'capture certificate must be issued at account birth') END;
            INSERT INTO {identity} (account_id) VALUES (NEW.account_id);
          END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_capture_identity_event AFTER INSERT ON {event}
          WHEN NEW.kind = 'account' BEGIN
            INSERT INTO {identity} (account_id) VALUES (NEW.subject_id) ON CONFLICT (account_id) DO NOTHING;
          END''')
        schema_editor.execute('DROP TRIGGER blind_capture_birth')
        schema_editor.execute(f'''CREATE TRIGGER blind_capture_birth AFTER INSERT ON {history}
          BEGIN INSERT INTO {certificate} (account_id, generation)
            SELECT NEW.account_id, generation FROM {epoch} WHERE id = 1 AND enabled = TRUE
              AND NOT EXISTS (SELECT 1 FROM {identity} WHERE account_id = NEW.account_id)
            ON CONFLICT (account_id) DO NOTHING; END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    identity = q(apps.get_model('classification_disagreements', 'BlindCaptureIdentity')._meta.db_table)
    event = q(apps.get_model('classification_disagreements', 'BlindProvenanceEvent')._meta.db_table)
    history = q(apps.get_model('classification_disagreements', 'BlindPrivilegeHistory')._meta.db_table)
    certificate = q(apps.get_model('classification_disagreements', 'BlindCaptureCertificate')._meta.db_table)
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    user = q(apps.get_model(settings.AUTH_USER_MODEL)._meta.db_table)
    schema_editor.execute(f'UPDATE {epoch} SET enabled = FALSE WHERE id = 1')
    if vendor == 'postgresql':
        schema_editor.execute(f'LOCK TABLE {event}, {history}, {certificate}, {identity} IN SHARE ROW EXCLUSIVE MODE')
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f'''SELECT 1 FROM {identity} i WHERE
            NOT EXISTS (SELECT 1 FROM {event} e WHERE e.kind = 'account' AND e.subject_id = i.account_id)
            AND NOT EXISTS (SELECT 1 FROM {history} h WHERE h.account_id = i.account_id)
            AND NOT EXISTS (SELECT 1 FROM {certificate} c WHERE c.account_id = i.account_id)
            AND NOT EXISTS (SELECT 1 FROM {user} u WHERE u.id = i.account_id) LIMIT 1''')
        if cursor.fetchone():
            raise RuntimeError('Retained blind account identities cannot be discarded by rollback.')
    if vendor == 'postgresql':
        schema_editor.execute(f'DROP TRIGGER blind_capture_identity_certificate ON {certificate}')
        schema_editor.execute(f'DROP TRIGGER blind_capture_identity_event ON {event}')
        schema_editor.execute(f'DROP TRIGGER blind_capture_identity_immutable ON {identity}')
        for name in ('blind_capture_identity_certificate', 'blind_capture_identity_event',
                     'blind_capture_identity_immutable'):
            schema_editor.execute(f'DROP FUNCTION {name}()')
        schema_editor.execute(f'''CREATE OR REPLACE FUNCTION blind_capture_birth() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN INSERT INTO {certificate} (account_id, generation)
            SELECT NEW.account_id, generation FROM {epoch} WHERE id = 1 AND enabled = TRUE FOR SHARE
            ON CONFLICT (account_id) DO NOTHING; RETURN NEW; END $$''')
    else:
        for name in ('blind_capture_identity_certificate', 'blind_capture_identity_event',
                     'blind_capture_identity_update', 'blind_capture_identity_delete', 'blind_capture_birth'):
            schema_editor.execute(f'DROP TRIGGER {name}')
        schema_editor.execute(f'''CREATE TRIGGER blind_capture_birth AFTER INSERT ON {history}
          BEGIN INSERT INTO {certificate} (account_id, generation)
            SELECT NEW.account_id, generation FROM {epoch} WHERE id = 1 AND enabled = TRUE
            ON CONFLICT (account_id) DO NOTHING; END''')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0048_blind_read_attestation')]
    operations = [
        migrations.CreateModel(name='BlindCaptureIdentity', fields=[
            ('account_id', models.PositiveBigIntegerField(primary_key=True, serialize=False)),
        ]),
        migrations.RunPython(install, uninstall),
    ]
