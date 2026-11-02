"""Frozen work plans, input facts and separate non-authoritative supervision."""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class ReviewBatch(models.Model):
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='review_batches')
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
            models.UniqueConstraint(fields=['project', 'idempotency_key'], name='review_batch_project_key_unique'),
        ]


class SecondReviewTask(models.Model):
    """One independent supervision per exact subject, including terminal history."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE)
    organization_id_frozen = models.PositiveBigIntegerField()
    instance_id = models.UUIDField()
    review_batch = models.ForeignKey(ReviewBatch, null=True, on_delete=models.SET_NULL)
    review_batch_id_frozen = models.PositiveBigIntegerField()
    submission = models.ForeignKey('tasks.Submission', null=True, on_delete=models.SET_NULL)
    submission_id_frozen = models.PositiveBigIntegerField()
    revision = models.PositiveBigIntegerField()
    first_review = models.ForeignKey('tasks.ReviewDecision', null=True, on_delete=models.SET_NULL, related_name='+')
    first_reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+'
    )
    original_submitter = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+'
    )
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    assignee_id_frozen = models.PositiveBigIntegerField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    actor_id_frozen = models.PositiveBigIntegerField()
    creator_principal = models.CharField(max_length=512)
    creator_source = models.CharField(max_length=128)
    created_at = models.DateTimeField(default=timezone.now)
    source_hash = models.CharField(max_length=64)
    first_review_hash = models.CharField(max_length=64)
    proof = models.JSONField()
    receipt = models.JSONField()
    receipt_hash = models.CharField(max_length=64)
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, default='pending')
    version = models.PositiveIntegerField(default=1)
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    cancel_key = models.UUIDField(null=True)
    cancellation = models.JSONField(null=True)
    cancellation_hash = models.CharField(max_length=64, default='')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('project', 'idempotency_key'), name='second_review_create_key_unique'),
            models.UniqueConstraint(fields=('project', 'cancel_key'), name='second_review_cancel_key_unique'),
            models.UniqueConstraint(
                fields=('project', 'instance_id', 'submission_id_frozen', 'revision'),
                name='second_review_subject_unique',
            ),
            models.CheckConstraint(
                check=(
                    models.Q(status='pending', version=1) | models.Q(status__in=('completed', 'cancelled'), version=2)
                ),
                name='second_review_task_state',
            ),
            models.CheckConstraint(
                check=(
                    models.Q(status='cancelled', cancel_key__isnull=False, cancellation__isnull=False)
                    | (
                        ~models.Q(status='cancelled')
                        & models.Q(cancel_key__isnull=True, cancellation__isnull=True, cancellation_hash='')
                    )
                ),
                name='second_review_cancel_envelope',
            ),
        ]
        indexes = [models.Index(fields=('project', 'assignee', 'status'), name='second_review_queue_idx')]


class SecondReviewDecision(models.Model):
    """Immutable supervisory opinion; never participates in approval/release."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE)
    task = models.OneToOneField(SecondReviewTask, null=True, on_delete=models.SET_NULL, related_name='opinion')
    task_id_frozen = models.UUIDField(unique=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    actor_id_frozen = models.PositiveBigIntegerField()
    creator_principal = models.CharField(max_length=512)
    creator_source = models.CharField(max_length=128)
    created_at = models.DateTimeField(default=timezone.now)
    source_hash = models.CharField(max_length=64)
    first_review_hash = models.CharField(max_length=64)
    outcome = models.CharField(max_length=16)
    reason = models.TextField(blank=True, default='')
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    receipt = models.JSONField()
    receipt_hash = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('project', 'idempotency_key'), name='second_review_decision_key_unique'),
            models.CheckConstraint(check=models.Q(outcome__in=('confirmed', 'concern')), name='second_review_outcome'),
        ]


