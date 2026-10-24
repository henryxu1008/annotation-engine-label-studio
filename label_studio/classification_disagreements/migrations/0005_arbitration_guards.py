from django.db import migrations

TASK_FIELDS = (
    "id",
    "project_id",
    "creator_principal",
    "actor_id_frozen",
    "assignee_id_frozen",
    "created_at",
    "batch_id_frozen",
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
    for kind, fields, nullable in (
        ("task", TASK_FIELDS, ("created_by_id", "assignee_id")),
        ("decision", DECISION_FIELDS, ("actor_id",)),
    ):
        table = "classification_disagreements_arbitration" + kind
        name = "classification_arb_" + kind + "_frozen"
        if vendor == "postgresql":
            old, new = (", ".join(f"{prefix}.{field}" for field in fields) for prefix in ("OLD", "NEW"))
            guard = f"ROW({old}) IS DISTINCT FROM ROW({new})"
            guard += " OR " + " OR ".join(
                f"(NEW.{f} IS NOT NULL AND NEW.{f} IS DISTINCT FROM OLD.{f})" for f in nullable
            )
            if kind == "task":
                old_state, new_state = (", ".join(f"{prefix}.{f}" for f in STATE_FIELDS) for prefix in ("OLD", "NEW"))
                guard += f" OR (OLD.status <> 'pending' AND ROW({old_state}) IS DISTINCT FROM ROW({new_state}))"
                guard += " OR (OLD.status = 'pending' AND NEW.status = 'resolved' AND NOT EXISTS (SELECT 1 FROM classification_disagreements_arbitrationdecision d WHERE d.arbitration_id = OLD.id))"
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
                guard += " OR (OLD.status = 'pending' AND NEW.status = 'resolved' AND NOT EXISTS (SELECT 1 FROM classification_disagreements_arbitrationdecision d WHERE d.arbitration_id = OLD.id))"
            schema_editor.execute(
                f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} WHEN {guard} BEGIN SELECT RAISE(ABORT, 'Arbitration record is immutable'); END;"
            )


def uninstall(apps, schema_editor):
    for kind in ("task", "decision"):
        name = "classification_arb_" + kind + "_frozen"
        table = "classification_disagreements_arbitration" + kind
        if schema_editor.connection.vendor == "postgresql":
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}; DROP FUNCTION IF EXISTS {name}();")
        elif schema_editor.connection.vendor == "sqlite":
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {name};")


class Migration(migrations.Migration):
    dependencies = [("classification_disagreements", "0004_arbitration")]
    operations = [migrations.RunPython(install, uninstall)]
