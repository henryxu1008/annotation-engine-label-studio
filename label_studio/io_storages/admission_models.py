"""Durable state for project-scoped external storage work."""
import uuid

from django.db import models


class StorageOperation(models.Model):
    class State(models.TextChoices):
        QUEUED = 'queued'
        RUNNING = 'running'
        UNCERTAIN = 'uncertain'
        COMPLETED = 'completed'
        RECONCILED = 'reconciled'
        CANCELLED = 'cancelled'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey('projects.Project', on_delete=models.PROTECT, related_name='storage_operations')
    storage_model = models.CharField(max_length=128)
    storage_id = models.PositiveBigIntegerField()
    operation = models.CharField(max_length=48)
    lifecycle_revision = models.PositiveBigIntegerField()
    subject_id = models.PositiveBigIntegerField(null=True)
    target_key = models.TextField(null=True)
    target_version_id = models.TextField(null=True)
    target_storage_binding = models.CharField(max_length=64, null=True)
    target_object_token = models.CharField(max_length=32, null=True)
    effect_payload = models.JSONField(null=True)
    effect_phase = models.CharField(max_length=32, null=True)
    effect_claim_token = models.UUIDField(null=True)
    next_dispatch_at = models.DateTimeField(null=True)
    dispatch_attempts = models.PositiveIntegerField(default=0)
    state = models.CharField(max_length=16, choices=State.choices, default=State.RUNNING)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)
    resolution_reason = models.TextField(blank=True, default='')
    resolved_by = models.ForeignKey('users.User', null=True, on_delete=models.SET_NULL, related_name='+')
    resolved_by_id_snapshot = models.BigIntegerField(null=True)

    class Meta:
        indexes = [models.Index(fields=['project', 'state'], name='storage_op_project_state_idx')]


class StorageOperationRecovery(models.Model):
    operation = models.ForeignKey(StorageOperation, on_delete=models.PROTECT, related_name='recoveries')
    claim_token = models.UUIDField()
    reason = models.TextField()
    actor = models.ForeignKey('users.User', null=True, on_delete=models.SET_NULL, related_name='+')
    actor_id_snapshot = models.BigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['operation', 'claim_token'], name='unique_storage_recovery_claim')]
