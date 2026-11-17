"""Dormant version-delete worker; no cloud execution until #144 is implemented.

The mandatory isolation gate is intentionally not a setting or an operator
attestation. Tests may replace it in an isolated process; production cannot
enable this worker until a reviewed, deployment-bound proof replaces the gate.
"""
import re
from datetime import timedelta
from uuid import uuid4

from access_control.authorization import authorization
from access_control.project_operations import (
    SECURITY_CANCEL,
    SYNC_STORAGE,
    lock_project_operation,
    require_project_operation,
)
from core.redis import start_job_async_or_sync
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from io_storages.admission import TERMINAL_STATES, _require_postgres
from io_storages.admission_models import StorageOperation, StorageOperationRecovery
from io_storages.s3.models import S3ExportStorage
from io_storages.s3.version_provenance import (
    inspect_aws_deletion_history,
    lock_s3_export_key,
    put_version,
    storage_binding,
    verify_aws_deletion_target,
)
from projects.models import Project
from rest_framework.exceptions import ValidationError

PRE_EFFECT = 's3_delete_pre_effect_v1'
SUBMITTING = 's3_delete_submitting_v1'


def _require_verified_cloud_isolation():
    # No production implementation of #144's provider-bound isolation and
    # terminal-request proof exists yet. Never replace this with a flag or a
    # caller-supplied boolean: either would permit unsafe cloud deletion.
    raise RuntimeError('Verified cloud isolation and request drain are unavailable.')


def _require_autocommit():
    _require_postgres()
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError('S3 deletion boundaries must commit independently.')


def _require_storage_admission():
    if not getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
        raise ValidationError('Storage admission is disabled.')


def _target(record, storage):
    binding = storage_binding(storage)
    owner = storage.expected_bucket_owner
    if (
        record.storage_model != 'io_storages.s3exportstorage'
        or record.storage_id != storage.pk
        or record.project_id != storage.project_id
        or record.operation != 'annotation_delete_pending'
        or type(record.subject_id) is not int
        or record.subject_id < 1
        or not isinstance(record.target_key, str)
        or not record.target_key
        or len(record.target_key.encode('utf-8')) > 1024
        or put_version({'VersionId': record.target_version_id}) is None
        or not isinstance(record.target_object_token, str)
        or not re.fullmatch(r'[0-9a-f]{32}', record.target_object_token)
        or not isinstance(owner, str)
        or not re.fullmatch(r'[0-9]{12}', owner)
        or not binding
        or not record.target_storage_binding
        or not constant_time_compare(binding, record.target_storage_binding)
    ):
        raise ValidationError('A fully bound S3 deletion target is required.')
    return {
        'operation_id': str(record.pk),
        'project_id': record.project_id,
        'storage_id': record.storage_id,
        'revision': record.lifecycle_revision,
        'dispatch_attempt': record.dispatch_attempts,
        'subject_id': record.subject_id,
        'key': record.target_key,
        'version': record.target_version_id,
        'token': record.target_object_token,
        'binding': binding,
        'owner': owner,
        'bucket': storage.bucket,
    }


def _other_key_work(record):
    unknown = Q(target_key__isnull=True) | Q(target_key='')
    same_key = Q(target_key=record.target_key) | unknown
    operations = StorageOperation.objects.filter(storage_model='io_storages.s3exportstorage').exclude(
        state__in=TERMINAL_STATES
    ).exclude(pk=record.pk).filter(same_key)
    return operations.filter(
        Q(operation='annotation_export')
        | Q(operation='annotation_delete_pending', state__in=(StorageOperation.State.RUNNING, StorageOperation.State.UNCERTAIN))
        | Q(operation='annotation_delete_pending') & unknown
    ).exists()


