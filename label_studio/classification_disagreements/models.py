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


class NerBatch(models.Model):
    """Independent NER evidence; never a classification or review decision."""

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='ner_batches')
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
            models.UniqueConstraint(fields=['project', 'idempotency_key'], name='ner_batch_project_key_unique')
        ]


class FieldBatch(models.Model):
    """Independent field evidence; never a classification or review decision."""

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='field_batches')
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
            models.UniqueConstraint(fields=['project', 'idempotency_key'], name='field_batch_project_key_unique')
        ]


class BboxQualityBatch(models.Model):
    """Immutable local quality evidence; not a review or release decision."""

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='bbox_quality_batches')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    creator_principal = models.CharField(max_length=512)
    creator_source = models.CharField(max_length=128)
    actor_id_frozen = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    name = models.CharField(max_length=160)
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    manifest = models.JSONField()
    manifest_hash = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['project', 'idempotency_key'],
                                               name='bbox_quality_project_key_unique')]


class BboxArbitrationTask(models.Model):
    """Local independent adoption workflow, never a Host review decision."""

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE)
    batch = models.ForeignKey(BboxQualityBatch, on_delete=models.RESTRICT)
    task_id_frozen = models.PositiveBigIntegerField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                   related_name='created_bbox_arbitrations')
    actor_id_frozen = models.PositiveBigIntegerField()
    creator_principal = models.CharField(max_length=512)
    creator_source = models.CharField(max_length=128)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                 related_name='bbox_arbitrations')
    assignee_id_frozen = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    input_hash = models.CharField(max_length=64)
    origin = models.JSONField()
    origin_hash = models.CharField(max_length=64)
    participant_ids = models.JSONField()
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, default='pending')
    version = models.PositiveIntegerField(default=1)
    cancel_key = models.UUIDField(null=True)
    cancel_request_hash = models.CharField(max_length=64, default='', blank=True)
    cancellation = models.JSONField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['project', 'idempotency_key'], name='bbox_arb_project_key_unique'),
            models.UniqueConstraint(fields=['project', 'task_id_frozen', 'input_hash'],
                                    condition=models.Q(status__in=['pending', 'resolved']), name='bbox_arb_input_unique'),
            models.UniqueConstraint(fields=['project', 'cancel_key'], name='bbox_arb_cancel_key_unique'),
            models.CheckConstraint(condition=(models.Q(status='pending', version=1, cancel_key__isnull=True,
                                                       cancellation__isnull=True, cancel_request_hash='') |
                                              models.Q(status='resolved', version=2, cancel_key__isnull=True,
                                                       cancellation__isnull=True, cancel_request_hash='') |
                                              models.Q(status='cancelled', version=2, cancel_key__isnull=False,
                                                       cancellation__isnull=False) & ~models.Q(cancel_request_hash='')),
                                   name='bbox_arb_state_valid'),
        ]


class BboxArbitrationDecision(models.Model):
    """Adopts an exact frozen submission; no annotation or release write."""

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE)
    arbitration = models.OneToOneField(BboxArbitrationTask, on_delete=models.CASCADE, related_name='decision')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    actor_id_frozen = models.PositiveBigIntegerField()
    principal = models.CharField(max_length=512)
    source = models.CharField(max_length=128)
    created_at = models.DateTimeField(default=timezone.now)
    input_hash = models.CharField(max_length=64)
    winner = models.JSONField()
    reason = models.TextField()
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    payload_hash = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['project', 'idempotency_key'], name='bbox_decision_project_key_unique')]


class BboxMediaAttempt(models.Model):
    """Durable fence for candidate publication and repeatable reconciliation."""

    class Status(models.TextChoices):
        ACTIVE = 'active'
        PUBLISHED = 'published'
        ABANDONED = 'abandoned'

    token = models.UUIDField(primary_key=True, editable=False)
    task = models.ForeignKey('tasks.Task', null=True, on_delete=models.SET_NULL, related_name='bbox_media_attempts')
    project = models.ForeignKey('projects.Project', null=True, on_delete=models.SET_NULL)
    task_id_frozen = models.PositiveBigIntegerField()
    project_id_frozen = models.PositiveBigIntegerField()
    actor_id_frozen = models.PositiveBigIntegerField()
    storage_binding = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    last_reconciled_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['task'], condition=models.Q(status='active'),
                                               name='bbox_one_active_attempt_per_task')]


