"""Additive database enforcement; SQLite half-state detection remains explicit."""
from django.db import migrations

TASK = 'review_batches_secondreviewtask'
DECISION = 'review_batches_secondreviewdecision'
TASK_FROZEN = (
    'id', 'project_id', 'organization_id_frozen', 'instance_id', 'review_batch_id_frozen', 'submission_id_frozen',
    'revision', 'assignee_id_frozen', 'actor_id_frozen', 'creator_principal', 'creator_source', 'created_at',
    'source_hash', 'first_review_hash', 'proof', 'receipt', 'receipt_hash', 'idempotency_key', 'request_hash',
)
DECISION_FROZEN = (
    'id', 'project_id', 'task_id_frozen', 'actor_id_frozen', 'creator_principal', 'creator_source', 'created_at',
    'source_hash', 'first_review_hash', 'outcome', 'reason', 'idempotency_key', 'request_hash', 'receipt', 'receipt_hash',
)


def cancel_predicate(vendor):
    """Structural cross-binding in SQL; canonical hashes are also checked on reads."""
    def get(path):
        return ("NEW.cancellation #>> '{" + path.replace('.', ',') + "}'") if vendor == 'postgresql' else f"json_extract(NEW.cancellation,'$.{path}')"
    def typed(path, kind):
        if vendor == 'postgresql':
            value = "NEW.cancellation #> '{" + path.replace('.', ',') + "}'"
            return f"jsonb_typeof({value})='{kind}'"
        sq = {'number': 'integer', 'string': 'text', 'object': 'object'}[kind]
        return f"json_type(NEW.cancellation,'$.{path}')='{sq}'"
    def hexadecimal(value):
        return f"{value} ~ '^[0-9a-f]{{64}}$'" if vendor == 'postgresql' else f"(length({value})=64 AND {value} NOT GLOB '*[^0-9a-f]*')"
    def uuid_equal(path, column):
        value = get(path)
        comparison = f'{value}=NEW.{column}::text' if vendor == 'postgresql' else f"(length({value})=36 AND substr({value},9,1)='-' AND substr({value},14,1)='-' AND substr({value},19,1)='-' AND substr({value},24,1)='-' AND replace({value},'-','')=NEW.{column})"
        return f"({typed(path, 'string')} AND {comparison})"
    def numeric_equal(path, column):
        value = get(path)
        return f"({typed(path, 'number')} AND {value}=NEW.{column}::text)" if vendor == 'postgresql' else f"({typed(path, 'number')} AND {value}=NEW.{column})"
    def count(path, length):
        if vendor == 'postgresql':
            target = "NEW.cancellation" if not path else "NEW.cancellation #> '{" + path.replace('.', ',') + "}'"
            return f'(SELECT count(*) FROM jsonb_object_keys({target}))={length}'
        suffix = '' if not path else ", '$." + path + "'"
        return f'(SELECT count(*) FROM json_each(NEW.cancellation{suffix}))={length}'
    def byte_limit(path, maximum):
        value = get(path)
        length = f"OCTET_LENGTH({value})" if vendor == 'postgresql' else f"length(CAST({value} AS BLOB))"
        return f"({typed(path, 'string')} AND {length} BETWEEN 1 AND {maximum})"
    checks = [
        "NEW.cancel_key IS NOT NULL", "NEW.cancelled_by_id IS NOT NULL", hexadecimal('NEW.cancellation_hash'),
        count('', 2), typed('body', 'object'), typed('receipt', 'object'), count('body', 3), count('receipt', 14),
        typed('receipt.owner', 'object'), count('receipt.owner', 3),
        f"{get('receipt.schema')}='second-review-command-receipt/v1'", f"{get('receipt.command')}='cancel'",
        f"{get('receipt.effect')}='supervisory_only'", uuid_equal('receipt.task_id', 'id'), uuid_equal('receipt.key', 'cancel_key'),
        uuid_equal('receipt.instance_id', 'instance_id'), numeric_equal('receipt.project_id', 'project_id'),
        numeric_equal('receipt.organization_id', 'organization_id_frozen'), numeric_equal('receipt.owner.actor_id', 'cancelled_by_id'),
        byte_limit('receipt.owner.principal', 512), byte_limit('receipt.owner.source', 128),
        typed('receipt.request_hash', 'string'), hexadecimal(get('receipt.request_hash')),
        typed('body.expected_viewer_hash', 'string'), hexadecimal(get('body.expected_viewer_hash')),
        typed('receipt.source_hash', 'string'), f"{get('receipt.source_hash')}=NEW.source_hash", byte_limit('receipt.reason', 8192),
        f"length({get('receipt.reason')})<=2000", f"length(trim({get('receipt.reason')}))>0",
        typed('body.reason', 'string'), f"{get('body.reason')}={get('receipt.reason')}", byte_limit('receipt.created_at', 40),
    ]
    for path, number in (('receipt.version', 2), ('body.expected_version', 1)):
        expected = f"'{number}'" if vendor == 'postgresql' else str(number)
        checks.extend([typed(path, 'number'), f'{get(path)}={expected}'])
    return ' AND '.join('(' + v + ')' for v in checks)


