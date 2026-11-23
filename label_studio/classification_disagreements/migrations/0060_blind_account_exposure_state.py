"""Use a common, retained account state for file and label exposure."""

from django.db import migrations, models


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    state = q(apps.get_model('classification_disagreements', 'BlindAccountExposureState')._meta.db_table)
    cert = q(apps.get_model('classification_disagreements', 'BlindCaptureCertificate')._meta.db_table)
    files = q(apps.get_model('classification_disagreements', 'BlindProjectFileExposure')._meta.db_table)
    labels = q(apps.get_model('classification_disagreements', 'BlindLabelExposure')._meta.db_table)
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    if vendor == 'postgresql':
        schema_editor.execute(f'LOCK TABLE {cert}, {files}, {labels} IN SHARE ROW EXCLUSIVE MODE')
    schema_editor.execute(
        f'''INSERT INTO {state} (account_id, generation, file_exposed, label_exposed)
      SELECT c.account_id, c.generation,
        EXISTS (SELECT 1 FROM {files} f WHERE f.account_id = c.account_id AND f.generation = c.generation),
        EXISTS (SELECT 1 FROM {labels} l WHERE l.account_id = c.account_id AND l.generation = c.generation)
      FROM {cert} c'''
    )
    if vendor == 'postgresql':
        # Direct certificate inserts must serialize with exposure inserts too,
        # not only certificates created through blind_capture_birth().
        schema_editor.execute(
            f'''CREATE FUNCTION blind_account_cert_fence() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN PERFORM id FROM {epoch} WHERE id = 1 FOR SHARE;
            IF NOT FOUND THEN RAISE EXCEPTION 'capture epoch is unavailable'; END IF;
            RETURN NEW; END $$'''
        )
        schema_editor.execute(
            f'''CREATE TRIGGER blind_account_cert_fence BEFORE INSERT ON {cert}
          FOR EACH ROW EXECUTE FUNCTION blind_account_cert_fence()'''
        )
        schema_editor.execute(
            f'''CREATE FUNCTION blind_account_exposure_state_guard() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'account exposure state is retained'; END IF;
            IF pg_trigger_depth() = 1 THEN RAISE EXCEPTION 'account exposure state is trigger managed'; END IF;
            IF TG_OP = 'UPDATE' AND (NEW.account_id <> OLD.account_id OR NEW.generation <> OLD.generation
              OR (OLD.file_exposed AND NOT NEW.file_exposed) OR (OLD.label_exposed AND NOT NEW.label_exposed))
            THEN RAISE EXCEPTION 'account exposure state is irreversible'; END IF;
            RETURN NEW;
          END $$'''
        )
        schema_editor.execute(
            f'''CREATE TRIGGER blind_account_exposure_state_guard BEFORE INSERT OR UPDATE OR DELETE ON {state}
          FOR EACH ROW EXECUTE FUNCTION blind_account_exposure_state_guard()'''
        )
        schema_editor.execute(
            f'''CREATE FUNCTION blind_account_exposure_state_create() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN INSERT INTO {state} (account_id, generation, file_exposed, label_exposed)
            VALUES (NEW.account_id, NEW.generation,
              EXISTS (SELECT 1 FROM {files} f WHERE f.account_id = NEW.account_id AND f.generation = NEW.generation),
              EXISTS (SELECT 1 FROM {labels} l WHERE l.account_id = NEW.account_id AND l.generation = NEW.generation));
            RETURN NEW; END $$'''
        )
        schema_editor.execute(
            f'''CREATE TRIGGER blind_account_exposure_state_create AFTER INSERT ON {cert}
          FOR EACH ROW EXECUTE FUNCTION blind_account_exposure_state_create()'''
        )
        schema_editor.execute(
            f'''CREATE FUNCTION blind_account_exposure_capture() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF TG_TABLE_NAME = '{apps.get_model('classification_disagreements', 'BlindProjectFileExposure')._meta.db_table}' THEN
              UPDATE {state} SET file_exposed = TRUE WHERE account_id = NEW.account_id AND generation = NEW.generation AND NOT file_exposed;
            ELSE
              UPDATE {state} SET label_exposed = TRUE WHERE account_id = NEW.account_id AND generation = NEW.generation AND NOT label_exposed;
            END IF;
            RETURN NEW;
          END $$'''
        )
        for name, table in (('blind_account_file_capture', files), ('blind_account_label_capture', labels)):
            schema_editor.execute(
                f'CREATE TRIGGER {name} AFTER INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION blind_account_exposure_capture()'
            )
        schema_editor.execute(f'DROP TRIGGER blind_read_file_revoke ON {files}')
        schema_editor.execute(f'DROP TRIGGER blind_read_label_revoke ON {labels}')
    else:
        schema_editor.execute(
            f'''CREATE TRIGGER blind_account_exposure_state_insert BEFORE INSERT ON {state}
          WHEN EXISTS (SELECT 1 FROM {state} WHERE account_id = NEW.account_id)
            OR NOT EXISTS (SELECT 1 FROM {cert} WHERE account_id = NEW.account_id AND generation = NEW.generation)
          BEGIN SELECT RAISE(ABORT, 'invalid account exposure state'); END'''
        )
        schema_editor.execute(
            f'''CREATE TRIGGER blind_account_exposure_state_update BEFORE UPDATE ON {state}
          WHEN NEW.account_id IS NOT OLD.account_id OR NEW.generation IS NOT OLD.generation
            OR (OLD.file_exposed AND NOT NEW.file_exposed) OR (OLD.label_exposed AND NOT NEW.label_exposed)
          BEGIN SELECT RAISE(ABORT, 'account exposure state is irreversible'); END'''
        )
        schema_editor.execute(
            f'''CREATE TRIGGER blind_account_exposure_state_delete BEFORE DELETE ON {state}
          BEGIN SELECT RAISE(ABORT, 'account exposure state is retained'); END'''
        )
        schema_editor.execute(
            f'''CREATE TRIGGER blind_account_exposure_state_create AFTER INSERT ON {cert}
          BEGIN INSERT INTO {state} (account_id, generation, file_exposed, label_exposed)
            VALUES (NEW.account_id, NEW.generation,
              EXISTS (SELECT 1 FROM {files} f WHERE f.account_id = NEW.account_id AND f.generation = NEW.generation),
              EXISTS (SELECT 1 FROM {labels} l WHERE l.account_id = NEW.account_id AND l.generation = NEW.generation)); END'''
        )
        for name, table, field in (
            ('blind_account_file_capture', files, 'file_exposed'),
            ('blind_account_label_capture', labels, 'label_exposed'),
        ):
            schema_editor.execute(
                f'''CREATE TRIGGER {name} AFTER INSERT ON {table}
              BEGIN UPDATE {state} SET {field} = TRUE WHERE account_id = NEW.account_id
                AND generation = NEW.generation AND NOT {field}; END'''
            )
        schema_editor.execute('DROP TRIGGER blind_read_file_revoke')
        schema_editor.execute('DROP TRIGGER blind_read_label_revoke')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    state = q(apps.get_model('classification_disagreements', 'BlindAccountExposureState')._meta.db_table)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f'SELECT 1 FROM {state} LIMIT 1')
        if cursor.fetchone():
            raise RuntimeError('Retained account exposure state cannot be discarded by rollback.')
    cert = q(apps.get_model('classification_disagreements', 'BlindCaptureCertificate')._meta.db_table)
    files = q(apps.get_model('classification_disagreements', 'BlindProjectFileExposure')._meta.db_table)
    labels = q(apps.get_model('classification_disagreements', 'BlindLabelExposure')._meta.db_table)
    if vendor == 'postgresql':
        for name, table, fn in (
            ('blind_account_cert_fence', cert, 'blind_account_cert_fence'),
            ('blind_account_exposure_state_guard', state, 'blind_account_exposure_state_guard'),
            ('blind_account_exposure_state_create', cert, 'blind_account_exposure_state_create'),
            ('blind_account_file_capture', files, None),
            ('blind_account_label_capture', labels, None),
        ):
            schema_editor.execute(f'DROP TRIGGER {name} ON {table}')
            if fn:
                schema_editor.execute(f'DROP FUNCTION {fn}()')
        schema_editor.execute('DROP FUNCTION blind_account_exposure_capture()')
        for name, table in (('blind_read_file_revoke', files), ('blind_read_label_revoke', labels)):
            schema_editor.execute(
                f'CREATE TRIGGER {name} AFTER INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION blind_read_evidence_revoke()'
            )
    else:
        for name in (
            'blind_account_exposure_state_insert',
            'blind_account_exposure_state_update',
            'blind_account_exposure_state_delete',
            'blind_account_exposure_state_create',
            'blind_account_file_capture',
            'blind_account_label_capture',
        ):
            schema_editor.execute(f'DROP TRIGGER {name}')
        binding = q(apps.get_model('classification_disagreements', 'BlindReadBinding')._meta.db_table)
        identity = q(apps.get_model('classification_disagreements', 'BlindReadIdentityState')._meta.db_table)
        for name, table in (('blind_read_file_revoke', files), ('blind_read_label_revoke', labels)):
            schema_editor.execute(
                f'''CREATE TRIGGER {name} AFTER INSERT ON {table}
              BEGIN UPDATE {identity} SET revoked = TRUE WHERE NOT revoked AND assignment_id IN
                (SELECT b.assignment_id_frozen FROM {binding} b JOIN {cert} c ON c.account_id = b.actor_id_frozen
                  WHERE b.purpose = 'quality' AND c.account_id = NEW.account_id AND c.generation = NEW.generation); END'''
            )


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0059_blind_read_reference_index')]
    operations = [
        migrations.CreateModel(
            name='BlindAccountExposureState',
            fields=[
                ('account_id', models.PositiveBigIntegerField(primary_key=True, serialize=False)),
                ('generation', models.PositiveBigIntegerField()),
                ('file_exposed', models.BooleanField(default=False)),
                ('label_exposed', models.BooleanField(default=False)),
            ],
        ),
        migrations.RunPython(install, uninstall),
    ]
