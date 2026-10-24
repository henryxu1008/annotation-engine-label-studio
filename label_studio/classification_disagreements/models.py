"""Frozen classification analyses and local arbitration receipts, never review approvals."""

from django.conf import settings
from django.db import models
from django.utils import timezone


class ClassificationBatch(models.Model):
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='classification_batches')
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
                fields=['project', 'idempotency_key'], name='classification_batch_project_key_unique'
            ),
        ]


class ArbitrationTask(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending'
        RESOLVED = 'resolved'
        CANCELLED = 'cancelled'

    project = models.ForeignKey(
        'projects.Project', on_delete=models.CASCADE, related_name='classification_arbitrations'
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name='created_classification_arbitrations',
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='classification_arbitrations'
    )
    creator_principal = models.CharField(max_length=512)
    actor_id_frozen = models.PositiveBigIntegerField()
    assignee_id_frozen = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    batch_id_frozen = models.PositiveBigIntegerField()
    task_id_frozen = models.PositiveBigIntegerField()
    input_set_hash = models.CharField(max_length=64)
    origin = models.JSONField()
    origin_hash = models.CharField(max_length=64)
    participant_ids = models.JSONField()
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    version = models.PositiveIntegerField(default=1)
    cancel_key = models.UUIDField(null=True)
    cancel_request_hash = models.CharField(max_length=64, blank=True, default='')
    cancellation = models.JSONField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['project', 'idempotency_key'], name='arb_project_create_key_unique'),
            models.UniqueConstraint(fields=['project', 'cancel_key'], name='arb_project_cancel_key_unique'),
            models.UniqueConstraint(
                fields=['project', 'task_id_frozen', 'input_set_hash'],
                condition=models.Q(status__in=['pending', 'resolved']),
                name='arb_active_input_unique',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status='pending',
                        version=1,
                        cancel_key__isnull=True,
                        cancellation__isnull=True,
                        cancel_request_hash='',
                    )
                    | models.Q(
                        status='resolved',
                        version=2,
                        cancel_key__isnull=True,
                        cancellation__isnull=True,
                        cancel_request_hash='',
                    )
                    | (
                        models.Q(status='cancelled', version=2, cancel_key__isnull=False, cancellation__isnull=False)
                        & ~models.Q(cancel_request_hash='')
                    )
                ),
                name='arb_task_state_valid',
            ),
        ]


class ArbitrationDecision(models.Model):
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE)
    arbitration = models.OneToOneField(ArbitrationTask, on_delete=models.CASCADE, related_name='decision')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    actor_id_frozen = models.PositiveBigIntegerField()
    principal = models.CharField(max_length=512)
    created_at = models.DateTimeField(default=timezone.now)
    input_set_hash = models.CharField(max_length=64)
    rule = models.CharField(max_length=64)
    winner = models.JSONField()
    reason = models.TextField()
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    payload_hash = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['project', 'idempotency_key'], name='arb_project_decision_key_unique')
        ]
