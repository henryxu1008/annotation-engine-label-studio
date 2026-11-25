from django.conf import settings
from django.db import migrations


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    user = q(apps.get_model(settings.AUTH_USER_MODEL)._meta.db_table)
    history = q(apps.get_model('classification_disagreements', 'BlindPrivilegeHistory')._meta.db_table)
    event = q(apps.get_model('classification_disagreements', 'BlindProvenanceEvent')._meta.db_table)
    fence = q(apps.get_model('classification_disagreements', 'BlindProvenanceFence')._meta.db_table)
    certificate = q(apps.get_model('classification_disagreements', 'BlindCaptureCertificate')._meta.db_table)
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)

    # Keep the old evidence when a numeric ID is reused. The replacement may
    # use the ordinary product, but cannot inherit blind eligibility from a
    # previous account generation. One trigger establishes the order without
    # relying on backend-specific ordering of separate AFTER INSERT triggers.
    if vendor == 'postgresql':
        schema_editor.execute(f'''CREATE OR REPLACE FUNCTION blind_capture_birth() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            INSERT INTO {certificate} (account_id, generation)
              SELECT NEW.account_id, generation FROM {epoch} WHERE id = 1 AND enabled = TRUE FOR SHARE
              ON CONFLICT (account_id) DO NOTHING;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'DROP TRIGGER blind_privilege_account ON {user}')
        schema_editor.execute(f'DROP TRIGGER blind_provenance_account ON {user}')
        schema_editor.execute(f'''CREATE TRIGGER blind_privilege_account AFTER UPDATE OF is_staff, is_superuser ON {user}
          FOR EACH ROW EXECUTE FUNCTION blind_privilege_account()''')
        schema_editor.execute(f'''
          CREATE FUNCTION blind_account_birth_v2() RETURNS trigger LANGUAGE plpgsql AS $$
          DECLARE ticket bigint; reused boolean;
          BEGIN
            SELECT EXISTS (SELECT 1 FROM {history} WHERE account_id = NEW.id)
                OR EXISTS (SELECT 1 FROM {event} WHERE kind = 'account' AND subject_id = NEW.id)
                OR EXISTS (SELECT 1 FROM {certificate} WHERE account_id = NEW.id) INTO reused;
            INSERT INTO {history} (account_id, elevated, had_assignment, first_assignment_id)
              VALUES (NEW.id, NEW.is_staff OR NEW.is_superuser OR reused, FALSE, NULL)
              ON CONFLICT (account_id) DO UPDATE SET elevated = TRUE;
            INSERT INTO {fence} (id, sequence)
              VALUES (1, (SELECT COALESCE(MAX(sequence), 0) FROM {event})) ON CONFLICT (id) DO NOTHING;
            UPDATE {fence} SET sequence = sequence + 1 WHERE id = 1 RETURNING sequence INTO ticket;
            IF ticket IS NULL THEN RAISE EXCEPTION 'blind provenance fence missing'; END IF;
            INSERT INTO {event} (kind, subject_id, sequence) VALUES ('account', NEW.id, ticket)
              ON CONFLICT (kind, subject_id) DO NOTHING;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_account_birth_v2 AFTER INSERT ON {user}
          FOR EACH ROW EXECUTE FUNCTION blind_account_birth_v2()''')
    else:
        schema_editor.execute('DROP TRIGGER blind_capture_birth')
        schema_editor.execute(f'''CREATE TRIGGER blind_capture_birth AFTER INSERT ON {history}
          BEGIN INSERT INTO {certificate} (account_id, generation)
            SELECT NEW.account_id, generation FROM {epoch} WHERE id = 1 AND enabled = TRUE
            ON CONFLICT (account_id) DO NOTHING; END''')
        schema_editor.execute('DROP TRIGGER blind_privilege_account_insert')
        schema_editor.execute('DROP TRIGGER blind_provenance_account')
        schema_editor.execute(f'''CREATE TRIGGER blind_account_birth_v2 AFTER INSERT ON {user}
          BEGIN
            INSERT INTO {history} (account_id, elevated, had_assignment, first_assignment_id)
              VALUES (NEW.id, NEW.is_staff OR NEW.is_superuser OR EXISTS
                (SELECT 1 FROM {event} WHERE kind = 'account' AND subject_id = NEW.id) OR EXISTS
                (SELECT 1 FROM {certificate} WHERE account_id = NEW.id), FALSE, NULL)
              ON CONFLICT (account_id) DO UPDATE SET elevated = TRUE;
            INSERT INTO {fence} (id, sequence)
              VALUES (1, (SELECT COALESCE(MAX(sequence), 0) FROM {event})) ON CONFLICT (id) DO NOTHING;
            UPDATE {fence} SET sequence = sequence + 1 WHERE id = 1;
            INSERT INTO {event} (kind, subject_id, sequence)
              VALUES ('account', NEW.id, (SELECT sequence FROM {fence} WHERE id = 1))
              ON CONFLICT (kind, subject_id) DO NOTHING;
          END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    user = q(apps.get_model(settings.AUTH_USER_MODEL)._meta.db_table)
    history = q(apps.get_model('classification_disagreements', 'BlindPrivilegeHistory')._meta.db_table)
    event = q(apps.get_model('classification_disagreements', 'BlindProvenanceEvent')._meta.db_table)
    fence = q(apps.get_model('classification_disagreements', 'BlindProvenanceFence')._meta.db_table)
    certificate = q(apps.get_model('classification_disagreements', 'BlindCaptureCertificate')._meta.db_table)
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    if vendor == 'postgresql':
        schema_editor.execute(f'''CREATE OR REPLACE FUNCTION blind_capture_birth() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            INSERT INTO {certificate} (account_id, generation)
              SELECT NEW.account_id, generation FROM {epoch} WHERE id = 1 AND enabled = TRUE FOR SHARE;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'DROP TRIGGER blind_account_birth_v2 ON {user}')
        schema_editor.execute('DROP FUNCTION blind_account_birth_v2()')
        schema_editor.execute(f'DROP TRIGGER blind_privilege_account ON {user}')
        schema_editor.execute(f'''CREATE TRIGGER blind_privilege_account AFTER INSERT OR UPDATE OF is_staff, is_superuser ON {user}
          FOR EACH ROW EXECUTE FUNCTION blind_privilege_account()''')
        schema_editor.execute(f'''CREATE TRIGGER blind_provenance_account AFTER INSERT ON {user}
          FOR EACH ROW EXECUTE FUNCTION blind_provenance_capture('account')''')
    else:
        schema_editor.execute('DROP TRIGGER blind_capture_birth')
        schema_editor.execute(f'''CREATE TRIGGER blind_capture_birth AFTER INSERT ON {history}
          BEGIN INSERT INTO {certificate} (account_id, generation)
            SELECT NEW.account_id, generation FROM {epoch} WHERE id = 1 AND enabled = TRUE; END''')
        schema_editor.execute('DROP TRIGGER blind_account_birth_v2')
        schema_editor.execute(f'''CREATE TRIGGER blind_provenance_account AFTER INSERT ON {user}
          BEGIN
            INSERT INTO {fence} (id, sequence)
              VALUES (1, (SELECT COALESCE(MAX(sequence), 0) FROM {event})) ON CONFLICT (id) DO NOTHING;
            UPDATE {fence} SET sequence = sequence + 1 WHERE id = 1;
            INSERT INTO {event} (kind, subject_id, sequence)
              VALUES ('account', NEW.id, (SELECT sequence FROM {fence} WHERE id = 1));
          END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_privilege_account_insert AFTER INSERT ON {user}
          BEGIN INSERT INTO {history} (account_id, elevated, had_assignment, first_assignment_id)
            VALUES (NEW.id, NEW.is_staff OR NEW.is_superuser, FALSE, NULL); END''')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0039_blind_label_exposure')]
    operations = [migrations.RunPython(install, uninstall)]
