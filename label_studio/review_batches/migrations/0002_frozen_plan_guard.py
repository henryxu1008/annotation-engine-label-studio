from django.db import migrations

FIELDS = (
    "project_id",
    "actor_id_frozen",
    "creator_principal",
    "created_at",
    "name",
    "idempotency_key",
    "request_hash",
    "plan",
    "plan_hash",
)


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        fields = ", ".join(f"OLD.{field}" for field in FIELDS)
        changed = ", ".join(f"NEW.{field}" for field in FIELDS)
        schema_editor.execute(
            f"""
            CREATE FUNCTION review_batch_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF ROW({fields}) IS DISTINCT FROM ROW({changed}) THEN
                    RAISE EXCEPTION 'Review batch plan is immutable';
                END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER review_batch_frozen BEFORE UPDATE ON review_batches_reviewbatch
            FOR EACH ROW EXECUTE FUNCTION review_batch_frozen();
        """
        )
    elif vendor == "sqlite":
        changed = " OR ".join(f"OLD.{field} IS NOT NEW.{field}" for field in FIELDS)
        schema_editor.execute(
            f"""
            CREATE TRIGGER review_batch_frozen BEFORE UPDATE ON review_batches_reviewbatch
            WHEN {changed} BEGIN SELECT RAISE(ABORT, 'Review batch plan is immutable'); END;
        """
        )


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP TRIGGER IF EXISTS review_batch_frozen ON review_batches_reviewbatch;")
        schema_editor.execute("DROP FUNCTION IF EXISTS review_batch_frozen();")
    elif schema_editor.connection.vendor == "sqlite":
        schema_editor.execute("DROP TRIGGER IF EXISTS review_batch_frozen;")


class Migration(migrations.Migration):
    dependencies = [("review_batches", "0001_initial")]
    operations = [migrations.RunPython(install, uninstall)]
