"""Private, frozen approved-Submission export receipts (not Host dataset versions)."""

import uuid

from django.db import models


class ApprovedDatasetRelease(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="approved_exports")
    organization_id_frozen = models.PositiveBigIntegerField()
    actor_id_frozen = models.PositiveBigIntegerField()
    principal_id = models.CharField(max_length=256)
    idempotency_key = models.CharField(max_length=128)
    request_hash = models.CharField(max_length=64)
    frozen_input = models.JSONField()
    input_sha256 = models.CharField(max_length=64)
    content_sha256 = models.CharField(max_length=64)
    state = models.CharField(max_length=32, default="dispatch_pending", db_index=True)
    error_code = models.CharField(max_length=64, blank=True)
    attempt_number = models.PositiveIntegerField(default=0)
    manifest = models.BinaryField(null=True)
    manifest_sha256 = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "organization_id_frozen", "actor_id_frozen", "idempotency_key"],
                name="approved_export_idempotency",
            )
        ]


class ApprovedDatasetReleaseItem(models.Model):
    release = models.ForeignKey(ApprovedDatasetRelease, on_delete=models.CASCADE, related_name="items")
    ordinal = models.PositiveIntegerField()
    submission_id_frozen = models.PositiveBigIntegerField()
    assignment_id_frozen = models.PositiveBigIntegerField()
    record = models.JSONField()
    record_sha256 = models.CharField(max_length=64)

    class Meta:
        ordering = ["ordinal"]
        constraints = [
            models.UniqueConstraint(fields=["release", "ordinal"], name="approved_export_ordinal"),
            models.UniqueConstraint(fields=["release", "submission_id_frozen"], name="approved_export_submission"),
            models.UniqueConstraint(fields=["release", "assignment_id_frozen"], name="approved_export_assignment"),
        ]


class ApprovedDatasetReleaseAttempt(models.Model):
    release = models.ForeignKey(ApprovedDatasetRelease, on_delete=models.CASCADE, related_name="attempts")
    number = models.PositiveIntegerField()
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    state = models.CharField(max_length=32, default="queued")
    heartbeat = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)
    error_code = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["release", "number"], name="approved_export_attempt")]


class ApprovedDatasetReleaseArtifact(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    release = models.ForeignKey(ApprovedDatasetRelease, on_delete=models.CASCADE, related_name="artifacts")
    attempt = models.ForeignKey(ApprovedDatasetReleaseAttempt, on_delete=models.PROTECT)
    name = models.CharField(max_length=128)
    format = models.CharField(max_length=32)
    storage_key = models.CharField(max_length=512)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveBigIntegerField()
    record_count = models.PositiveIntegerField()
    provenance = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["release", "name"], name="approved_export_artifact")]


class ApprovedDatasetReleaseAccessEvent(models.Model):
    release = models.ForeignKey(
        ApprovedDatasetRelease, null=True, on_delete=models.PROTECT, related_name="access_events"
    )
    project_id_frozen = models.PositiveBigIntegerField()
    actor_id_frozen = models.PositiveBigIntegerField(null=True)
    principal_id = models.CharField(max_length=256, blank=True)
    phase = models.CharField(max_length=32)
    policy_version = models.CharField(max_length=128, blank=True)
    allowed = models.BooleanField()
    reason = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
