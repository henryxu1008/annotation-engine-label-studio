"""Frozen local work plans and input facts, never a second decision ledger."""
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