def _locked_target(operation_id, *, state, phase=None, token=None):
    candidate = StorageOperation.objects.filter(pk=operation_id).values('project_id', 'target_key').first()
    if not candidate or not isinstance(candidate['target_key'], str) or not candidate['target_key']:
        raise ValidationError('A key-bound pending deletion is required.')
    project = Project.objects.select_for_update().get(pk=candidate['project_id'])
    lock_s3_export_key(candidate['target_key'])
    record = StorageOperation.objects.select_for_update().filter(
        pk=operation_id, project=project, state=state, effect_phase=phase, effect_claim_token=token,
        target_key=candidate['target_key'], operation='annotation_delete_pending',
        storage_model='io_storages.s3exportstorage',
    ).first()
    if record is None or record.lifecycle_revision != project.lifecycle_revision:
        raise ValidationError('The deletion claim or lifecycle generation changed.')
    require_project_operation(project, SYNC_STORAGE)
    storage = S3ExportStorage.objects.select_for_update().filter(pk=record.storage_id, project=project).first()
    if storage is None:
        raise ValidationError('The original S3 storage is unavailable.')
    target = _target(record, storage)
    if _other_key_work(record):
        raise ValidationError('Unresolved S3 work blocks deletion of this key.')
    return record, storage, target


def _claim(operation_id, *, dispatch_attempt):
    _require_autocommit()
    if type(dispatch_attempt) is not int or dispatch_attempt < 1:
        raise ValidationError('A valid deletion dispatch attempt is required.')
    with transaction.atomic():
        record, storage, target = _locked_target(operation_id, state=StorageOperation.State.QUEUED)
        if record.dispatch_attempts != dispatch_attempt or record.next_dispatch_at is None:
            raise ValidationError('The deletion dispatch attempt is stale.')
        if record.effect_payload is not None:
            raise ValidationError('An existing deletion attempt requires explicit recovery.')
        claim = uuid4()
        record.state = StorageOperation.State.RUNNING
        record.effect_phase = PRE_EFFECT
        record.effect_claim_token = claim
        record.effect_payload = {'kind': 's3_version_delete_v1', 'target': target, 'attempt': str(claim)}
        record.save(update_fields=['state', 'effect_phase', 'effect_claim_token', 'effect_payload'])
        return storage, target, claim


def _begin_submission(operation_id, claim, target):
    _require_autocommit()
    _require_verified_cloud_isolation()
    _require_storage_admission()
    with transaction.atomic():
        record, _, current = _locked_target(
            operation_id, state=StorageOperation.State.RUNNING, phase=PRE_EFFECT, token=claim
        )
        if current != target or record.effect_payload != {
            'kind': 's3_version_delete_v1', 'target': target, 'attempt': str(claim)
        }:
            raise ValidationError('The claimed S3 deletion target changed.')
        record.effect_phase = SUBMITTING
        record.save(update_fields=['effect_phase'])


def _finish(operation_id, claim, target, response):
    _require_autocommit()
    meta = response.get('ResponseMetadata') if isinstance(response, dict) else None
    if (
        not isinstance(meta, dict)
        or meta.get('HTTPStatusCode') != 204
        or not isinstance(meta.get('RequestId'), str)
        or not re.fullmatch(r'[A-Za-z0-9+/=_-]{1,256}', meta['RequestId'])
        or response.get('VersionId') != target['version']
        or response.get('DeleteMarker', False) is not False
    ):
        raise ValueError('The provider response does not prove the pinned version was deleted.')
    with transaction.atomic():
        record, _, current = _locked_target(
            operation_id, state=StorageOperation.State.RUNNING, phase=SUBMITTING, token=claim
        )
        if current != target or record.effect_payload != {
            'kind': 's3_version_delete_v1', 'target': target, 'attempt': str(claim)
        }:
            raise ValidationError('The submitted S3 deletion target changed.')
        record.state = StorageOperation.State.COMPLETED
        record.finished_at = timezone.now()
        record.effect_payload = {**record.effect_payload, 'provider_request_id': meta['RequestId']}
        record.save(update_fields=['state', 'finished_at', 'effect_payload'])


def _mark_uncertain(operation_id, claim):
    # Never overwrite a newer claim or a recorded success. If DB access fails,
    # RUNNING/SUBMITTING remains blocking and is not automatically retried.
    StorageOperation.objects.filter(
        pk=operation_id, operation='annotation_delete_pending', state=StorageOperation.State.RUNNING,
        effect_phase=SUBMITTING, effect_claim_token=claim,
    ).update(state=StorageOperation.State.UNCERTAIN)


