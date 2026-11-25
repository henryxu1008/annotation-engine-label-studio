"""Retain PostgreSQL privilege invalidation for shared participant reads."""

from django.db import migrations, models


def install(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    q = schema_editor.quote_name
    history = q(apps.get_model('classification_disagreements', 'BlindPrivilegeHistory')._meta.db_table)
    state = q(apps.get_model('classification_disagreements', 'BlindPrivilegeReadState')._meta.db_table)
    # Lock history before the snapshot and trigger installation; its writers
    # never need to wait for a capture-epoch lock in these triggers.
    schema_editor.execute(f'LOCK TABLE {history}, {state} IN SHARE ROW EXCLUSIVE MODE')
    schema_editor.execute(
        f'''INSERT INTO {state} (account_id, invalidated)
        SELECT account_id, elevated FROM {history}'''
    )
    schema_editor.execute(
        '''CREATE FUNCTION blind_privilege_read_state_guard() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'privilege read state is retained'; END IF;
        IF pg_trigger_depth() = 1 THEN RAISE EXCEPTION 'privilege read state is trigger managed'; END IF;
        IF TG_OP = 'UPDATE' AND (NEW.account_id <> OLD.account_id OR (OLD.invalidated AND NOT NEW.invalidated))
        THEN RAISE EXCEPTION 'privilege read state is irreversible'; END IF;
        RETURN NEW;
      END $$'''
    )
    schema_editor.execute(
        f'''CREATE TRIGGER blind_privilege_read_state_guard BEFORE INSERT OR UPDATE OR DELETE ON {state}
      FOR EACH ROW EXECUTE FUNCTION blind_privilege_read_state_guard()'''
    )
    schema_editor.execute(
        f'''CREATE FUNCTION blind_privilege_read_state_create() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN INSERT INTO {state} (account_id, invalidated) VALUES (NEW.account_id, NEW.elevated)
        ON CONFLICT (account_id) DO NOTHING; RETURN NEW; END $$'''
    )
    schema_editor.execute(
        f'''CREATE TRIGGER blind_privilege_read_state_create AFTER INSERT ON {history}
      FOR EACH ROW EXECUTE FUNCTION blind_privilege_read_state_create()'''
    )
    schema_editor.execute(
        f'''CREATE FUNCTION blind_privilege_read_state_invalidate() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP = 'DELETE' THEN
          UPDATE {state} SET invalidated = TRUE WHERE account_id = OLD.account_id AND NOT invalidated;
          RETURN OLD;
        END IF;
        IF NEW.elevated AND NOT OLD.elevated THEN
          UPDATE {state} SET invalidated = TRUE WHERE account_id = OLD.account_id AND NOT invalidated;
        END IF;
        RETURN NEW;
      END $$'''
    )
    # Existing deletion fences lock the epoch first. PostgreSQL orders same-
    # kind triggers by name, so take the state row only after those fences.
    schema_editor.execute(
        f'''CREATE TRIGGER blind_zz_privilege_read_state_invalidate BEFORE UPDATE OR DELETE ON {history}
      FOR EACH ROW EXECUTE FUNCTION blind_privilege_read_state_invalidate()'''
    )


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    q = schema_editor.quote_name
    history = q(apps.get_model('classification_disagreements', 'BlindPrivilegeHistory')._meta.db_table)
    state = q(apps.get_model('classification_disagreements', 'BlindPrivilegeReadState')._meta.db_table)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f'SELECT 1 FROM {state} LIMIT 1')
        if cursor.fetchone():
            raise RuntimeError('Retained privilege read state cannot be discarded by rollback.')
    for name, table in (
        ('blind_privilege_read_state_guard', state),
        ('blind_privilege_read_state_create', history),
        ('blind_zz_privilege_read_state_invalidate', history),
    ):
        schema_editor.execute(f'DROP TRIGGER {name} ON {table}')
    for name in (
        'blind_privilege_read_state_guard',
        'blind_privilege_read_state_create',
        'blind_privilege_read_state_invalidate',
    ):
        schema_editor.execute(f'DROP FUNCTION {name}()')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0061_blind_provenance_sequence_backfill')]
    operations = [
        migrations.CreateModel(
            name='BlindPrivilegeReadState',
            fields=[
                ('account_id', models.PositiveBigIntegerField(primary_key=True, serialize=False)),
                ('invalidated', models.BooleanField(default=False)),
            ],
        ),
        migrations.RunPython(install, uninstall),
    ]