def changed(field, vendor):
    if vendor == 'postgresql':
        return f'OLD.{field}::text COLLATE "C" IS DISTINCT FROM NEW.{field}::text COLLATE "C"'
    return f'OLD.{field} IS NOT NEW.{field}'


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    for table, fields, nullable in (
        (TASK, TASK_FROZEN, ('review_batch_id', 'submission_id', 'assignee_id', 'created_by_id', 'first_review_id', 'first_reviewer_id', 'original_submitter_id')),
        (DECISION, DECISION_FROZEN, ('task_id', 'created_by_id')),
    ):
        guard = ' OR '.join([changed(f, vendor) for f in fields] + [f'(NEW.{f} IS NOT NULL AND ({changed(f, vendor)}))' for f in nullable])
        name = table + '_frozen'
        if vendor == 'postgresql':
            schema_editor.execute(f'''CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                IF {guard} THEN RAISE EXCEPTION 'Second review immutable field'; END IF; RETURN NEW; END $$;
                CREATE TRIGGER {name} BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION {name}();''')
        else:
            schema_editor.execute(f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} WHEN {guard} BEGIN SELECT RAISE(ABORT, 'Second review immutable field'); END;")
    if vendor == 'sqlite':
        # SQLite's UUID/varchar/datetime affinities do not enforce physical
        # widths. Primary keys are always selected by Django, even with defer.
        # Bound new immutable scalar storage at INSERT as well as SELECT gates.
        for table, uuids in ((TASK, ('id', 'instance_id', 'idempotency_key')), (DECISION, ('id', 'task_id_frozen', 'idempotency_key'))):
            valid = ' AND '.join(f"(typeof(NEW.{f})='text' AND length(NEW.{f})=32 AND NEW.{f} NOT GLOB '*[^0-9a-f]*')" for f in uuids)
            valid += " AND length(CAST(NEW.created_at AS BLOB))<=128 AND length(CAST(NEW.creator_principal AS BLOB)) BETWEEN 1 AND 512 AND length(CAST(NEW.creator_source AS BLOB)) BETWEEN 1 AND 128"
            integers = ('project_id', 'organization_id_frozen', 'review_batch_id_frozen', 'submission_id_frozen', 'revision', 'assignee_id_frozen', 'actor_id_frozen', 'version') if table == TASK else ('project_id', 'actor_id_frozen')
            valid += ' AND ' + ' AND '.join(f"(typeof(NEW.{f})='integer' AND NEW.{f}>0)" for f in integers)
            schema_editor.execute(f"CREATE TRIGGER {table}_scalar_insert BEFORE INSERT ON {table} WHEN NOT COALESCE(({valid}),0) BEGIN SELECT RAISE(ABORT, 'Second review invalid scalar'); END;")
        cancel_valid = cancel_predicate(vendor)
        schema_editor.execute(f'''CREATE TRIGGER second_review_transition BEFORE UPDATE ON {TASK} WHEN
            NOT COALESCE(((OLD.status=NEW.status AND OLD.version=NEW.version AND OLD.cancel_key IS NEW.cancel_key
                AND OLD.cancellation IS NEW.cancellation AND OLD.cancellation_hash=NEW.cancellation_hash
                AND (NEW.cancelled_by_id IS NULL OR OLD.cancelled_by_id IS NEW.cancelled_by_id))
              OR (OLD.status='pending' AND OLD.version=1 AND NEW.version=2 AND
                ((NEW.status='completed' AND NEW.cancel_key IS NULL AND NEW.cancellation IS NULL AND NEW.cancelled_by_id IS NULL
                    AND EXISTS(SELECT 1 FROM {DECISION} d WHERE d.task_id=NEW.id AND d.project_id=NEW.project_id
                        AND d.actor_id_frozen=NEW.assignee_id_frozen AND d.source_hash=NEW.source_hash AND d.first_review_hash=NEW.first_review_hash))
                OR (NEW.status='cancelled' AND COALESCE(({cancel_valid}),0)
                    AND NOT EXISTS(SELECT 1 FROM {DECISION} d WHERE d.task_id_frozen=NEW.id))))),0)
            BEGIN SELECT RAISE(ABORT, 'Second review invalid transition'); END;''')
        schema_editor.execute(f'''CREATE TRIGGER second_review_initial BEFORE INSERT ON {TASK} WHEN
            NEW.status != 'pending' OR NEW.version != 1 OR NEW.cancel_key IS NOT NULL OR NEW.cancellation IS NOT NULL
            OR NEW.cancelled_by_id IS NOT NULL OR NEW.cancellation_hash != ''
            BEGIN SELECT RAISE(ABORT, 'Second review invalid initial state'); END;''')
        schema_editor.execute(f'''CREATE TRIGGER second_review_opinion_insert BEFORE INSERT ON {DECISION} WHEN
            NOT EXISTS(SELECT 1 FROM {TASK} t WHERE t.id=NEW.task_id AND t.id=NEW.task_id_frozen AND t.project_id=NEW.project_id
                AND t.status='pending' AND t.version=1 AND t.assignee_id=NEW.created_by_id AND t.assignee_id_frozen=NEW.actor_id_frozen
                AND t.source_hash=NEW.source_hash AND t.first_review_hash=NEW.first_review_hash)
            BEGIN SELECT RAISE(ABORT, 'Second review invalid opinion'); END;''')
    else:
        schema_editor.execute(f'''CREATE FUNCTION second_review_transition() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF TG_OP='INSERT' THEN
                IF NEW.status!='pending' OR NEW.version!=1 OR NEW.cancel_key IS NOT NULL OR NEW.cancellation IS NOT NULL
                    OR NEW.cancelled_by_id IS NOT NULL OR NEW.cancellation_hash!='' THEN RAISE EXCEPTION 'Invalid initial state'; END IF;
            ELSIF NOT COALESCE(((OLD.status=NEW.status AND OLD.version=NEW.version AND OLD.cancel_key IS NOT DISTINCT FROM NEW.cancel_key
                AND OLD.cancellation::text COLLATE "C" IS NOT DISTINCT FROM NEW.cancellation::text COLLATE "C"
                AND OLD.cancellation_hash=NEW.cancellation_hash AND (NEW.cancelled_by_id IS NULL OR OLD.cancelled_by_id=NEW.cancelled_by_id))
                OR (OLD.status='pending' AND OLD.version=1 AND NEW.version=2 AND
                  ((NEW.status='completed' AND NEW.cancel_key IS NULL AND NEW.cancellation IS NULL AND NEW.cancelled_by_id IS NULL
                    AND EXISTS(SELECT 1 FROM {DECISION} d WHERE d.task_id=NEW.id AND d.project_id=NEW.project_id
                        AND d.actor_id_frozen=NEW.assignee_id_frozen AND d.source_hash=NEW.source_hash AND d.first_review_hash=NEW.first_review_hash))
                  OR (NEW.status='cancelled' AND {cancel_predicate(vendor)}
                    AND NOT EXISTS(SELECT 1 FROM {DECISION} d WHERE d.task_id_frozen=NEW.id))))),FALSE)
                THEN RAISE EXCEPTION 'Second review invalid transition'; END IF; RETURN NEW; END $$;
            CREATE TRIGGER second_review_transition BEFORE INSERT OR UPDATE ON {TASK} FOR EACH ROW EXECUTE FUNCTION second_review_transition();
            CREATE FUNCTION second_review_opinion_insert() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                IF NOT EXISTS(SELECT 1 FROM {TASK} t WHERE t.id=NEW.task_id AND t.id=NEW.task_id_frozen AND t.project_id=NEW.project_id
                    AND t.status='pending' AND t.version=1 AND t.assignee_id=NEW.created_by_id AND t.assignee_id_frozen=NEW.actor_id_frozen
                    AND t.source_hash=NEW.source_hash AND t.first_review_hash=NEW.first_review_hash)
                    THEN RAISE EXCEPTION 'Second review invalid opinion'; END IF; RETURN NEW; END $$;
            CREATE TRIGGER second_review_opinion_insert BEFORE INSERT ON {DECISION} FOR EACH ROW EXECUTE FUNCTION second_review_opinion_insert();
            CREATE FUNCTION second_review_consistent() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE target uuid; t {TASK}%%ROWTYPE; d {DECISION}%%ROWTYPE; BEGIN
                IF TG_TABLE_NAME='{TASK}' THEN target=NEW.id;
                ELSIF TG_OP='DELETE' THEN target=OLD.task_id_frozen; ELSE target=NEW.task_id_frozen; END IF;
                SELECT * INTO t FROM {TASK} WHERE id=target;
                IF NOT FOUND THEN RETURN NULL; END IF;
                SELECT * INTO d FROM {DECISION} WHERE task_id_frozen=target;
                IF (t.status='completed') IS DISTINCT FROM (d.id IS NOT NULL)
                    OR (d.id IS NOT NULL AND (d.task_id IS DISTINCT FROM t.id OR d.project_id!=t.project_id
                    OR d.source_hash!=t.source_hash OR d.first_review_hash!=t.first_review_hash OR d.actor_id_frozen!=t.assignee_id_frozen))
                    THEN RAISE EXCEPTION 'Second review inconsistent terminal state'; END IF; RETURN NULL; END $$;
            CREATE CONSTRAINT TRIGGER second_review_task_consistent AFTER INSERT OR UPDATE ON {TASK}
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION second_review_consistent();
            CREATE CONSTRAINT TRIGGER second_review_decision_consistent AFTER INSERT OR UPDATE OR DELETE ON {DECISION}
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION second_review_consistent();''')


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == 'sqlite':
        for name in (TASK + '_frozen', DECISION + '_frozen', TASK + '_scalar_insert', DECISION + '_scalar_insert', 'second_review_transition', 'second_review_initial', 'second_review_opinion_insert'):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}')
    elif schema_editor.connection.vendor == 'postgresql':
        for table, name in ((TASK, TASK + '_frozen'), (DECISION, DECISION + '_frozen'), (TASK, 'second_review_transition'),
                            (DECISION, 'second_review_opinion_insert'), (TASK, 'second_review_task_consistent'), (DECISION, 'second_review_decision_consistent')):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name} ON {table}')
        for name in (TASK + '_frozen', DECISION + '_frozen', 'second_review_transition', 'second_review_opinion_insert', 'second_review_consistent'):
            schema_editor.execute(f'DROP FUNCTION IF EXISTS {name}()')


class Migration(migrations.Migration):
    dependencies = [('review_batches', '0007_second_review')]
    operations = [migrations.RunPython(install, uninstall)]