def recover_unsubmitted_delete(project_id, operation_id, principal, *, reason):
    """Fence an old claimant only while its DELETE cannot have been sent."""
    _require_autocommit()
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValidationError('A recovery reason is required.')
    with transaction.atomic():
        project, actor = lock_project_operation(project_id, principal, SECURITY_CANCEL)
        authorization.require(authorization.can_manage_project(principal, project), 'Project manager role is required.')
        candidate = StorageOperation.objects.filter(pk=operation_id, project=project).values('target_key').first()
        if not candidate or not isinstance(candidate['target_key'], str) or not candidate['target_key']:
            raise ValidationError('A key-bound S3 deletion claim is required.')
        lock_s3_export_key(candidate['target_key'])
        record = StorageOperation.objects.select_for_update().filter(
            pk=operation_id, project=project, operation='annotation_delete_pending',
            storage_model='io_storages.s3exportstorage', state=StorageOperation.State.RUNNING,
            effect_phase=PRE_EFFECT, effect_claim_token__isnull=False, target_key=candidate['target_key'],
        ).first()
        if record is None or record.lifecycle_revision != project.lifecycle_revision:
            raise ValidationError('A provably unsubmitted S3 deletion claim is required.')
        require_project_operation(project, SYNC_STORAGE)
        storage = S3ExportStorage.objects.select_for_update().filter(pk=record.storage_id, project=project).first()
        if storage is None:
            raise ValidationError('The original S3 storage is unavailable.')
        target = _target(record, storage)
        if record.effect_payload != {
            'kind': 's3_version_delete_v1', 'target': target, 'attempt': str(record.effect_claim_token)
        }:
            raise ValidationError('The claimed S3 deletion identity changed.')
        StorageOperationRecovery.objects.create(
            operation=record, claim_token=record.effect_claim_token, reason=reason.strip(),
            actor=actor, actor_id_snapshot=actor.pk,
        )
        record.state = StorageOperation.State.QUEUED
        record.effect_phase = None
        record.effect_claim_token = None
        record.effect_payload = None
        record.next_dispatch_at = None
        record.dispatch_attempts += 1
        record.save(update_fields=[
            'state', 'effect_phase', 'effect_claim_token', 'effect_payload', 'next_dispatch_at', 'dispatch_attempts',
        ])
        # No automatic dispatch: production isolation remains unavailable.


def dispatch_versioned_delete(operation_id):
    _require_autocommit()
    _require_verified_cloud_isolation()
    _require_storage_admission()
    with transaction.atomic():
        record, _, _ = _locked_target(operation_id, state=StorageOperation.State.QUEUED)
        if record.effect_payload is not None:
            raise ValidationError('An existing deletion attempt requires explicit recovery.')
        if record.next_dispatch_at is not None and record.next_dispatch_at > timezone.now():
            raise ValidationError('The deletion dispatch reservation is not yet due.')
        record.dispatch_attempts += 1
        record.next_dispatch_at = timezone.now() + timedelta(minutes=5)
        record.save(update_fields=['dispatch_attempts', 'next_dispatch_at'])
        attempt = record.dispatch_attempts
    # A lost enqueue response leaves the durable reservation. A later replay
    # advances the epoch; a delayed worker from the old epoch cannot claim it.
    return start_job_async_or_sync(
        versioned_delete_worker, str(operation_id), dispatch_attempt=attempt, queue_name='low'
    )


def versioned_delete_worker(operation_id, *, dispatch_attempt=None, **kwargs):
    """One attempt; every ambiguous post-submission outcome stays blocking."""
    _require_verified_cloud_isolation()
    _require_storage_admission()
    storage, target, claim = _claim(operation_id, dispatch_attempt=dispatch_attempt)
    client, _ = storage.get_client_and_resource(single_attempt=True)
    record = StorageOperation.objects.get(pk=operation_id)
    verify_aws_deletion_target(storage, record, target['owner'], client)
    history = inspect_aws_deletion_history(storage, record, target['owner'], client)
    if history not in ('noncurrent', 'current_without_other_object_versions'):
        raise ValidationError('Deleting this version could expose an older object.')
    # A preflight is an observation, not a lock. The isolation gate and the
    # project/key ledger fence are both mandatory at the submission boundary.
    _begin_submission(operation_id, claim, target)
    try:
        response = client.delete_object(
            Bucket=target['bucket'], Key=target['key'], VersionId=target['version'],
            ExpectedBucketOwner=target['owner'],
        )
        _finish(operation_id, claim, target, response)
    except BaseException:
        _mark_uncertain(operation_id, claim)
        raise
