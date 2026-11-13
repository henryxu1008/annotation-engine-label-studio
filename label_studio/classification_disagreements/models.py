"""Frozen classification analyses and local arbitration receipts, never review approvals."""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class BlindProvenanceFence(models.Model):
    """One transactional ordering point for prospective provenance events."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    sequence = models.PositiveBigIntegerField(default=0)


class BlindProvenanceEvent(models.Model):
    """Internal database-captured ordering evidence, not proof of no exposure."""

    kind = models.CharField(max_length=12)
    subject_id = models.PositiveBigIntegerField()
    sequence = models.PositiveBigIntegerField(unique=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['kind', 'subject_id'], name='blind_provenance_subject_unique'),
            models.CheckConstraint(condition=models.Q(kind__in=['account', 'reference']),
                                   name='blind_provenance_kind_valid'),
        ]


class BlindCaptureEpoch(models.Model):
    """Fail-closed capture generation, not an authorization switch."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    generation = models.PositiveBigIntegerField(default=1)
    enabled = models.BooleanField(default=False)


class BlindPrivilegeHistory(models.Model):
    """Forward-only conservative privilege history; absence denies admission."""

    account_id = models.PositiveBigIntegerField(primary_key=True)
    elevated = models.BooleanField(default=False)
    had_assignment = models.BooleanField(default=False)
    first_assignment_id = models.PositiveBigIntegerField(null=True)

    class Meta:
        constraints = [models.CheckConstraint(
            condition=(models.Q(had_assignment=False, first_assignment_id__isnull=True)
                       | models.Q(had_assignment=True, first_assignment_id__isnull=False)),
            name='blind_first_assignment_consistent',
        )]


class BlindCaptureCertificate(models.Model):
    """Account birth observed while capture was enabled for this generation."""

    account_id = models.PositiveBigIntegerField(primary_key=True)
    generation = models.PositiveBigIntegerField()


class BlindAssignmentExposure(models.Model):
    """Forward-only task assignments observed during a certified generation."""

    account_id = models.PositiveBigIntegerField()
    task_id = models.PositiveBigIntegerField()
    project_id = models.PositiveBigIntegerField()
    generation = models.PositiveBigIntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['account_id', 'task_id', 'generation'], name='blind_assignment_exposure_unique',
        )]


class BlindProjectFileExposure(models.Model):
    """Prospective record that an account accessed project-controlled file bytes."""

    account_id = models.PositiveBigIntegerField()
    project_id = models.PositiveBigIntegerField()
    generation = models.PositiveBigIntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['account_id', 'project_id', 'generation'], name='blind_project_file_exposure_unique',
        )]


class BlindLabelExposure(models.Model):
    """Prospective record of organization-wide label metadata access."""

    account_id = models.PositiveBigIntegerField()
    generation = models.PositiveBigIntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['account_id', 'generation'], name='blind_label_exposure_unique',
        )]


class BlindTextLengthExposure(models.Model):
    """Conservative, non-content evidence of assigned top-level text lengths."""

    account_id = models.PositiveBigIntegerField()
    assignment_id = models.PositiveBigIntegerField()
    generation = models.PositiveBigIntegerField()
    byte_length = models.IntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['account_id', 'assignment_id', 'generation', 'byte_length'], name='blind_text_length_unique',
        )]


class BlindReferenceTextLength(models.Model):
    reference_id = models.PositiveBigIntegerField()
    byte_length = models.PositiveIntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['reference_id', 'byte_length'], name='blind_reference_length_unique',
        )]


class BlindEligibilityProof(models.Model):
    """Immutable assignment-time admission evidence; contains no answers."""

    assignment_id = models.PositiveBigIntegerField(unique=True)
    manifest = models.JSONField()
    manifest_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)


class BlindQualityAssignment(models.Model):
    """Private retained purpose evidence, including after project deletion."""

    assignment_id = models.PositiveBigIntegerField(unique=True)
    task_id = models.PositiveBigIntegerField(db_index=True)
    project_id = models.PositiveBigIntegerField(db_index=True)
    assignee_id = models.PositiveBigIntegerField()
    reference_id = models.PositiveBigIntegerField(null=True)
    reference_hash = models.CharField(max_length=64, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(reference_id__isnull=True, reference_hash__isnull=True)
                           | models.Q(reference_id__isnull=False, reference_hash__isnull=False)),
                name='blind_quality_reference_pair',
            ),
        ]


class BlindGoldScore(models.Model):
    """Internal frozen score for one quality revision; no public endpoint."""

    submission = models.OneToOneField('tasks.Submission', on_delete=models.CASCADE, related_name='blind_gold_score')
    manifest = models.JSONField()
    manifest_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)


class BlindScoreOutbox(models.Model):
    """Private uniform queue record for submissions in a quality project."""

    submission = models.OneToOneField('tasks.Submission', on_delete=models.CASCADE, related_name='blind_score_outbox')
    token = models.UUIDField(default=uuid.uuid4)
    state = models.CharField(max_length=16, default='pending')
    queued_at = models.DateTimeField(null=True)
    updated_at = models.DateTimeField(auto_now=True)
    error_code = models.CharField(max_length=64, default='', blank=True)

    class Meta:
        constraints = [models.CheckConstraint(
            condition=models.Q(state__in=['pending', 'queued', 'scored', 'ignored', 'invalid']),
            name='blind_score_outbox_state_valid',
        )]


