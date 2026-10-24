"""Immutable local classification analyses, never review or arbitration decisions."""

from django.conf import settings
from django.db import models


class ClassificationBatch(models.Model):
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="classification_batches")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    creator_principal = models.CharField(max_length=512)
    actor_id_frozen = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    name = models.CharField(max_length=160)
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    manifest = models.JSONField()
    manifest_hash = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "idempotency_key"], name="classification_batch_project_key_unique"
            ),
        ]