class ReviewConfidenceObservation(models.Model):
    """Trusted producer assertion on one exact revision. Contains no answers."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE)
    instance_id = models.UUIDField()
    producer_namespace = models.UUIDField()
    profile_id = models.UUIDField()
    profile_digest = models.CharField(max_length=64)
    event_uuid = models.UUIDField()
    submission = models.ForeignKey('tasks.Submission', null=True, on_delete=models.SET_NULL)
    submission_id_frozen = models.PositiveBigIntegerField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    actor_id_frozen = models.PositiveBigIntegerField()
    creator_principal = models.CharField(max_length=512)
    creator_source = models.CharField(max_length=128)
    request_hash = models.CharField(max_length=64)
    proof = models.JSONField()
    receipt = models.JSONField()
    fact_hash = models.CharField(max_length=64)
    receipt_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('project', 'instance_id', 'producer_namespace', 'event_uuid'),
                name='confidence_event_unique',
            ),
            models.UniqueConstraint(
                fields=('project', 'instance_id', 'profile_id', 'profile_digest', 'submission_id_frozen'),
                name='confidence_subject_unique',
            ),
        ]


class ReviewArbitrationHandoff(models.Model):
    """Immutable provenance and command receipt, never another decision ledger."""

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE)
    review_batch = models.ForeignKey(ReviewBatch, null=True, on_delete=models.SET_NULL)
    arbitration = models.ForeignKey(
        'classification_disagreements.ArbitrationTask', null=True, on_delete=models.SET_NULL
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    review_batch_id_frozen = models.PositiveBigIntegerField()
    submission_id_frozen = models.PositiveBigIntegerField()
    arbitration_id_frozen = models.PositiveBigIntegerField()
    actor_id_frozen = models.PositiveBigIntegerField()
    creator_principal = models.CharField(max_length=512)
    created_at = models.DateTimeField(default=timezone.now)
    origin = models.JSONField()
    origin_hash = models.CharField(max_length=64)
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['project', 'idempotency_key'], name='review_handoff_project_key_unique'),
            models.UniqueConstraint(
                fields=['project', 'review_batch_id_frozen', 'submission_id_frozen', 'arbitration_id_frozen'],
                name='review_handoff_binding_unique',
            ),
        ]
        indexes = [
            models.Index(
                fields=['project', 'review_batch_id_frozen', 'submission_id_frozen'],
                name='review_handoff_source_idx',
            ),
        ]


class NerReviewArbitrationHandoff(models.Model):
    """Typed NER provenance; never review authority or another decision."""

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE)
    review_batch = models.ForeignKey(ReviewBatch, null=True, on_delete=models.SET_NULL)
    ner_arbitration = models.ForeignKey(
        'classification_disagreements.NerArbitrationTask', null=True, on_delete=models.SET_NULL
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    review_batch_id_frozen = models.PositiveBigIntegerField()
    submission_id_frozen = models.PositiveBigIntegerField()
    arbitration_id_frozen = models.PositiveBigIntegerField()
    actor_id_frozen = models.PositiveBigIntegerField()
    creator_principal = models.CharField(max_length=512)
    creator_source = models.CharField(max_length=128)
    created_at = models.DateTimeField(default=timezone.now)
    origin = models.JSONField()
    origin_hash = models.CharField(max_length=64)
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['project', 'idempotency_key'], name='ner_handoff_project_key_unique'),
            models.UniqueConstraint(
                fields=['project', 'review_batch_id_frozen', 'submission_id_frozen', 'arbitration_id_frozen'],
                name='ner_handoff_binding_unique',
            ),
        ]
        indexes = [
            models.Index(
                fields=['project', 'review_batch_id_frozen', 'submission_id_frozen'], name='ner_handoff_source_idx'
            )
        ]


class FieldReviewArbitrationHandoff(models.Model):
    """Typed field provenance; never review authority or another decision."""

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE)
    review_batch = models.ForeignKey(ReviewBatch, null=True, on_delete=models.SET_NULL)
    field_arbitration = models.ForeignKey(
        'classification_disagreements.FieldArbitrationTask', null=True, on_delete=models.SET_NULL
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    review_batch_id_frozen = models.PositiveBigIntegerField()
    submission_id_frozen = models.PositiveBigIntegerField()
    arbitration_id_frozen = models.PositiveBigIntegerField()
    actor_id_frozen = models.PositiveBigIntegerField()
    creator_principal = models.CharField(max_length=512)
    creator_source = models.CharField(max_length=128)
    created_at = models.DateTimeField(default=timezone.now)
    origin = models.JSONField()
    origin_hash = models.CharField(max_length=64)
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['project', 'idempotency_key'], name='field_handoff_project_key_unique'),
            models.UniqueConstraint(
                fields=['project', 'review_batch_id_frozen', 'submission_id_frozen', 'arbitration_id_frozen'],
                name='field_handoff_binding_unique',
            ),
        ]
        indexes = [
            models.Index(
                fields=['project', 'review_batch_id_frozen', 'submission_id_frozen'], name='field_handoff_source_idx'
            )
        ]
