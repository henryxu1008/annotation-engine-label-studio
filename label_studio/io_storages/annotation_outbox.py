"""Experimental transactional outbox for built-in annotation exports."""
from datetime import timedelta

from access_control.project_operations import SYNC_STORAGE, require_project_operation
from core.redis import start_job_async_or_sync
from data_export.boundary import has_immutable_release_boundary
from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from io_storages.admission import (
    TERMINAL_STATES,
    _require_postgres,
    begin_claimed_storage_effect,
    claim_storage_dispatch,
    finish_storage_operation,
    require_no_pending_deletion,
)
from io_storages.admission_models import StorageOperation
from projects.models import Project


def queue_annotation_exports(annotation, storage_class):
    """Write outbox entries in the source transaction; no network before commit."""
    _require_postgres()
    # Signals can run after a task/annotation lock. NOWAIT fails closed instead
    # of waiting in the opposite lock order when a transition holds Project.
    with transaction.atomic():
        project = Project.objects.select_for_update(nowait=True).get(pk=annotation.project_id)
        storage_ids = list(storage_class.objects.filter(project=project).values_list('pk', flat=True))
        if not storage_ids or has_immutable_release_boundary(project.pk):
            return
        require_project_operation(project, SYNC_STORAGE)
        for storage_id in storage_ids:
            require_no_pending_deletion(project, storage_class._meta.label_lower, storage_id)
            record = StorageOperation.objects.create(
                project=project,
                storage_model=storage_class._meta.label_lower,
                storage_id=storage_id,
                operation='annotation_export_v3_queued',
                lifecycle_revision=project.lifecycle_revision,
                subject_id=annotation.pk,
                state=StorageOperation.State.QUEUED,
            )
            transaction.on_commit(lambda pk=record.pk: dispatch_queued_annotation_export(pk), robust=True)


def queue_annotation_deletion(
    annotation, storage, target_key, *, target_version=None, target_binding=None, target_token=None
):
    """Record deletion before source commit; actual provider deletion is manual."""
    _require_postgres()
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('Annotation deletion requires a database transaction')
    project = Project.objects.select_for_update(nowait=True).get(pk=annotation.project_id)
    require_project_operation(project, SYNC_STORAGE)
    if storage.project_id != project.pk:
        raise ValueError('Storage project mismatch')
    if (
        StorageOperation.objects.filter(
            project=project,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
        )
        .exclude(state__in=TERMINAL_STATES)
        .exists()
    ):
        raise ValueError('Pending storage work must be resolved before annotation deletion')
    if storage._meta.label_lower == 'io_storages.s3exportstorage':
        from io_storages.s3.version_provenance import require_s3_deletion_admission

        # Projects and storage rows are not unique per bucket/key. Hold the
        # same key lock used by PUT admission until this intent commits.
        require_s3_deletion_admission(target_key)
    return StorageOperation.objects.create(
        project=project,
        storage_model=storage._meta.label_lower,
        storage_id=storage.pk,
        operation='annotation_delete_pending',
        lifecycle_revision=project.lifecycle_revision,
        subject_id=annotation.pk,
        target_key=str(target_key),
        target_version_id=target_version,
        target_storage_binding=target_binding,
        target_object_token=target_token,
        state=StorageOperation.State.QUEUED,
    )


def dispatch_queued_annotation_export(operation_id):
    """Re-dispatch is safe: the database claim permits at most one execution."""
    if not getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
        raise RuntimeError('Storage admission must be enabled to dispatch outbox entries')
    if not StorageOperation.objects.filter(
        pk=operation_id,
        operation='annotation_export_v3_queued',
        state=StorageOperation.State.QUEUED,
    ).exists():
        raise ValueError('A queued v3 annotation export is required.')
    return start_job_async_or_sync(annotation_export_v4, str(operation_id), queue_name='low')


def annotation_export_v2(operation_id, **kwargs):
    """Retired queue entry: old records require isolation and explicit disposition."""
    raise ValueError('Legacy annotation export jobs cannot run on this worker.')


def annotation_export_v3(operation_id, **kwargs):
    """New entry and ledger operation cannot be claimed by a v2 worker."""
    return _annotation_export(operation_id, fenced=False)


def annotation_export_v4(operation_id, **kwargs):
    return _annotation_export(operation_id, fenced=True)


def _annotation_export(operation_id, *, fenced):
    from io_storages.base_models import ExportStorage
    from tasks.models import Annotation

    if not getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
        raise ValueError('Storage admission is disabled')
    record = StorageOperation.objects.get(pk=operation_id)
    storage_class = apps.get_model(record.storage_model)
    if storage_class is None or not issubclass(storage_class, ExportStorage):
        raise ValueError('Invalid outbox storage type')
    storage = storage_class.objects.get(pk=record.storage_id, project_id=record.project_id)
    record = claim_storage_dispatch(storage, record.pk, 'annotation_export_v3_queued', fenced=fenced)
    storage._storage_lifecycle_revision = record.lifecycle_revision
    if fenced:
        begin_claimed_storage_effect(record)
    try:
        annotation = Annotation.objects.filter(pk=record.subject_id, project_id=record.project_id).first()
        if annotation is not None:
            storage.save_annotation(annotation)
    except BaseException as exc:
        from io_storages.s3.version_provenance import S3ExportTargetBusy

        if isinstance(exc, S3ExportTargetBusy) and getattr(exc, '_s3_cancelled_attempt', None):
            # The child attempt was rejected and cancelled before submission.
            # Keep its original queued parent for the bounded redispatch loop.
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=record.project_id)
                cancelled = StorageOperation.objects.filter(
                    pk=exc._s3_cancelled_attempt,
                    project_id=record.project_id,
                    storage_model=record.storage_model,
                    storage_id=record.storage_id,
                    operation='annotation_export',
                    state=StorageOperation.State.CANCELLED,
                    subject_id=record.subject_id,
                    effect_payload={'issue86_s3_preflight': 'v2'},
                    target_key__isnull=False,
                    target_object_token__isnull=True,
                ).exists()
                if not cancelled:
                    raise RuntimeError('Cannot verify the blocked S3 export attempt.')
                updated = StorageOperation.objects.filter(
                    pk=record.pk,
                    state=StorageOperation.State.RUNNING,
                    operation='annotation_export_v3_queued',
                ).update(
                    state=StorageOperation.State.QUEUED, next_dispatch_at=timezone.now() + timedelta(minutes=5),
                    effect_phase=None, effect_claim_token=None,
                )
                if updated != 1:
                    raise RuntimeError('Cannot requeue the blocked S3 export.')
            return
        finish_storage_operation(record.pk, succeeded=False)
        raise
    else:
        finish_storage_operation(record.pk, succeeded=True)
