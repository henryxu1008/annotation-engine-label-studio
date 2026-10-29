from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


FROZEN = (
    "id", "project_id", "review_batch_id_frozen", "submission_id_frozen",
    "arbitration_id_frozen", "actor_id_frozen", "creator_principal", "creator_source", "created_at",
    "origin", "origin_hash", "idempotency_key", "request_hash",
)
NULLABLE = ("review_batch_id", "ner_arbitration_id", "created_by_id")
TABLE = "review_batches_nerreviewarbitrationhandoff"
TRIGGER = "ner_review_arbitration_handoff_frozen"


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        old, new = (", ".join(f"{prefix}.{field}" for field in FROZEN) for prefix in ("OLD", "NEW"))
        guard = f"ROW({old}) IS DISTINCT FROM ROW({new})"
        guard += " OR " + " OR ".join(
            f"(NEW.{field} IS NOT NULL AND NEW.{field} IS DISTINCT FROM OLD.{field})" for field in NULLABLE
        )
        schema_editor.execute(f"""
            CREATE FUNCTION {TRIGGER}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF {guard} THEN RAISE EXCEPTION 'NER review handoff is immutable'; END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER {TRIGGER} BEFORE UPDATE ON {TABLE}
            FOR EACH ROW EXECUTE FUNCTION {TRIGGER}();
        """)
    elif vendor == "sqlite":
        guard = " OR ".join(f"OLD.{field} IS NOT NEW.{field}" for field in FROZEN)
        guard += " OR " + " OR ".join(
            f"(NEW.{field} IS NOT NULL AND NEW.{field} IS NOT OLD.{field})" for field in NULLABLE
        )
        schema_editor.execute(
            f"CREATE TRIGGER {TRIGGER} BEFORE UPDATE ON {TABLE} WHEN {guard} "
            "BEGIN SELECT RAISE(ABORT, 'NER review handoff is immutable'); END;"
        )


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON {TABLE}; DROP FUNCTION IF EXISTS {TRIGGER}();")
    elif schema_editor.connection.vendor == "sqlite":
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {TRIGGER};")


class Migration(migrations.Migration):
    dependencies = [
        ("review_batches", "0003_arbitration_handoff"),
        ("classification_disagreements", "0008_ner_arbitration_guards"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="NerReviewArbitrationHandoff",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("review_batch_id_frozen", models.PositiveBigIntegerField()),
                ("submission_id_frozen", models.PositiveBigIntegerField()),
                ("arbitration_id_frozen", models.PositiveBigIntegerField()),
                ("actor_id_frozen", models.PositiveBigIntegerField()),
                ("creator_principal", models.CharField(max_length=512)),
                ("creator_source", models.CharField(max_length=128)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("origin", models.JSONField()),
                ("origin_hash", models.CharField(max_length=64)),
                ("idempotency_key", models.UUIDField()),
                ("request_hash", models.CharField(max_length=64)),
                ("ner_arbitration", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to="classification_disagreements.nerarbitrationtask")),
                ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="projects.project")),
                ("review_batch", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to="review_batches.reviewbatch")),
            ],
            options={
                "indexes": [models.Index(fields=["project", "review_batch_id_frozen", "submission_id_frozen"], name="ner_handoff_source_idx")],
                "constraints": [
                    models.UniqueConstraint(fields=("project", "idempotency_key"), name="ner_handoff_project_key_unique"),
                    models.UniqueConstraint(
                        fields=("project", "review_batch_id_frozen", "submission_id_frozen", "arbitration_id_frozen"),
                        name="ner_handoff_binding_unique",
                    ),
                ],
            },
        ),
        migrations.RunPython(install, uninstall),
    ]
