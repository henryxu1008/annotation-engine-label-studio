"""Immutable dispositions and a database-owned project cascade, on both DBs."""

from importlib import import_module

from django.db import migrations

TABLE = 'review_batches_secondreviewconcernfollowup'
PROJECT = 'project'
TASK = 'review_batches_secondreviewtask'
DECISION = 'review_batches_secondreviewdecision'
NAME = 'concern_followup'
FROZEN = (
    'id',
    'project_id',
    'organization_id_frozen',
    'instance_id',
    'task_id_frozen',
    'opinion_id_frozen',
    'opinion_created_at',
    'actor_id_frozen',
    'creator_principal',
    'creator_source',
    'created_at',
    'concern_hash',
    'origin_ref',
    'disposition',
    'reason',
    'related_submission_id_frozen',
    'target_ref',
    'version',
    'idempotency_key',
    'request_hash',
    'receipt',
    'receipt_hash',
)
NULLABLE = ('second_review_task_id', 'opinion_id', 'created_by_id', 'related_submission_id')


def equal_json(left, right, vendor):
    if vendor == 'postgresql':
        return f'({left})::text COLLATE "C" = ({right})::text COLLATE "C"'
    # Object key order is not a protocol identity; array order and numeric
    # storage types are. SQLite JSON tree paths preserve both without decoding
    # payloads into Python or normalizing 1.0 to the integer 1.
    a = f'SELECT fullkey,type,atom FROM json_tree({left})'
    b = f'SELECT fullkey,type,atom FROM json_tree({right})'
    return f'(NOT EXISTS({a} EXCEPT {b}) AND NOT EXISTS({b} EXCEPT {a}))'


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('sqlite', 'postgresql'):
        return
    changed = import_module('review_batches.migrations.0008_second_review_guards').changed
    guard = ' OR '.join(
        [changed(f, vendor) for f in FROZEN] + [f'(NEW.{f} IS NOT NULL AND ({changed(f, vendor)}))' for f in NULLABLE]
    )
    source = "NEW.origin_ref->'source'" if vendor == 'postgresql' else "json_extract(NEW.origin_ref,'$.source')"
    opinion = "NEW.origin_ref->'opinion'" if vendor == 'postgresql' else "json_extract(NEW.origin_ref,'$.opinion')"
    dispatch_hash = (
        "NEW.origin_ref->>'dispatch_receipt_hash'"
        if vendor == 'postgresql'
        else "json_extract(NEW.origin_ref,'$.dispatch_receipt_hash')"
    )
    opinion_hash = (
        "NEW.origin_ref->>'opinion_receipt_hash'"
        if vendor == 'postgresql'
        else "json_extract(NEW.origin_ref,'$.opinion_receipt_hash')"
    )
    valid = f'''EXISTS(SELECT 1 FROM {TASK} t JOIN {DECISION} d ON d.task_id=t.id
        WHERE t.id=NEW.second_review_task_id AND t.id=NEW.task_id_frozen
          AND d.id=NEW.opinion_id AND d.id=NEW.opinion_id_frozen
          AND t.project_id=NEW.project_id AND d.project_id=NEW.project_id
          AND t.organization_id_frozen=NEW.organization_id_frozen AND t.instance_id=NEW.instance_id
          AND t.status='completed' AND t.version=2 AND d.outcome='concern'
          AND d.actor_id_frozen<>NEW.actor_id_frozen AND d.created_at=NEW.opinion_created_at
          AND {equal_json(source, 't.proof', vendor)} AND {equal_json(opinion, 'd.receipt', vendor)}
          AND {dispatch_hash}=t.receipt_hash AND {opinion_hash}=d.receipt_hash)
        AND NEW.created_by_id=NEW.actor_id_frozen AND NEW.version=1
        AND ((NEW.disposition='no_change' AND NEW.related_submission_id IS NULL
              AND NEW.related_submission_id_frozen IS NULL AND NEW.target_ref IS NULL)
          OR (NEW.disposition='revision_linked' AND NEW.related_submission_id=NEW.related_submission_id_frozen
              AND NEW.related_submission_id_frozen>0 AND NEW.target_ref IS NOT NULL))'''
    measure = 'OCTET_LENGTH(CAST({} AS TEXT))' if vendor == 'postgresql' else 'length(CAST({} AS BLOB))'
    stored = '+'.join(
        'COALESCE(' + measure.format('NEW.' + f) + ',0)' for f in ('origin_ref', 'target_ref', 'receipt', 'reason')
    )
    if vendor == 'sqlite':
        # SQLite json_extract selects the first duplicate object key, whereas
        # Python's JSON decoder selects the last. Reject that ambiguity in the
        # bounded stored envelope before trusting any extracted binding.
        for field in ('origin_ref', 'target_ref', 'receipt'):
            valid = f'''(NOT EXISTS(SELECT 1 FROM json_tree(NEW.{field})
                WHERE parent IS NOT NULL GROUP BY parent,key COLLATE BINARY HAVING COUNT(*)>1) AND ({valid}))'''
    valid = (
        f'CASE WHEN ({stored}) <= 131072 THEN ({valid}) ELSE ' + ('FALSE' if vendor == 'postgresql' else '0') + ' END'
    )
    if vendor == 'sqlite':
        for f in ('id', 'instance_id', 'task_id_frozen', 'opinion_id_frozen', 'idempotency_key'):
            valid += f" AND typeof(NEW.{f})='text' AND length(NEW.{f})=32 AND NEW.{f} NOT GLOB '*[^0-9a-f]*'"
        for f in ('project_id', 'organization_id_frozen', 'actor_id_frozen', 'version'):
            valid += f" AND typeof(NEW.{f})='integer' AND NEW.{f}>0"
        valid += (
            " AND (NEW.related_submission_id_frozen IS NULL OR typeof(NEW.related_submission_id_frozen)='integer')"
        )
        for f, maximum in (
            ('creator_principal', 512),
            ('creator_source', 128),
            ('created_at', 128),
            ('opinion_created_at', 128),
            ('reason', 8192),
        ):
            valid += f" AND typeof(NEW.{f})='text' AND length(CAST(NEW.{f} AS BLOB)) BETWEEN 1 AND {maximum}"
        valid += " AND length(NEW.reason)<=2000 AND length(trim(NEW.reason))>0 AND instr(NEW.reason,char(0))=0"
        schema_editor.execute(
            f"CREATE TRIGGER {NAME}_frozen BEFORE UPDATE ON {TABLE} WHEN {guard} BEGIN SELECT RAISE(ABORT, 'Concern followup is immutable'); END;"
        )
        schema_editor.execute(
            f"CREATE TRIGGER {NAME}_initial BEFORE INSERT ON {TABLE} WHEN NOT COALESCE(({valid}),0) BEGIN SELECT RAISE(ABORT, 'Invalid concern followup'); END;"
        )
        schema_editor.execute(
            f"CREATE TRIGGER {NAME}_delete BEFORE DELETE ON {TABLE} WHEN EXISTS(SELECT 1 FROM {PROJECT} WHERE id=OLD.project_id) BEGIN SELECT RAISE(ABORT, 'Concern followup requires project deletion'); END;"
        )
        schema_editor.execute(
            f"CREATE TRIGGER {NAME}_project_cleanup AFTER DELETE ON {PROJECT} BEGIN DELETE FROM {TABLE} WHERE project_id=OLD.id; END;"
        )
    else:
        schema_editor.execute(
            f'''CREATE FUNCTION {NAME}_frozen() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF {guard} THEN RAISE EXCEPTION 'Concern followup is immutable'; END IF; RETURN NEW; END $$;
            CREATE TRIGGER {NAME}_frozen BEFORE UPDATE ON {TABLE} FOR EACH ROW EXECUTE FUNCTION {NAME}_frozen();
            CREATE FUNCTION {NAME}_initial() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                IF NOT COALESCE(({valid}),FALSE) THEN RAISE EXCEPTION 'Invalid concern followup'; END IF; RETURN NEW; END $$;
            CREATE TRIGGER {NAME}_initial BEFORE INSERT ON {TABLE} FOR EACH ROW EXECUTE FUNCTION {NAME}_initial();
            CREATE FUNCTION {NAME}_delete() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                IF EXISTS(SELECT 1 FROM {PROJECT} WHERE id=OLD.project_id) THEN RAISE EXCEPTION 'Concern followup requires project deletion'; END IF; RETURN OLD; END $$;
            CREATE TRIGGER {NAME}_delete BEFORE DELETE ON {TABLE} FOR EACH ROW EXECUTE FUNCTION {NAME}_delete();
            CREATE FUNCTION {NAME}_project_cleanup() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                DELETE FROM {TABLE} WHERE project_id=OLD.id; RETURN OLD; END $$;
            CREATE TRIGGER {NAME}_project_cleanup AFTER DELETE ON {PROJECT} FOR EACH ROW EXECUTE FUNCTION {NAME}_project_cleanup();'''
        )


def uninstall(apps, schema_editor):
    for suffix, table in (('project_cleanup', PROJECT), ('delete', TABLE), ('initial', TABLE), ('frozen', TABLE)):
        name = NAME + '_' + suffix
        if schema_editor.connection.vendor == 'sqlite':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}')
        elif schema_editor.connection.vendor == 'postgresql':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name} ON {table}; DROP FUNCTION IF EXISTS {name}();')


class Migration(migrations.Migration):
    dependencies = [('review_batches', '0009_concern_followup')]
    operations = [migrations.RunPython(install, uninstall)]
