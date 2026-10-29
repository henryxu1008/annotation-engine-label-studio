from django.db import migrations

TASK_FIELDS = (
    "id",
    "project_id",
    "creator_principal",
    "creator_source",
    "actor_id_frozen",
    "assignee_id_frozen",
    "created_at",
    "ner_batch_id_frozen",
    "task_id_frozen",
    "input_set_hash",
    "origin",
    "origin_hash",
    "participant_ids",
    "idempotency_key",
    "request_hash",
)
DECISION_FIELDS = (
    "id",
    "project_id",
    "arbitration_id",
    "actor_id_frozen",
    "principal",
    "source",
    "created_at",
    "input_set_hash",
    "rule",
    "winner",
    "reason",
    "idempotency_key",
    "request_hash",
    "payload_hash",
)
STATE_FIELDS = ("status", "version", "cancel_key", "cancel_request_hash", "cancellation")


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    decision_table = 'classification_disagreements_nerarbitrationdecision'
    task_table = 'classification_disagreements_nerarbitrationtask'
    match = ("t.id=NEW.arbitration_id AND t.project_id=NEW.project_id AND t.status='pending' AND t.version=1 "
             "AND t.input_set_hash=NEW.input_set_hash AND t.assignee_id_frozen=NEW.actor_id_frozen "
             "AND NEW.rule='adopt-exact-ner-submission/v1' AND NEW.principal<>'' AND NEW.source<>''")
    if vendor == 'postgresql':
        match += (" AND EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN octet_length(t.origin::text)<=262144 "
                  "AND jsonb_typeof(t.origin->'item'->'cohort')='array' THEN t.origin->'item'->'cohort' "
                  "ELSE '[]'::jsonb END) e WHERE e->'source'=NEW.winner)")
        schema_editor.execute(f"""CREATE FUNCTION ner_arb_decision_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NOT EXISTS (SELECT 1 FROM {task_table} t WHERE {match})
              THEN RAISE EXCEPTION 'NER decision input mismatch'; END IF; RETURN NEW; END $$;
            CREATE TRIGGER ner_arb_decision_insert BEFORE INSERT ON {decision_table}
            FOR EACH ROW EXECUTE FUNCTION ner_arb_decision_insert();""")
    elif vendor == 'sqlite':
        match += (" AND EXISTS (SELECT 1 FROM json_each(CASE WHEN length(CAST(t.origin AS BLOB))<=262144 "
                  "AND json_type(t.origin,'$.item.cohort')='array' THEN json_extract(t.origin,'$.item.cohort') "
                  "ELSE '[]' END) e WHERE json_extract(e.value,'$.source')=json(NEW.winner))")
        schema_editor.execute(f"""CREATE TRIGGER ner_arb_decision_insert BEFORE INSERT ON {decision_table}
            WHEN NOT EXISTS (SELECT 1 FROM {task_table} t WHERE {match})
            BEGIN SELECT RAISE(ABORT, 'NER decision input mismatch'); END;""")
    for kind, fields, nullable in (
        ("task", TASK_FIELDS, ("created_by_id", "assignee_id")),
        ("decision", DECISION_FIELDS, ("actor_id",)),
    ):
        table = "classification_disagreements_nerarbitration" + kind
        name = "ner_arb_" + kind + "_frozen"
        if vendor == "postgresql":
            old, new = (", ".join(f"{prefix}.{field}" for field in fields) for prefix in ("OLD", "NEW"))
            guard = f"ROW({old}) IS DISTINCT FROM ROW({new})"
            guard += " OR " + " OR ".join(
                f"(NEW.{f} IS NOT NULL AND NEW.{f} IS DISTINCT FROM OLD.{f})" for f in nullable
            )
            if kind == "task":
                old_state, new_state = (", ".join(f"{prefix}.{f}" for f in STATE_FIELDS) for prefix in ("OLD", "NEW"))
                guard += f" OR (OLD.status <> 'pending' AND ROW({old_state}) IS DISTINCT FROM ROW({new_state}))"
                guard += " OR (OLD.status = 'pending' AND NEW.status = 'resolved' AND NOT EXISTS (SELECT 1 FROM classification_disagreements_nerarbitrationdecision d WHERE d.arbitration_id = OLD.id AND d.project_id = OLD.project_id AND d.input_set_hash = OLD.input_set_hash AND d.actor_id_frozen = OLD.assignee_id_frozen))"
                guard += " OR (NEW.status = 'cancelled' AND EXISTS (SELECT 1 FROM classification_disagreements_nerarbitrationdecision d WHERE d.arbitration_id = OLD.id))"
            schema_editor.execute(
                f"""
                CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF {guard} THEN RAISE EXCEPTION 'Arbitration record is immutable'; END IF;
                    RETURN NEW;
                END $$;
                CREATE TRIGGER {name} BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION {name}();
            """
            )
        elif vendor == "sqlite":
            guard = " OR ".join(f"OLD.{f} IS NOT NEW.{f}" for f in fields)
            guard += " OR " + " OR ".join(f"(NEW.{f} IS NOT NULL AND NEW.{f} IS NOT OLD.{f})" for f in nullable)
            if kind == "task":
                state = " OR ".join(f"OLD.{f} IS NOT NEW.{f}" for f in STATE_FIELDS)
                guard += f" OR (OLD.status <> 'pending' AND ({state}))"
                guard += " OR (OLD.status = 'pending' AND NEW.status = 'resolved' AND NOT EXISTS (SELECT 1 FROM classification_disagreements_nerarbitrationdecision d WHERE d.arbitration_id = OLD.id AND d.project_id = OLD.project_id AND d.input_set_hash = OLD.input_set_hash AND d.actor_id_frozen = OLD.assignee_id_frozen))"
                guard += " OR (NEW.status = 'cancelled' AND EXISTS (SELECT 1 FROM classification_disagreements_nerarbitrationdecision d WHERE d.arbitration_id = OLD.id))"
            schema_editor.execute(
                f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} WHEN {guard} BEGIN SELECT RAISE(ABORT, 'Arbitration record is immutable'); END;"
            )


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('DROP TRIGGER IF EXISTS ner_arb_decision_insert ON classification_disagreements_nerarbitrationdecision; DROP FUNCTION IF EXISTS ner_arb_decision_insert();')
    elif schema_editor.connection.vendor == 'sqlite':
        schema_editor.execute('DROP TRIGGER IF EXISTS ner_arb_decision_insert;')
    for kind in ("task", "decision"):
        name = "ner_arb_" + kind + "_frozen"
        table = "classification_disagreements_nerarbitration" + kind
        if schema_editor.connection.vendor == "postgresql":
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}; DROP FUNCTION IF EXISTS {name}();")
        elif schema_editor.connection.vendor == "sqlite":
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {name};")


class Migration(migrations.Migration):
    dependencies = [("classification_disagreements", "0007_ner_arbitration")]
    operations = [migrations.RunPython(install, uninstall)]
