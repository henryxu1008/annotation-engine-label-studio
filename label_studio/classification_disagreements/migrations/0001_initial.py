from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("projects", "0035_projectmember_role_and_unique"),
    ]
    operations = [
        migrations.CreateModel(
            name="ClassificationBatch",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creator_principal", models.CharField(max_length=512)),
                ("actor_id_frozen", models.PositiveBigIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("name", models.CharField(max_length=160)),
                ("idempotency_key", models.UUIDField()),
                ("request_hash", models.CharField(max_length=64)),
                ("manifest", models.JSONField()),
                ("manifest_hash", models.CharField(max_length=64)),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="classification_batches",
                        to="projects.project",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("project", "idempotency_key"), name="classification_batch_project_key_unique"
                    )
                ]
            },
        ),
    ]
