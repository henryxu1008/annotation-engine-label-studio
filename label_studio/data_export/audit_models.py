"""Audit receipts live in the export app, independently of release authority."""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class AuditActorBinding(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instance_id = models.UUIDField()
    live_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.DO_NOTHING)
    tombstone = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('instance_id', 'live_user'), name='audit_live_actor_unique')]


class AuditPackage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey('projects.Project', on_delete=models.DO_NOTHING)
    organization_id_frozen = models.PositiveBigIntegerField()
    instance_id = models.UUIDField()
    creator = models.ForeignKey(AuditActorBinding, on_delete=models.DO_NOTHING)
    creator_principal = models.CharField(max_length=512)
    creator_source = models.CharField(max_length=128)
    owner_hash = models.CharField(max_length=64)
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    access_policy = models.TextField()
    summary_record = models.TextField()
    input_bundle = models.TextField()
    input_sha256 = models.CharField(max_length=64)
    projection = models.TextField()
    projection_sha256 = models.CharField(max_length=64)
    captured_at = models.DateTimeField(default=timezone.now)
    state = models.CharField(max_length=32, default='dispatch_pending')
    token = models.UUIDField(null=True)
    attempt_number = models.PositiveIntegerField(default=0)
    queued_at = models.DateTimeField(null=True)
    started_at = models.DateTimeField(null=True)
    error_code = models.CharField(max_length=64, default='')
    manifest = models.TextField(null=True)
    manifest_sha256 = models.CharField(max_length=64, default='')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('project', 'creator', 'instance_id', 'idempotency_key'), name='audit_package_key_unique'
            ),
            models.UniqueConstraint(fields=('project', 'idempotency_key'), name='audit_project_command_key'),
        ]
        indexes = [models.Index(fields=('project', 'captured_at', 'id'), name='audit_package_history_idx')]


class AuditAttempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(AuditPackage, on_delete=models.DO_NOTHING)
    number = models.PositiveIntegerField()
    state = models.CharField(max_length=32, default='queued')
    created_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('package', 'number'), name='audit_attempt_number_unique')]


class AuditArtifact(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.OneToOneField(AuditPackage, on_delete=models.DO_NOTHING, related_name='artifact')
    attempt = models.ForeignKey(AuditAttempt, on_delete=models.DO_NOTHING)
    storage_key = models.CharField(max_length=512)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveBigIntegerField()


class AuditRetryReceipt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(AuditPackage, on_delete=models.DO_NOTHING)
    key = models.UUIDField()
    receipt = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('package', 'key'), name='audit_retry_key_unique')]


class AuditAccessEvent(models.Model):
    package = models.ForeignKey(AuditPackage, null=True, on_delete=models.DO_NOTHING)
    project = models.ForeignKey('projects.Project', on_delete=models.DO_NOTHING)
    actor_id_frozen = models.PositiveBigIntegerField(null=True)
    phase = models.CharField(max_length=32)
    policy_version = models.CharField(max_length=128, default='')
    authorization_version = models.CharField(max_length=128, default='')
    allowed = models.BooleanField()
    reason = models.CharField(max_length=64, default='')
    created_at = models.DateTimeField(default=timezone.now)
