"""Bounded redispatch of committed storage outbox entries."""
from django.apps import apps
from django.conf import settings
from io_storages.admission_models import StorageOperation
from rest_framework.exceptions import ValidationError


def redispatch_queued_operation(operation_id, *, expected_attempt=None):
    if not getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
        raise ValidationError('Storage admission is disabled.')
    record = StorageOperation.objects.get(pk=operation_id)
    if record.state != StorageOperation.State.QUEUED:
        raise ValidationError('Storage operation is not queued.')
    if record.operation == 'annotation_export_v3_queued':
        from io_storages.annotation_outbox import dispatch_queued_annotation_export

        return dispatch_queued_annotation_export(record.pk)
    if record.operation == 'webhook_task_created':
        from webhooks.durable import dispatch_webhook

        return dispatch_webhook(record.pk)
    if record.operation in {
        'webhook_annotation_created',
        'webhook_annotation_updated',
        'webhook_annotations_deleted',
        'webhook_tasks_deleted',
        'webhook_project_created',
        'webhook_project_updated',
        'webhook_label_link_created',
        'webhook_label_link_updated',
        'webhook_label_link_deleted',
    }:
        from webhooks.durable import dispatch_project_event

        return dispatch_project_event(record.pk)
    if record.operation not in {'import_sync', 'export_sync', 'export_sync_new'}:
        raise ValidationError('This operation requires manual reconciliation.')
    if type(expected_attempt) is not int or expected_attempt < 0 or record.dispatch_attempts != expected_attempt:
        raise ValidationError({'reason': 'STALE_STORAGE_DISPATCH_ATTEMPT'})
    from core.redis import start_job_async_or_sync
    from io_storages.base_models import ExportStorage, ImportStorage, storage_sync_v2_failure, storage_sync_v3

    storage_class = apps.get_model(record.storage_model)
    expected_base = ImportStorage if record.operation == 'import_sync' else ExportStorage
    if storage_class is None or not issubclass(storage_class, expected_base):
        raise ValidationError('Storage type does not match the queued operation.')
    from io_storages.admission import QUEUE_OWNER_META_KEY, record_storage_dispatch_job

    # Avoid repeatedly placing a known unowned legacy queue entry onto the
    # broker. This observation is only a preflight; the worker still rechecks
    # under locks because the owner may change immediately afterwards.
    storage = storage_class.objects.filter(
        pk=record.storage_id,
        project_id=record.project_id,
        status='queued',
        **{f'meta__{QUEUE_OWNER_META_KEY}': str(record.pk)},
    ).first()
    if storage is None:
        raise ValidationError({'reason': 'STORAGE_QUEUE_OWNERSHIP_UNVERIFIED'})
    # Use stored IDs, not caller-provided project or storage fields. Claim will
    # revalidate project, storage, operation, generation and policy atomically.
    job = start_job_async_or_sync(
        storage_sync_v3,
        storage_class,
        record.storage_id,
        str(record.pk),
        record.operation,
        expected_attempt,
        queue_name='low',
        job_timeout=settings.RQ_LONG_JOB_TIMEOUT,
        on_failure=storage_sync_v2_failure,
    )
    # Synchronous fallback can return the job's result rather than an RQ job.
    # A delayed enqueue response must never overwrite a newer owner or a
    # cancelled/completed operation's metadata.
    job_id = getattr(job, 'id', None)
    if isinstance(job_id, str) and job_id:
        record_storage_dispatch_job(storage, record.pk, job_id, dispatch_attempt=expected_attempt)
    return job
