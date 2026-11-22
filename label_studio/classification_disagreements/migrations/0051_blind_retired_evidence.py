from django.conf import settings
from django.db import migrations, models


def _birth_trigger(apps, schema_editor, *, retired_guard):
    q = schema_editor.quote_name
    user = q(apps.get_model(settings.AUTH_USER_MODEL)._meta.db_table)
    history = q(apps.get_model('classification_disagreements', 'BlindPrivilegeHistory')._meta.db_table)
    event = q(apps.get_model('classification_disagreements', 'BlindProvenanceEvent')._meta.db_table)
    fence = q(apps.get_model('classification_disagreements', 'BlindProvenanceFence')._meta.db_table)
    certificate = q(apps.get_model('classification_disagreements', 'BlindCaptureCertificate')._meta.db_table)
    identity = q(apps.get_model('classification_disagreements', 'BlindCaptureIdentity')._meta.db_table)
    retired = q(apps.get_model('classification_disagreements', 'BlindRetiredEvidence')._meta.db_table)
    extra = f' OR EXISTS (SELECT 1 FROM {identity} WHERE account_id = NEW.id)' if retired_guard else ''
    event_filter = (f"WHERE NOT EXISTS (SELECT 1 FROM {retired} WHERE kind = 'account' AND subject_id = NEW.id AND retired = TRUE)"
                    if retired_guard else 'WHERE 1 = 1')
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(f'''CREATE OR REPLACE FUNCTION blind_account_birth_v2() RETURNS trigger LANGUAGE plpgsql AS $$
          DECLARE ticket bigint; reused boolean;
          BEGIN
            SELECT EXISTS (SELECT 1 FROM {history} WHERE account_id = NEW.id)
                OR EXISTS (SELECT 1 FROM {event} WHERE kind = 'account' AND subject_id = NEW.id)
                OR EXISTS (SELECT 1 FROM {certificate} WHERE account_id = NEW.id){extra} INTO reused;
            INSERT INTO {history} (account_id, elevated, had_assignment, first_assignment_id)
              VALUES (NEW.id, NEW.is_staff OR NEW.is_superuser OR reused, FALSE, NULL)
              ON CONFLICT (account_id) DO UPDATE SET elevated = TRUE;
            INSERT INTO {fence} (id, sequence)
              VALUES (1, (SELECT COALESCE(MAX(sequence), 0) FROM {event})) ON CONFLICT (id) DO NOTHING;
            UPDATE {fence} SET sequence = sequence + 1 WHERE id = 1 RETURNING sequence INTO ticket;
            IF ticket IS NULL THEN RAISE EXCEPTION 'blind provenance fence missing'; END IF;
            INSERT INTO {event} (kind, subject_id, sequence)
              SELECT 'account', NEW.id, ticket {event_filter} ON CONFLICT (kind, subject_id) DO NOTHING;
            RETURN NEW;
          END $$''')
    else:
        schema_editor.execute('DROP TRIGGER blind_account_birth_v2')
        schema_editor.execute(f'''CREATE TRIGGER blind_account_birth_v2 AFTER INSERT ON {user}
          BEGIN
            INSERT INTO {history} (account_id, elevated, had_assignment, first_assignment_id)
              VALUES (NEW.id, NEW.is_staff OR NEW.is_superuser OR EXISTS
                (SELECT 1 FROM {event} WHERE kind = 'account' AND subject_id = NEW.id) OR EXISTS
                (SELECT 1 FROM {certificate} WHERE account_id = NEW.id){extra}, FALSE, NULL)
              ON CONFLICT (account_id) DO UPDATE SET elevated = TRUE;
            INSERT INTO {fence} (id, sequence)
              VALUES (1, (SELECT COALESCE(MAX(sequence), 0) FROM {event})) ON CONFLICT (id) DO NOTHING;
            UPDATE {fence} SET sequence = sequence + 1 WHERE id = 1;
            INSERT INTO {event} (kind, subject_id, sequence)
              SELECT 'account', NEW.id, (SELECT sequence FROM {fence} WHERE id = 1) {event_filter}
              ON CONFLICT (kind, subject_id) DO NOTHING;
          END''')


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    retired = q(apps.get_model('classification_disagreements', 'BlindRetiredEvidence')._meta.db_table)
    event = q(apps.get_model('classification_disagreements', 'BlindProvenanceEvent')._meta.db_table)
    history = q(apps.get_model('classification_disagreements', 'BlindPrivilegeHistory')._meta.db_table)
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    schema_editor.execute(f'UPDATE {epoch} SET enabled = FALSE WHERE id = 1')
    if vendor == 'postgresql':
        schema_editor.execute(f'LOCK TABLE {event}, {history} IN SHARE ROW EXCLUSIVE MODE')
    schema_editor.execute(f'''INSERT INTO {retired} (kind, subject_id, retired)
      SELECT kind, subject_id, FALSE FROM {event}
      UNION SELECT 'privilege', account_id, FALSE FROM {history}''')
    if vendor == 'postgresql':
        schema_editor.execute('''CREATE FUNCTION blind_retired_evidence_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'retired blind evidence is immutable'; END IF;
            IF NEW.id <> OLD.id OR NEW.kind <> OLD.kind OR NEW.subject_id <> OLD.subject_id
               OR (OLD.retired AND NOT NEW.retired) THEN
              RAISE EXCEPTION 'retired blind evidence is immutable'; END IF;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_retired_evidence_immutable BEFORE UPDATE OR DELETE ON {retired}
          FOR EACH ROW EXECUTE FUNCTION blind_retired_evidence_immutable()''')
        schema_editor.execute(f'''CREATE FUNCTION blind_retire_event() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN PERFORM id FROM {epoch} WHERE id = 1 FOR UPDATE;
            INSERT INTO {retired} (kind, subject_id, retired) VALUES (OLD.kind, OLD.subject_id, TRUE)
              ON CONFLICT (kind, subject_id) DO UPDATE SET retired = TRUE;
            RETURN OLD; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_retire_event BEFORE DELETE ON {event}
          FOR EACH ROW EXECUTE FUNCTION blind_retire_event()''')
        schema_editor.execute(f'''CREATE FUNCTION blind_retire_privilege() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN PERFORM id FROM {epoch} WHERE id = 1 FOR UPDATE;
            INSERT INTO {retired} (kind, subject_id, retired) VALUES ('privilege', OLD.account_id, TRUE)
              ON CONFLICT (kind, subject_id) DO UPDATE SET retired = TRUE;
            RETURN OLD; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_retire_privilege BEFORE DELETE ON {history}
          FOR EACH ROW EXECUTE FUNCTION blind_retire_privilege()''')
        schema_editor.execute(f'''CREATE FUNCTION blind_retired_event_guard() RETURNS trigger LANGUAGE plpgsql AS $$
          DECLARE was_retired boolean;
          BEGIN
            INSERT INTO {retired} (kind, subject_id, retired) VALUES (NEW.kind, NEW.subject_id, FALSE)
              ON CONFLICT (kind, subject_id) DO UPDATE SET subject_id = EXCLUDED.subject_id
              RETURNING retired INTO was_retired;
            IF was_retired THEN RAISE EXCEPTION 'retired provenance cannot be replayed'; END IF;
            RETURN NEW; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_retired_event_guard BEFORE INSERT ON {event}
          FOR EACH ROW EXECUTE FUNCTION blind_retired_event_guard()''')
        schema_editor.execute(f'''CREATE FUNCTION blind_retired_privilege_guard() RETURNS trigger LANGUAGE plpgsql AS $$
          DECLARE was_retired boolean;
          BEGIN
            INSERT INTO {retired} (kind, subject_id, retired) VALUES ('privilege', NEW.account_id, FALSE)
              ON CONFLICT (kind, subject_id) DO UPDATE SET subject_id = EXCLUDED.subject_id
              RETURNING retired INTO was_retired;
            IF was_retired AND NOT NEW.elevated THEN
              RAISE EXCEPTION 'retired privilege history cannot be replayed'; END IF;
            RETURN NEW; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_retired_privilege_guard BEFORE INSERT ON {history}
          FOR EACH ROW EXECUTE FUNCTION blind_retired_privilege_guard()''')
    else:
        schema_editor.execute(f'''CREATE TRIGGER blind_retired_evidence_insert_guard BEFORE INSERT ON {retired}
          WHEN EXISTS (SELECT 1 FROM {retired} WHERE (kind = NEW.kind AND subject_id = NEW.subject_id)
                       OR (NEW.id <> -1 AND id = NEW.id))
          BEGIN SELECT RAISE(ABORT, 'retired blind evidence cannot be replaced'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_retired_evidence_update BEFORE UPDATE ON {retired}
          WHEN NEW.id IS NOT OLD.id OR NEW.kind IS NOT OLD.kind OR NEW.subject_id IS NOT OLD.subject_id
               OR (OLD.retired AND NOT NEW.retired)
          BEGIN SELECT RAISE(ABORT, 'retired blind evidence is immutable'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_retired_evidence_delete BEFORE DELETE ON {retired}
          BEGIN SELECT RAISE(ABORT, 'retired blind evidence is immutable'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_retire_event BEFORE DELETE ON {event} BEGIN
          INSERT INTO {retired} (kind, subject_id, retired) SELECT OLD.kind, OLD.subject_id, TRUE
            WHERE NOT EXISTS (SELECT 1 FROM {retired} WHERE kind = OLD.kind AND subject_id = OLD.subject_id);
          UPDATE {retired} SET retired = TRUE WHERE kind = OLD.kind AND subject_id = OLD.subject_id; END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_retire_privilege BEFORE DELETE ON {history} BEGIN
          INSERT INTO {retired} (kind, subject_id, retired) SELECT 'privilege', OLD.account_id, TRUE
            WHERE NOT EXISTS (SELECT 1 FROM {retired} WHERE kind = 'privilege' AND subject_id = OLD.account_id);
          UPDATE {retired} SET retired = TRUE WHERE kind = 'privilege' AND subject_id = OLD.account_id; END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_retired_event_guard BEFORE INSERT ON {event} BEGIN
          INSERT INTO {retired} (kind, subject_id, retired) SELECT NEW.kind, NEW.subject_id, FALSE
            WHERE NOT EXISTS (SELECT 1 FROM {retired} WHERE kind = NEW.kind AND subject_id = NEW.subject_id);
          SELECT CASE WHEN EXISTS (SELECT 1 FROM {retired}
            WHERE kind = NEW.kind AND subject_id = NEW.subject_id AND retired = TRUE)
            THEN RAISE(ABORT, 'retired provenance cannot be replayed') END; END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_retired_privilege_guard BEFORE INSERT ON {history} BEGIN
          INSERT INTO {retired} (kind, subject_id, retired) SELECT 'privilege', NEW.account_id, FALSE
            WHERE NOT EXISTS (SELECT 1 FROM {retired} WHERE kind = 'privilege' AND subject_id = NEW.account_id);
          SELECT CASE WHEN NOT NEW.elevated AND EXISTS (SELECT 1 FROM {retired}
            WHERE kind = 'privilege' AND subject_id = NEW.account_id AND retired = TRUE)
            THEN RAISE(ABORT, 'retired privilege history cannot be replayed') END; END''')
    _birth_trigger(apps, schema_editor, retired_guard=True)


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    retired = q(apps.get_model('classification_disagreements', 'BlindRetiredEvidence')._meta.db_table)
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    schema_editor.execute(f'UPDATE {epoch} SET enabled = FALSE WHERE id = 1')
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f'SELECT 1 FROM {retired} WHERE retired = TRUE LIMIT 1')
        if cursor.fetchone():
            raise RuntimeError('Retired blind evidence cannot be discarded by rollback.')
    _birth_trigger(apps, schema_editor, retired_guard=False)
    event = q(apps.get_model('classification_disagreements', 'BlindProvenanceEvent')._meta.db_table)
    history = q(apps.get_model('classification_disagreements', 'BlindPrivilegeHistory')._meta.db_table)
    if vendor == 'postgresql':
        for name, table in (('blind_retire_event', event), ('blind_retire_privilege', history),
                            ('blind_retired_event_guard', event), ('blind_retired_privilege_guard', history),
                            ('blind_retired_evidence_immutable', retired)):
            schema_editor.execute(f'DROP TRIGGER {name} ON {table}')
            schema_editor.execute(f'DROP FUNCTION {name}()')
    else:
        for name in ('blind_retire_event', 'blind_retire_privilege', 'blind_retired_event_guard',
                     'blind_retired_privilege_guard', 'blind_retired_evidence_insert_guard',
                     'blind_retired_evidence_update', 'blind_retired_evidence_delete'):
            schema_editor.execute(f'DROP TRIGGER {name}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0050_blind_completion_submission_fence')]
    operations = [
        migrations.CreateModel(name='BlindRetiredEvidence', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('kind', models.CharField(max_length=24)),
            ('subject_id', models.PositiveBigIntegerField()),
            ('retired', models.BooleanField(default=False)),
        ], options={'constraints': [
            models.UniqueConstraint(fields=('kind', 'subject_id'), name='blind_retired_evidence_unique'),
            models.CheckConstraint(condition=models.Q(kind__in=['account', 'reference', 'privilege']),
                                   name='blind_retired_evidence_kind_valid'),
        ]}),
        migrations.RunPython(install, uninstall),
    ]