class BboxMediaPin(models.Model):
    """Server-owned media evidence; the stored reference is private."""

    task = models.OneToOneField('tasks.Task', on_delete=models.CASCADE, related_name='bbox_media_pin')
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE)
    token = models.UUIDField(unique=True)
    actor_id_frozen = models.PositiveBigIntegerField()
    field = models.CharField(max_length=255)
    source = models.JSONField()
    task_data_hash = models.CharField(max_length=64)
    config_hash = models.CharField(max_length=64)
    artifact_key = models.CharField(max_length=255)
    storage_binding = models.CharField(max_length=64)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveBigIntegerField()
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)


class BboxSubmissionProof(models.Model):
    """Proof for one immutable revision, created in the submission transaction."""

    submission = models.OneToOneField('tasks.Submission', on_delete=models.CASCADE, related_name='bbox_media_proof')
    media = models.ForeignKey(BboxMediaPin, on_delete=models.RESTRICT, related_name='submission_proofs')
    assignment_id_frozen = models.PositiveBigIntegerField()
    revision = models.PositiveIntegerField()
    result_hash = models.CharField(max_length=64)
    proof_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)


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


class NerArbitrationTask(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending'
        RESOLVED = 'resolved'
        CANCELLED = 'cancelled'

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='ner_arbitrations')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name='created_ner_arbitrations',
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='ner_arbitrations'
    )
    creator_principal = models.CharField(max_length=512)
    creator_source = models.CharField(max_length=128)
    actor_id_frozen = models.PositiveBigIntegerField()
    assignee_id_frozen = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    ner_batch_id_frozen = models.PositiveBigIntegerField()
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
            models.UniqueConstraint(fields=['project', 'idempotency_key'], name='ner_arb_project_create_key_unique'),
            models.UniqueConstraint(fields=['project', 'cancel_key'], name='ner_arb_project_cancel_key_unique'),
            models.UniqueConstraint(
                fields=['project', 'task_id_frozen', 'input_set_hash'],
                condition=models.Q(status__in=['pending', 'resolved']),
                name='ner_arb_active_input_unique',
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
                name='ner_arb_task_state_valid',
            ),
        ]


class NerArbitrationDecision(models.Model):
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE)
    arbitration = models.OneToOneField(NerArbitrationTask, on_delete=models.CASCADE, related_name='decision')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    actor_id_frozen = models.PositiveBigIntegerField()
    principal = models.CharField(max_length=512)
    source = models.CharField(max_length=128)
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
            models.UniqueConstraint(fields=['project', 'idempotency_key'], name='ner_arb_project_decision_key_unique')
        ]


class FieldArbitrationTask(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending'
        RESOLVED = 'resolved'
        CANCELLED = 'cancelled'

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='field_arbitrations')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name='created_field_arbitrations',
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='field_arbitrations'
    )
    creator_principal = models.CharField(max_length=512)
    creator_source = models.CharField(max_length=128)
    actor_id_frozen = models.PositiveBigIntegerField()
    assignee_id_frozen = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    field_batch_id_frozen = models.PositiveBigIntegerField()
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
            models.UniqueConstraint(fields=['project', 'idempotency_key'], name='field_arb_project_create_key_unique'),
            models.UniqueConstraint(fields=['project', 'cancel_key'], name='field_arb_project_cancel_key_unique'),
            models.UniqueConstraint(
                fields=['project', 'task_id_frozen', 'input_set_hash'],
                condition=models.Q(status__in=['pending', 'resolved']),
                name='field_arb_active_input_unique',
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
                name='field_arb_task_state_valid',
            ),
        ]


class FieldArbitrationDecision(models.Model):
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE)
    arbitration = models.OneToOneField(FieldArbitrationTask, on_delete=models.CASCADE, related_name='decision')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    actor_id_frozen = models.PositiveBigIntegerField()
    principal = models.CharField(max_length=512)
    source = models.CharField(max_length=128)
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
            models.UniqueConstraint(
                fields=['project', 'idempotency_key'], name='field_arb_project_decision_key_unique'
            )
        ]
