from django.db import migrations

TASK = 'classification_disagreements_bboxarbitrationtask'
DECISION = 'classification_disagreements_bboxarbitrationdecision'
TASK_FIELDS = ('id', 'project_id', 'batch_id', 'task_id_frozen', 'actor_id_frozen', 'creator_principal',
               'creator_source', 'assignee_id_frozen', 'created_at', 'input_hash', 'origin', 'origin_hash',
               'participant_ids', 'idempotency_key', 'request_hash')
DECISION_FIELDS = ('id', 'project_id', 'arbitration_id', 'actor_id_frozen', 'principal', 'source', 'created_at',
                   'input_hash', 'winner', 'reason', 'idempotency_key', 'request_hash', 'payload_hash')


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    base = ("t.id=NEW.arbitration_id AND t.project_id=NEW.project_id AND t.status='pending' "
            "AND t.version=1 AND t.input_hash=NEW.input_hash AND t.assignee_id_frozen=NEW.actor_id_frozen "
            "AND NEW.principal<>'' AND NEW.source<>'' AND NEW.reason<>''")
    if vendor == 'postgresql':
        winner = ("EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN octet_length(t.origin::text)<=2097152 "
                  "AND jsonb_typeof(t.origin->'item'->'cohort')='array' THEN t.origin->'item'->'cohort' "
                  "ELSE '[]'::jsonb END) e WHERE e->'source'=NEW.winner)")
        independent = ("jsonb_typeof(t.participant_ids)='array' AND NOT EXISTS "
                       "(SELECT 1 FROM jsonb_array_elements_text(t.participant_ids) p WHERE p=CAST(NEW.actor_id_frozen AS text))")
        schema_editor.execute(f"""CREATE FUNCTION bbox_decision_scope() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN IF NOT EXISTS (SELECT 1 FROM {TASK} t WHERE {base} AND {winner} AND {independent})
            THEN RAISE EXCEPTION 'bbox decision scope mismatch' USING ERRCODE='23514'; END IF; RETURN NEW; END $$;
          CREATE TRIGGER bbox_decision_scope BEFORE INSERT ON {DECISION}
          FOR EACH ROW EXECUTE FUNCTION bbox_decision_scope();""")
    elif vendor == 'sqlite':
        winner = ("EXISTS (SELECT 1 FROM json_each(CASE WHEN length(CAST(t.origin AS BLOB))<=2097152 "
                  "AND json_type(t.origin,'$.item.cohort')='array' THEN json_extract(t.origin,'$.item.cohort') "
                  "ELSE '[]' END) e WHERE json_extract(e.value,'$.source')=json(NEW.winner))")
        independent = ("json_type(t.participant_ids)='array' AND NOT EXISTS "
                       "(SELECT 1 FROM json_each(t.participant_ids) p WHERE p.value=NEW.actor_id_frozen)")
        schema_editor.execute(f"""CREATE TRIGGER bbox_decision_scope BEFORE INSERT ON {DECISION}
          WHEN NOT EXISTS (SELECT 1 FROM {TASK} t WHERE {base} AND {winner} AND {independent})
          BEGIN SELECT RAISE(ABORT, 'bbox decision scope mismatch'); END;""")
    for name, table, fields, nullable in (
        ('bbox_arbitration_immutable', TASK, TASK_FIELDS, ('created_by_id', 'assignee_id')),
        ('bbox_decision_immutable', DECISION, DECISION_FIELDS, ('actor_id',)),
    ):
        if vendor == 'postgresql':
            old, new = (', '.join(f'{prefix}.{field}' for field in fields) for prefix in ('OLD', 'NEW'))
            guard = f'ROW({old}) IS DISTINCT FROM ROW({new})'
            guard += ''.join(f' OR (NEW.{field} IS NOT NULL AND NEW.{field} IS DISTINCT FROM OLD.{field})'
                             for field in nullable)
            if table == TASK:
                guard += (" OR (OLD.status<>'pending' AND (OLD.status,OLD.version,OLD.cancel_key,OLD.cancel_request_hash,OLD.cancellation) "
                          "IS DISTINCT FROM (NEW.status,NEW.version,NEW.cancel_key,NEW.cancel_request_hash,NEW.cancellation))"
                          f" OR (NEW.status='resolved' AND NOT EXISTS (SELECT 1 FROM {DECISION} d WHERE "
                          "d.arbitration_id=OLD.id AND d.project_id=OLD.project_id AND d.input_hash=OLD.input_hash "
                          "AND d.actor_id_frozen=OLD.assignee_id_frozen))"
                          f" OR (NEW.status='cancelled' AND EXISTS (SELECT 1 FROM {DECISION} d WHERE d.arbitration_id=OLD.id))")
            schema_editor.execute(f"""CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
              BEGIN IF {guard} THEN RAISE EXCEPTION 'bbox arbitration evidence is immutable' USING ERRCODE='23514';
              END IF; RETURN NEW; END $$; CREATE TRIGGER {name} BEFORE UPDATE ON {table}
              FOR EACH ROW EXECUTE FUNCTION {name}();""")
        elif vendor == 'sqlite':
            guard = ' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in fields)
            guard += ''.join(f' OR (NEW.{field} IS NOT NULL AND NEW.{field} IS NOT OLD.{field})' for field in nullable)
            if table == TASK:
                guard += (" OR (OLD.status<>'pending' AND (OLD.status IS NOT NEW.status OR OLD.version IS NOT NEW.version "
                          "OR OLD.cancel_key IS NOT NEW.cancel_key OR OLD.cancel_request_hash IS NOT NEW.cancel_request_hash "
                          "OR OLD.cancellation IS NOT NEW.cancellation))"
                          f" OR (NEW.status='resolved' AND NOT EXISTS (SELECT 1 FROM {DECISION} d WHERE "
                          "d.arbitration_id=OLD.id AND d.project_id=OLD.project_id AND d.input_hash=OLD.input_hash "
                          "AND d.actor_id_frozen=OLD.assignee_id_frozen))"
                          f" OR (NEW.status='cancelled' AND EXISTS (SELECT 1 FROM {DECISION} d WHERE d.arbitration_id=OLD.id))")
            schema_editor.execute(f"""CREATE TRIGGER {name} BEFORE UPDATE ON {table} WHEN {guard}
              BEGIN SELECT RAISE(ABORT, 'bbox arbitration evidence is immutable'); END;""")


def uninstall(apps, schema_editor):
    for name, table in (('bbox_decision_scope', DECISION), ('bbox_arbitration_immutable', TASK),
                        ('bbox_decision_immutable', DECISION)):
        if schema_editor.connection.vendor == 'postgresql':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name} ON {table}; DROP FUNCTION IF EXISTS {name}();')
        elif schema_editor.connection.vendor == 'sqlite':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name};')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0017_bboxarbitrationtask_bboxarbitrationdecision_and_more')]
    operations = [migrations.RunPython(install, uninstall)]
