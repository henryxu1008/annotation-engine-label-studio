"""Frozen local work plans, never a second review-decision ledger."""

from django.conf import settings
from django.db import models


class ReviewBatch(models.Model):
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="review_batches")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    actor_id_frozen = models.PositiveBigIntegerField()
    creator_principal = models.CharField(max_length=512)
    created_at = models.DateTimeField(auto_now_add=True)
    name = models.CharField(max_length=160)
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    plan = models.JSONField()
    plan_hash = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "idempotency_key"], name="review_batch_project_key_unique"),
        ]
