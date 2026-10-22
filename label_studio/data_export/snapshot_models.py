"""Source snapshots are export evidence, never Host DatasetVersions."""

import uuid

from django.db import models


class ReleaseSnapshot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="release_snapshots")
    release = models.ForeignKey(
        "data_export.ApprovedDatasetRelease", on_delete=models.PROTECT, related_name="snapshots"
    )
    actor_id = models.PositiveBigIntegerField()
    principal_id = models.CharField(max_length=256)
    idempotency_key = models.CharField(max_length=128)
    policy = models.JSONField()
    state = models.CharField(max_length=32, default="dispatch_pending")
    token = models.UUIDField(default=uuid.uuid4)
    queued_at = models.DateTimeField(null=True)
    started_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    error_code = models.CharField(max_length=64, blank=True)
    manifest = models.BinaryField(null=True)
    manifest_sha256 = models.CharField(max_length=64, blank=True)
    content_digest = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "actor_id", "idempotency_key"], name="release_snapshot_key")
        ]
