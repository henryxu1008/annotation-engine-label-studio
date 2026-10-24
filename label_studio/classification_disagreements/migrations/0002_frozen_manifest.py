from django.db import migrations

FIELDS = (
    "project_id",
    "creator_principal",
    "actor_id_frozen",
    "created_at",
    "name",
    "idempotency_key",
    "request_hash",
    "manifest",
    "manifest_hash",
)


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        old = ", ".join(f"OLD.{field}" for field in FIELDS)
        new = ", ".join(f"NEW.{field}" for field in FIELDS)
        schema_editor.execute(f"""
            CREATE FUNCTION classification_batch_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF ROW({old}) IS DISTINCT FROM ROW({new}) OR
                   (NEW.created_by_id IS NOT NULL AND NEW.created_by_id IS DISTINCT FROM OLD.created_by_id) THEN
                    RAISE EXCEPTION 'Classification batch is immutable';
                END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER classification_batch_frozen BEFORE UPDATE ON classification_disagreements_classificationbatch
            FOR EACH ROW EXECUTE FUNCTION classification_batch_frozen();
        """)
    elif vendor == "sqlite":
        changed = " OR ".join(f"OLD.{field} IS NOT NEW.{field}" for field in FIELDS)
        schema_editor.execute(f"""
            CREATE TRIGGER classification_batch_frozen BEFORE UPDATE ON classification_disagreements_classificationbatch
            WHEN {changed} OR (NEW.created_by_id IS NOT NULL AND NEW.created_by_id IS NOT OLD.created_by_id)
            BEGIN SELECT RAISE(ABORT, 'Classification batch is immutable'); END;
        """)


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS classification_batch_frozen ON classification_disagreements_classificationbatch;"
        )
        schema_editor.execute("DROP FUNCTION IF EXISTS classification_batch_frozen();")
    elif schema_editor.connection.vendor == "sqlite":
        schema_editor.execute("DROP TRIGGER IF EXISTS classification_batch_frozen;")


class Migration(migrations.Migration):
    dependencies = [("classification_disagreements", "0001_initial")]
    operations = [migrations.RunPython(install, uninstall)]