class BlindDispatchReceipt(models.Model):
    """Private idempotency record for an initial quality assignment."""

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE)
    assignment = models.OneToOneField('tasks.TaskAssignment', null=True, on_delete=models.SET_NULL)
    assignment_id_frozen = models.PositiveBigIntegerField()
    key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    actor_id = models.PositiveBigIntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['project', 'key'], name='blind_dispatch_project_key_unique')]


class BlindMixDecision(models.Model):
    """Private persisted sampling decision for one ordinary assignment command."""

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE)
    source_assignment = models.ForeignKey('tasks.TaskAssignment', null=True, on_delete=models.SET_NULL,
                                          related_name='+')
    chosen_assignment = models.ForeignKey('tasks.TaskAssignment', null=True, on_delete=models.SET_NULL,
                                          related_name='+')
    source_id_frozen = models.PositiveBigIntegerField()
    source_task_id_frozen = models.PositiveBigIntegerField()
    source_assignee_id_frozen = models.PositiveBigIntegerField()
    chosen_id_frozen = models.PositiveBigIntegerField()
    reference_id_frozen = models.PositiveBigIntegerField(null=True)
    predecessor_id_frozen = models.PositiveBigIntegerField(null=True, unique=True)
    key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    actor_id = models.PositiveBigIntegerField()
    ratio_basis_points = models.PositiveSmallIntegerField()
    selected = models.BooleanField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['project', 'key'], name='blind_mix_project_key_unique'),
            models.UniqueConstraint(fields=['project', 'source_id_frozen'], name='blind_mix_source_unique'),
            models.UniqueConstraint(fields=['chosen_id_frozen'], name='blind_mix_chosen_unique'),
            models.CheckConstraint(condition=models.Q(ratio_basis_points__lte=10000), name='blind_mix_ratio_valid'),
            models.CheckConstraint(condition=(models.Q(selected=False, reference_id_frozen__isnull=True)
                                             | models.Q(selected=True, reference_id_frozen__isnull=False)),
                                   name='blind_mix_reference_valid'),
        ]


class BlindMixEvidence(models.Model):
    """Retained proof that a sampled assignment came from a mix decision."""

    decision_id_frozen = models.PositiveBigIntegerField(unique=True)
    project_id_frozen = models.PositiveBigIntegerField()
    source_id_frozen = models.PositiveBigIntegerField()
    source_task_id_frozen = models.PositiveBigIntegerField()
    source_assignee_id_frozen = models.PositiveBigIntegerField()
    chosen_id_frozen = models.PositiveBigIntegerField(unique=True)


class BlindMixCompletion(models.Model):
    """Retained evidence that a sample submitted before its source was released."""

    decision_id_frozen = models.PositiveBigIntegerField(unique=True)
    project_id_frozen = models.PositiveBigIntegerField()
    source_id_frozen = models.PositiveBigIntegerField()
    source_version_after = models.PositiveBigIntegerField()
    chosen_id_frozen = models.PositiveBigIntegerField(unique=True)
    submission_id_frozen = models.PositiveBigIntegerField(unique=True)


class BlindTaskHandle(models.Model):
    """Retained opaque participant identifiers for one assignment, not a public API."""

    assignment_id_frozen = models.PositiveBigIntegerField(unique=True)
    task_id_frozen = models.PositiveBigIntegerField()
    project_id_frozen = models.PositiveBigIntegerField()
    actor_id_frozen = models.PositiveBigIntegerField()
    task_handle = models.PositiveBigIntegerField(unique=True)
    assignment_handle = models.PositiveBigIntegerField(unique=True)


class BlindResourceHandle(models.Model):
    """Immutable participant token bound to one concrete draft or annotation."""

    assignment_id_frozen = models.PositiveBigIntegerField()
    task_id_frozen = models.PositiveBigIntegerField()
    project_id_frozen = models.PositiveBigIntegerField()
    actor_id_frozen = models.PositiveBigIntegerField()
    kind = models.CharField(max_length=16)
    object_id_frozen = models.PositiveBigIntegerField()
    handle = models.PositiveBigIntegerField(unique=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['kind', 'object_id_frozen'], name='blind_resource_object_unique'),
            models.CheckConstraint(condition=models.Q(kind__in=['annotation', 'draft']), name='blind_resource_kind_valid'),
        ]


class ClassificationGoldEvidence(models.Model):
    """Append-only manager-curated reference or exact-revision score."""

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='classification_gold_evidence')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    actor_id_frozen = models.PositiveBigIntegerField()
    principal = models.CharField(max_length=512)
    principal_source = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    kind = models.CharField(max_length=12)
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    manifest = models.JSONField()
    manifest_hash = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['project', 'idempotency_key'], name='classification_gold_project_key_unique'),
            models.CheckConstraint(condition=models.Q(kind__in=['reference', 'score']), name='classification_gold_kind_valid'),
        ]


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
