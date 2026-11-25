from django.db import migrations, models


EPOCH = 'classification_disagreements_blindcaptureepoch'
CERTIFICATE = 'classification_disagreements_blindcapturecertificate'
HISTORY = 'classification_disagreements_blindprivilegehistory'


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    epoch, certificate, history, migrations_table = map(q, (EPOCH, CERTIFICATE, HISTORY, 'django_migrations'))
    schema_editor.execute(f'INSERT INTO {epoch} (id, generation, enabled) VALUES (1, 1, FALSE)')
    if vendor == 'postgresql':
        schema_editor.execute(f'''
            CREATE FUNCTION blind_capture_birth() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              INSERT INTO {certificate} (account_id, generation)
                SELECT NEW.account_id, generation FROM {epoch} WHERE id = 1 AND enabled = TRUE FOR SHARE;
              RETURN NEW;
            END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_capture_birth AFTER INSERT ON {history}
            FOR EACH ROW EXECUTE FUNCTION blind_capture_birth()''')
        schema_editor.execute(f'''
            CREATE FUNCTION blind_capture_invalidate() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN UPDATE {epoch} SET enabled = FALSE WHERE id = 1; RETURN NULL; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_capture_migrations
            AFTER INSERT OR UPDATE OR DELETE ON {migrations_table}
            FOR EACH STATEMENT EXECUTE FUNCTION blind_capture_invalidate()''')
        schema_editor.execute('''
            CREATE FUNCTION blind_capture_epoch_forward() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.id <> OLD.id OR NEW.generation < OLD.generation OR
                (NEW.enabled AND NOT OLD.enabled AND NEW.generation <= OLD.generation) THEN
                RAISE EXCEPTION 'capture generation must advance before enabling';
              END IF;
              RETURN NEW;
            END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_capture_epoch_forward BEFORE UPDATE ON {epoch}
            FOR EACH ROW EXECUTE FUNCTION blind_capture_epoch_forward()''')
        schema_editor.execute(f'''
            CREATE FUNCTION blind_capture_epoch_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.enabled OR NEW.generation <= (SELECT COALESCE(MAX(generation), 0) FROM {certificate}) THEN
                RAISE EXCEPTION 'capture generation cannot reuse existing certificates';
              END IF;
              RETURN NEW;
            END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_capture_epoch_insert BEFORE INSERT ON {epoch}
            FOR EACH ROW EXECUTE FUNCTION blind_capture_epoch_insert()''')
        schema_editor.execute('''
            CREATE FUNCTION blind_capture_certificate_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'capture certificate is immutable'; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_capture_certificate_immutable BEFORE UPDATE ON {certificate}
            FOR EACH ROW EXECUTE FUNCTION blind_capture_certificate_immutable()''')
    else:
        schema_editor.execute(f'''CREATE TRIGGER blind_capture_birth AFTER INSERT ON {history}
            BEGIN INSERT INTO {certificate} (account_id, generation)
              SELECT NEW.account_id, generation FROM {epoch} WHERE id = 1 AND enabled = TRUE; END''')
        for suffix, op in (('insert', 'INSERT'), ('update', 'UPDATE'), ('delete', 'DELETE')):
            schema_editor.execute(f'''CREATE TRIGGER blind_capture_migrations_{suffix} AFTER {op} ON {migrations_table}
              BEGIN UPDATE {epoch} SET enabled = FALSE WHERE id = 1; END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_capture_epoch_forward BEFORE UPDATE ON {epoch}
            WHEN NEW.id IS NOT OLD.id OR NEW.generation < OLD.generation OR
              (NEW.enabled AND NOT OLD.enabled AND NEW.generation <= OLD.generation)
            BEGIN SELECT RAISE(ABORT, 'capture generation must advance before enabling'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_capture_epoch_insert BEFORE INSERT ON {epoch}
            WHEN NEW.enabled OR NEW.generation <= (SELECT COALESCE(MAX(generation), 0) FROM {certificate})
            BEGIN SELECT RAISE(ABORT, 'capture generation cannot reuse existing certificates'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_capture_certificate_immutable BEFORE UPDATE ON {certificate}
            BEGIN SELECT RAISE(ABORT, 'capture certificate is immutable'); END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'postgresql':
        for name, table in (('blind_capture_birth', HISTORY), ('blind_capture_migrations', 'django_migrations'),
                            ('blind_capture_epoch_forward', EPOCH),
                            ('blind_capture_epoch_insert', EPOCH),
                            ('blind_capture_certificate_immutable', CERTIFICATE)):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name} ON {schema_editor.quote_name(table)}')
        for name in ('blind_capture_birth', 'blind_capture_invalidate', 'blind_capture_epoch_forward',
                     'blind_capture_epoch_insert',
                     'blind_capture_certificate_immutable'):
            schema_editor.execute(f'DROP FUNCTION IF EXISTS {name}()')
    elif vendor == 'sqlite':
        for name in ('blind_capture_birth', 'blind_capture_migrations_insert', 'blind_capture_migrations_update',
                     'blind_capture_migrations_delete',
                     'blind_capture_epoch_forward', 'blind_capture_epoch_insert',
                     'blind_capture_certificate_immutable'):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0022_blind_privilege_history')]
    operations = [
        migrations.CreateModel(name='BlindCaptureEpoch', fields=[
            ('id', models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
            ('generation', models.PositiveBigIntegerField(default=1)),
            ('enabled', models.BooleanField(default=False)),
        ]),
        migrations.CreateModel(name='BlindCaptureCertificate', fields=[
            ('account_id', models.PositiveBigIntegerField(primary_key=True, serialize=False)),
            ('generation', models.PositiveBigIntegerField()),
        ]),
        migrations.RunPython(install, uninstall),
    ]
