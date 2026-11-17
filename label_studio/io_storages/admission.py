"""Project-first admission ledger for external storage effects.

These primitives are not yet a complete storage fence: callers must enroll all
execution paths, and a transition must call lock_drained_project in its own
transaction. Do not enable lifecycle enforcement until those conditions hold.
"""
from contextlib import contextmanager
from functools import wraps
from uuid import UUID

from access_control.authorization import authorization
from access_control.project_operations import (
    SECURITY_CANCEL,
    SYNC_STORAGE,
    lock_project_operation,
    require_project_operation,
)
from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from io_storages.admission_models import StorageOperation, StorageOperationRecovery
from projects.models import Project
from rest_framework.exceptions import PermissionDenied, ValidationError

TERMINAL_STATES = (
    StorageOperation.State.COMPLETED,
    StorageOperation.State.RECONCILED,
    StorageOperation.State.CANCELLED,
)
CLOUD_STORAGE_MODELS = frozenset(
    {
        'io_storages.s3importstorage',
        'io_storages.s3exportstorage',
        'io_storages.gcsimportstorage',
        'io_storages.gcsexportstorage',
        'io_storages.azureblobimportstorage',
        'io_storages.azureblobexportstorage',
    }
)
MANUAL_RECONCILIATION_MODELS = frozenset(
    {
        'io_storages.localfilesimportstorage',
        'io_storages.localfilesexportstorage',
        'webhooks.webhook',
    }
)
VERSIONED_IMPORT_MODELS = frozenset(
    {
        'io_storages.localfilesimportstorage',
        'io_storages.redisimportstorage',
        'io_storages.s3importstorage',
        'io_storages.gcsimportstorage',
        'io_storages.azureblobimportstorage',
    }
)
VERSIONED_EXPORT_MODELS = frozenset(
    {
        'io_storages.localfilesexportstorage',
        'io_storages.redisexportstorage',
        'io_storages.s3exportstorage',
        'io_storages.gcsexportstorage',
        'io_storages.azureblobexportstorage',
    }
)
LEGACY_STORAGE_MODELS = {
    'storage_local_import': 'io_storages.localfilesimportstorage',
    'storage_local_export': 'io_storages.localfilesexportstorage',
    'storage_redis_import': 'io_storages.redisimportstorage',
    'storage_redis_export': 'io_storages.redisexportstorage',
    'storage_s3_import': 'io_storages.s3importstorage',
    'storage_s3_export': 'io_storages.s3exportstorage',
    'storage_gcs_import': 'io_storages.gcsimportstorage',
    'storage_gcs_export': 'io_storages.gcsexportstorage',
    'storage_azure_import': 'io_storages.azureblobimportstorage',
    'storage_azure_export': 'io_storages.azureblobexportstorage',
}
LEGACY_STORAGE_SETTLED_STATUSES = frozenset({'initialized', 'completed'})
QUEUE_OWNER_META_KEY = '_issue86_queue_owner'
QUEUE_JOB_ATTEMPT_META_KEY = '_issue86_job_attempt'
SYNC_OPERATIONS = ('import_sync', 'export_sync', 'export_sync_new')
STORAGE_PRE_EFFECT = 'storage_pre_effect_v1'
STORAGE_EFFECT_STARTED = 'storage_effect_started_v1'


def require_manual_reconciliation_supported(record):
    """Reject unverified cloud and unknown providers; known local/webhook claims remain explicit."""
    if record.storage_model in CLOUD_STORAGE_MODELS:
        raise ValidationError({'reason': 'CLOUD_RECONCILIATION_UNAVAILABLE'})
    from webhooks.durable import PROJECT_EVENTS

    allowed = (
        (record.storage_model == 'io_storages.localfilesimportstorage' and record.operation == 'import_sync')
        or (
            record.storage_model == 'io_storages.localfilesexportstorage'
            and record.operation
            in {
                'export_sync',
                'export_sync_new',
                'annotation_export',
                'annotation_export_queued',
                'annotation_export_v3_queued',
                'annotation_delete_pending',
            }
        )
        or (
            record.storage_model == 'webhooks.webhook'
            and record.operation in {'webhook_task_created', *PROJECT_EVENTS.values()}
        )
    )
    if record.storage_model not in MANUAL_RECONCILIATION_MODELS or not allowed:
        raise ValidationError({'reason': 'VERIFIED_RECONCILIATION_UNAVAILABLE'})


def require_no_pending_deletion(project, storage_model, storage_id):
    if (
        StorageOperation.objects.filter(
            project=project,
            storage_model=storage_model,
            storage_id=storage_id,
            operation='annotation_delete_pending',
        )
        .exclude(state__in=TERMINAL_STATES)
        .exists()
    ):
        raise ValidationError({'reason': 'STORAGE_DELETION_PENDING'})


def _require_postgres():
    if connection.vendor != 'postgresql':
        raise RuntimeError('Durable storage admission requires PostgreSQL row locks')


def begin_storage_operation(
    project_id,
    storage_model,
    storage_id,
    operation,
    *,
    expected_revision=None,
    s3_identity_pending=False,
    s3_preflight_key=None,
    s3_subject_id=None,
):
    """Commit admission before any external effect; never join an outer transaction."""
    _require_postgres()
    if connection.in_atomic_block:
        raise RuntimeError('Storage admission must commit before the external effect')
    if not storage_model or not operation or not isinstance(storage_id, int) or storage_id < 1:
        raise ValueError('Valid storage identity and operation are required')
    if s3_identity_pending and (storage_model != 'io_storages.s3exportstorage' or operation != 'annotation_export'):
        raise ValueError('S3 preflight state is only valid for annotation export.')
    if s3_identity_pending and (
        not isinstance(s3_preflight_key, str)
        or not s3_preflight_key
        or type(s3_subject_id) is not int
        or s3_subject_id < 1
    ):
        raise ValueError('An S3 preflight key and annotation are required.')
    with transaction.atomic():
        project = Project.objects.select_for_update().get(pk=project_id)
        require_project_operation(project, SYNC_STORAGE)
        require_no_pending_deletion(project, storage_model, storage_id)
        if project.lifecycle_state != 'legacy' and expected_revision is None:
            raise ValidationError({'reason': 'LIFECYCLE_GENERATION_REQUIRED'})
        if expected_revision is not None and project.lifecycle_revision != expected_revision:
            raise ValidationError({'reason': 'STALE_LIFECYCLE_GENERATION'})
        if s3_identity_pending:
            from io_storages.s3.version_provenance import lock_s3_export_key

            lock_s3_export_key(s3_preflight_key)
        return StorageOperation.objects.create(
            project=project,
            storage_model=storage_model,
            storage_id=storage_id,
            operation=operation,
            lifecycle_revision=project.lifecycle_revision,
            effect_payload={'issue86_s3_preflight': 'v2'} if s3_identity_pending else None,
            target_key=s3_preflight_key if s3_identity_pending else None,
            subject_id=s3_subject_id if s3_identity_pending else None,
        )


def _legacy_export_queries(project):
    from data_export.approved_models import ApprovedDatasetRelease, ApprovedDatasetReleaseAttempt
    from data_export.audit_models import AuditAttempt, AuditPackage
    from data_export.models import ConvertedFormat, Export
    from data_export.snapshot_models import ReleaseSnapshot
    from django.apps import apps

    # Local storage models are normally imported by URL configuration, not
    # io_storages.models. A worker or management command may evaluate drain
    # before any URL is resolved; register the models independently of HTTP.
    from io_storages.localfiles import models as _localfiles_models  # noqa: F401

    queries = {
        'exports': Export.objects.filter(project=project).exclude(status=Export.Status.COMPLETED),
        'conversions': ConvertedFormat.objects.filter(Q(export__project=project) | Q(project=project)).exclude(
            status=ConvertedFormat.Status.COMPLETED
        ),
        'releases': ApprovedDatasetRelease.objects.filter(project=project).exclude(state='ready'),
        'release_attempts': ApprovedDatasetReleaseAttempt.objects.filter(release__project=project).exclude(
            state='ready'
        ),
        'snapshots': ReleaseSnapshot.objects.filter(project=project).exclude(state='ready'),
        'audit_packages': AuditPackage.objects.filter(project=project).exclude(state='ready'),
        'audit_attempts': AuditAttempt.objects.filter(package__project=project).exclude(state='ready'),
    }
    # Old sync workers update StorageInfo directly and never create a
    # StorageOperation. Failure or a timeout-derived FAILED status is not
    # evidence that the old worker or its provider request has terminated.
    for name, label in LEGACY_STORAGE_MODELS.items():
        storage_model = apps.get_model(label)
        queries[name] = storage_model.objects.filter(project=project).exclude(
            status__in=LEGACY_STORAGE_SETTLED_STATUSES
        )
    return queries


def legacy_export_blocker_counts(project):
    """Read-only inventory; counts do not prove old processes are isolated."""
    return {name: query.count() for name, query in _legacy_export_queries(project).items()}


def legacy_export_blocker_details(project, *, limit=50, cursor=None):
    """Keyset page of project-scoped identifiers and states, never receipt payloads."""
    if not 1 <= limit <= 100:
        raise ValueError('A bounded inventory limit is required.')
    queries = _legacy_export_queries(project)
    start_kind, start_pk = None, None
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or not cursor or len(cursor) > 2048:
                raise ValueError('Invalid cursor')
            payload = signing.loads(cursor, salt='issue86.legacy-export-page.v1', max_age=3600)
            if (
                not isinstance(payload, dict)
                or set(payload) != {'version', 'project', 'kind', 'id'}
                or payload['version'] != 1
                or type(payload['project']) is not int
                or payload['project'] != project.pk
                or payload['kind'] not in queries
                or not isinstance(payload['id'], str)
                or len(payload['id']) > 64
            ):
                raise ValueError('Invalid cursor')
            start_kind = payload['kind']
            start_pk = queries[start_kind].model._meta.pk.to_python(payload['id'])
            if (
                str(start_pk) != payload['id']
                or isinstance(start_pk, int)
                and not 1 <= start_pk <= 9223372036854775807
            ):
                raise ValueError('Invalid cursor')
        except (signing.BadSignature, ValueError, TypeError, DjangoValidationError, KeyError):
            raise ValidationError('Invalid or expired legacy export cursor.') from None
    entries = []
    started = start_kind is None
    for name, query in queries.items():
        if not started:
            if name != start_kind:
                continue
            started = True
            query = query.filter(pk__gt=start_pk)
        state_field = 'status' if name in ('exports', 'conversions') or name.startswith('storage_') else 'state'
        remaining = limit + 1 - len(entries)
        if remaining <= 0:
            break
        parent_field = {
            'conversions': 'export_id',
            'release_attempts': 'release_id',
            'snapshots': 'release_id',
            'audit_attempts': 'package_id',
        }.get(name)
        fields = ('pk', state_field, parent_field) if parent_field else ('pk', state_field)
        rows = query.order_by('pk').values_list(*fields)[:remaining]
        for row in rows:
            parent_id = str(row[2]) if parent_field and row[2] is not None else None
            entries.append({'kind': name, 'id': str(row[0]), 'state': row[1], 'parent_id': parent_id})
    has_more = len(entries) > limit
    page = entries[:limit]
    next_cursor = (
        signing.dumps(
            {
                'version': 1,
                'project': project.pk,
                'kind': page[-1]['kind'],
                'id': page[-1]['id'],
            },
            salt='issue86.legacy-export-page.v1',
        )
        if has_more
        else None
    )
    return {'items': page, 'limit': limit, 'possibly_truncated': has_more, 'next_cursor': next_cursor}


def storage_operation_page(project, *, limit=100, cursor=None):
    """Page outstanding ledger entries with a project-bound, time-limited cursor."""
    if not 1 <= limit <= 100:
        raise ValueError('A bounded inventory limit is required.')
    query = StorageOperation.objects.filter(project=project).exclude(state__in=TERMINAL_STATES)
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or not cursor or len(cursor) > 2048:
                raise ValueError('Invalid cursor')
            payload = signing.loads(cursor, salt='issue86.storage-operation-page.v1', max_age=3600)
            if (
                not isinstance(payload, dict)
                or set(payload) != {'version', 'project', 'started_at', 'id'}
                or payload['version'] != 1
                or type(payload['project']) is not int
                or payload['project'] != project.pk
                or not isinstance(payload['started_at'], str)
                or len(payload['started_at']) > 64
                or not isinstance(payload['id'], str)
            ):
                raise ValueError('Invalid cursor')
            started_at = parse_datetime(payload['started_at'])
            operation_id = UUID(payload['id'])
            if (
                started_at is None
                or timezone.is_aware(started_at) != settings.USE_TZ
                or str(operation_id) != payload['id']
            ):
                raise ValueError('Invalid cursor')
            query = query.filter(Q(started_at__gt=started_at) | Q(started_at=started_at, pk__gt=operation_id))
        except (signing.BadSignature, ValueError, TypeError, KeyError, OverflowError):
            raise ValidationError('Invalid or expired storage operation cursor.') from None
    fields = (
        'id',
        'state',
        'operation',
        'storage_model',
        'storage_id',
        'subject_id',
        'target_key',
        'lifecycle_revision',
        'started_at',
        'effect_payload',
        'target_object_token',
        'target_version_id',
        'effect_phase',
        'effect_claim_token',
    )
    entries = list(query.order_by('started_at', 'pk').values(*fields)[: limit + 1])
    has_more = len(entries) > limit
    page = entries[:limit]
    from webhooks.durable import PROJECT_EVENTS, WEBHOOK_PRE_SEND

    for entry in page:
        payload = entry.pop('effect_payload')
        token = entry.pop('target_object_token')
        version = entry.pop('target_version_id')
        phase = entry.pop('effect_phase')
        claim_token = entry.pop('effect_claim_token')
        entry['can_recover_webhook_pre_send'] = bool(
            entry['storage_model'] == 'webhooks.webhook'
            and entry['operation'] in {'webhook_task_created', *PROJECT_EVENTS.values()}
            and entry['state'] == StorageOperation.State.RUNNING
            and phase == WEBHOOK_PRE_SEND
            and claim_token is not None
        )
        entry['can_recover_storage_pre_effect'] = bool(
            entry['state'] == StorageOperation.State.RUNNING
            and phase == STORAGE_PRE_EFFECT
            and claim_token is not None
            and (
                (entry['operation'] == 'import_sync' and entry['storage_model'] in VERSIONED_IMPORT_MODELS)
                or (entry['operation'] in {'export_sync', 'export_sync_new', 'annotation_export_v3_queued'}
                    and entry['storage_model'] in VERSIONED_EXPORT_MODELS)
            )
        )
        entry['can_cancel_s3_preflight'] = bool(
            entry['storage_model'] == 'io_storages.s3exportstorage'
            and entry['operation'] == 'annotation_export'
            and entry['state'] in (StorageOperation.State.RUNNING, StorageOperation.State.UNCERTAIN)
            and payload == {'issue86_s3_preflight': 'v2'}
            and isinstance(entry['target_key'], str)
            and entry['target_key']
            and entry['subject_id'] is not None
            and token is None
            and version is None
        )
    next_cursor = (
        signing.dumps(
            {
                'version': 1,
                'project': project.pk,
                'started_at': page[-1]['started_at'].isoformat(),
                'id': str(page[-1]['id']),
            },
            salt='issue86.storage-operation-page.v1',
        )
        if has_more
        else None
    )
    return {'operations': page, 'limit': limit, 'possibly_truncated': has_more, 'operations_next_cursor': next_cursor}


def lock_drained_project(project_id):
    """Lock and reject outstanding ledger and legacy export work.

    This inventory is conservative but cannot prove old producers or cloud
    requests are stopped; deployment isolation remains a separate prerequisite.
    """
    _require_postgres()
    if not connection.in_atomic_block:
        raise RuntimeError('Storage drain requires the transition transaction')
    project = Project.objects.select_for_update().get(pk=project_id)
    if StorageOperation.objects.filter(project=project).exclude(state__in=TERMINAL_STATES).exists():
        raise ValidationError({'reason': 'STORAGE_NOT_DRAINED'})
    # Legacy export workers have their own receipts, not StorageOperation.
    # Treat failures and unrecognized states as unresolved, not as proof that
    # the worker and its external effects are quiescent.
    queries = _legacy_export_queries(project)
    if any(query.exists() for name, query in queries.items() if name.startswith('storage_')):
        raise ValidationError({'reason': 'LEGACY_STORAGE_NOT_DRAINED'})
    if any(query.exists() for name, query in queries.items() if not name.startswith('storage_')):
        raise ValidationError({'reason': 'EXPORT_NOT_DRAINED'})
    return project


def finish_storage_operation(operation_id, *, succeeded):
    """Unknown outcomes stay blocking; never turn failure or lost DB contact into success."""
    state = StorageOperation.State.COMPLETED if succeeded else StorageOperation.State.UNCERTAIN
    updated = StorageOperation.objects.filter(pk=operation_id, state=StorageOperation.State.RUNNING).update(
        state=state, finished_at=timezone.now()
    )
    if updated != 1:
        raise ValidationError('Storage operation is not running.')


def bind_queued_storage_status(storage, record):
    """Bind a new queued status to its durable record inside the same transaction."""
    if not connection.in_atomic_block:
        raise RuntimeError('Storage queue ownership requires an atomic transaction')
    if (
        storage.status != 'queued'
        or record.state != StorageOperation.State.QUEUED
        or record.operation not in SYNC_OPERATIONS
        or record.project_id != storage.project_id
        or record.storage_model != storage._meta.label_lower
        or record.storage_id != storage.pk
        or not isinstance(storage.meta, dict)
    ):
        raise ValidationError('Storage queue ownership does not match the durable record.')
    storage.meta = {**storage.meta, QUEUE_OWNER_META_KEY: str(record.pk)}
    storage.save(update_fields=['meta'])


def record_storage_dispatch_job(storage, operation_id, job_id, *, dispatch_attempt=0):
    """Do not let a delayed enqueue response overwrite a newer queue owner."""
    _require_postgres()
    if type(dispatch_attempt) is not int or dispatch_attempt < 0:
        raise ValidationError('A valid storage dispatch attempt is required.')
    with transaction.atomic():
        project = Project.objects.select_for_update().get(pk=storage.project_id)
        record = (
            StorageOperation.objects.select_for_update()
            .filter(
                pk=operation_id,
                project=project,
                storage_model=storage._meta.label_lower,
                storage_id=storage.pk,
                state__in=(StorageOperation.State.QUEUED, StorageOperation.State.RUNNING),
                operation__in=SYNC_OPERATIONS,
                dispatch_attempts=dispatch_attempt,
            )
            .first()
        )
        if record is None:
            return 0
        locked_storage = (
            type(storage).objects.select_for_update().filter(
                pk=storage.pk,
                project=project,
                status__in=('queued', 'in_progress'),
                **{f'meta__{QUEUE_OWNER_META_KEY}': str(record.pk)},
            ).first()
        )
        if locked_storage is None:
            return 0
        locked_storage.last_sync_job = job_id
        locked_storage.meta = {**locked_storage.meta, QUEUE_JOB_ATTEMPT_META_KEY: dispatch_attempt}
        locked_storage.save(update_fields=['last_sync_job', 'meta'])
        return 1


def prepare_storage_dispatch(storage, operation, *, mark_queued=False):
    """Durably bind a queued dispatch to a lifecycle generation before enqueue."""
    _require_postgres()
    if connection.in_atomic_block:
        raise RuntimeError('Storage dispatch must commit before enqueue')
    with transaction.atomic():
        project = Project.objects.select_for_update().get(pk=storage.project_id)
        require_project_operation(project, SYNC_STORAGE)
        require_no_pending_deletion(project, storage._meta.label_lower, storage.pk)
        # Legacy jobs have no StorageOperation. In particular, health checks
        # may mark an in-flight job FAILED; requeueing must not overwrite that
        # unresolved evidence and later make the storage appear drained.
        locked_storage = type(storage).objects.select_for_update().filter(pk=storage.pk, project=project).first()
        if locked_storage is None:
            raise ValidationError('The storage no longer exists in this project.')
        pending_sync = (
            StorageOperation.objects.filter(
                project=project,
                storage_model=storage._meta.label_lower,
                storage_id=storage.pk,
                operation__in=SYNC_OPERATIONS,
            )
            .exclude(state__in=TERMINAL_STATES)
            .exists()
        )
        if pending_sync:
            if mark_queued and locked_storage.status in ('queued', 'in_progress'):
                return None
            raise ValidationError({'reason': 'STORAGE_OPERATION_PENDING'})
        if locked_storage.status not in LEGACY_STORAGE_SETTLED_STATUSES:
            if mark_queued and locked_storage.status in ('queued', 'in_progress'):
                return None
            raise ValidationError({'reason': 'LEGACY_STORAGE_NOT_DRAINED'})
        if mark_queued and not storage.info_set_queued():
            return None
        record = StorageOperation.objects.create(
            project=project,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
            operation=operation,
            lifecycle_revision=project.lifecycle_revision,
            state=StorageOperation.State.QUEUED,
        )
        if mark_queued:
            bind_queued_storage_status(storage, record)
        return record


def claim_storage_dispatch(storage, operation_id, operation, *, dispatch_attempt=0, fenced=False):
    """Claim only the original storage, operation and generation under the project lock."""
    _require_postgres()
    if connection.in_atomic_block:
        raise RuntimeError('Storage dispatch claim must commit before execution')
    with transaction.atomic():
        project = Project.objects.select_for_update().get(pk=storage.project_id)
        record = StorageOperation.objects.select_for_update().filter(pk=operation_id, project=project).first()
        if (
            record is None
            or record.state != StorageOperation.State.QUEUED
            or record.storage_model != storage._meta.label_lower
            or record.storage_id != storage.pk
            or record.operation != operation
            or record.effect_phase is not None
            or record.effect_claim_token is not None
        ):
            raise ValidationError('The queued storage operation does not match.')
        if record.lifecycle_revision != project.lifecycle_revision:
            raise ValidationError({'reason': 'STALE_LIFECYCLE_GENERATION'})
        require_project_operation(project, SYNC_STORAGE)
        if operation in SYNC_OPERATIONS:
            if (
                type(dispatch_attempt) is not int
                or dispatch_attempt < 0
                or record.dispatch_attempts != dispatch_attempt
            ):
                raise ValidationError({'reason': 'STALE_STORAGE_DISPATCH_ATTEMPT'})
            # A legacy producer may have overwritten shared StorageInfo after
            # this v2 record was queued. Never run against a different or
            # unidentifiable queue owner, even if the ledger is still queued.
            locked_storage = type(storage).objects.select_for_update().filter(pk=storage.pk, project=project).first()
            owner = (
                locked_storage.meta.get(QUEUE_OWNER_META_KEY)
                if (locked_storage is not None and isinstance(locked_storage.meta, dict))
                else None
            )
            if locked_storage is None or locked_storage.status != 'queued' or owner != str(record.pk):
                raise ValidationError({'reason': 'STORAGE_QUEUE_OWNERSHIP_UNVERIFIED'})
            # The worker originally loaded storage before acquiring the lock.
            # Refresh so it does not execute with an already stale status/meta.
            storage.refresh_from_db()
        record.state = StorageOperation.State.RUNNING
        if fenced:
            from uuid import uuid4

            record.effect_phase = STORAGE_PRE_EFFECT
            record.effect_claim_token = uuid4()
        record.save(update_fields=['state', 'effect_phase', 'effect_claim_token'])
        return record


def begin_claimed_storage_effect(record):
    """Commit the irreversible-effect fence before invoking a storage provider."""
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError('Storage effect admission must commit before execution')
    if not isinstance(record.effect_claim_token, UUID):
        raise ValidationError('A storage claim token is required.')
    with transaction.atomic():
        updated = StorageOperation.objects.filter(
            pk=record.pk, project_id=record.project_id, storage_model=record.storage_model,
            storage_id=record.storage_id, operation=record.operation, lifecycle_revision=record.lifecycle_revision,
            state=StorageOperation.State.RUNNING, effect_phase=STORAGE_PRE_EFFECT,
            effect_claim_token=record.effect_claim_token,
        ).update(effect_phase=STORAGE_EFFECT_STARTED)
        if updated != 1:
            raise ValidationError('Storage pre-effect admission is no longer active.')


def cancel_queued_storage_dispatch(project_id, operation_id, principal, *, reason):
    _require_postgres()
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValidationError('A cancellation reason is required.')
    with transaction.atomic():
        project, actor = lock_project_operation(project_id, principal, SECURITY_CANCEL)
        authorization.require(
            authorization.can_manage_project(principal, project), 'Project manager role is required.'
        )
        record = StorageOperation.objects.select_for_update().filter(pk=operation_id, project=project).first()
        from webhooks.durable import PROJECT_EVENTS

        if record is None or record.state != StorageOperation.State.QUEUED:
            raise ValidationError('A queued, version-fenced operation in this project is required.')
        webhook_operations = {'webhook_task_created', *PROJECT_EVENTS.values()}
        version_fenced = (
            (record.operation == 'import_sync' and record.storage_model in VERSIONED_IMPORT_MODELS)
            or (
                record.operation
                in {'export_sync', 'export_sync_new', 'annotation_export_queued', 'annotation_export_v3_queued'}
                and record.storage_model in VERSIONED_EXPORT_MODELS
            )
            or (record.operation in webhook_operations and record.storage_model == 'webhooks.webhook')
        )
        if not version_fenced:
            raise ValidationError('A queued, version-fenced operation in this project is required.')
        record.state = StorageOperation.State.CANCELLED
        record.resolution_reason = reason.strip()
        record.resolved_by = actor
        record.resolved_by_id_snapshot = actor.pk
        record.finished_at = timezone.now()
        record.save(
            update_fields=['state', 'resolution_reason', 'resolved_by', 'resolved_by_id_snapshot', 'finished_at']
        )
        if record.operation in {'import_sync', 'export_sync', 'export_sync_new'}:
            from django.apps import apps
            from io_storages.base_models import StorageInfo

            try:
                storage_class = apps.get_model(record.storage_model)
            except (LookupError, ValueError):
                storage_class = None
            if storage_class is not None and issubclass(storage_class, StorageInfo):
                storage = (
                    storage_class.objects.select_for_update().filter(pk=record.storage_id, project=project).first()
                )
                other_sync = (
                    StorageOperation.objects.filter(
                        project=project,
                        storage_model=record.storage_model,
                        storage_id=record.storage_id,
                        operation__in=SYNC_OPERATIONS,
                    )
                    .exclude(state__in=TERMINAL_STATES)
                    .exists()
                )
                owner = (
                    storage.meta.get(QUEUE_OWNER_META_KEY)
                    if storage is not None and isinstance(storage.meta, dict)
                    else None
                )
                if (
                    storage is not None
                    and storage.status == storage.Status.QUEUED
                    and not other_sync
                    and owner == str(record.pk)
                ):
                    storage.status = storage.Status.INITIALIZED
                    storage.last_sync_job = None
                    storage.meta = {key: value for key, value in storage.meta.items() if key != QUEUE_OWNER_META_KEY}
                    storage.save(update_fields=['status', 'last_sync_job', 'meta'])


def reconcile_pending_deletion(project_id, operation_id, principal, *, reason, attested_complete):
    """Record an operator-verified external deletion; never issues the deletion itself."""
    _require_postgres()
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000 or attested_complete is not True:
        raise ValidationError('A reason and explicit deletion attestation are required.')
    with transaction.atomic():
        project, actor = lock_project_operation(project_id, principal, SECURITY_CANCEL)
        authorization.require(
            authorization.can_manage_project(principal, project), 'Project manager role is required.'
        )
        record = StorageOperation.objects.select_for_update().filter(pk=operation_id, project=project).first()
        if (
            record is None
            or record.state != StorageOperation.State.QUEUED
            or record.operation != 'annotation_delete_pending'
        ):
            raise ValidationError('A pending deletion in this project is required.')
        require_manual_reconciliation_supported(record)
        record.state = StorageOperation.State.RECONCILED
        record.resolution_reason = reason.strip()
        record.resolved_by = actor
        record.resolved_by_id_snapshot = actor.pk
        record.finished_at = timezone.now()
        record.save(
            update_fields=['state', 'resolution_reason', 'resolved_by', 'resolved_by_id_snapshot', 'finished_at']
        )


def cancel_unsubmitted_s3_preflight(project_id, operation_id, principal, *, reason):
    """Cancel a v2 attempt only while its provider request cannot have started."""
    _require_postgres()
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValidationError('A cancellation reason is required.')
    from io_storages.s3.version_provenance import lock_s3_export_key

    with transaction.atomic():
        project, actor = lock_project_operation(project_id, principal, SECURITY_CANCEL)
        authorization.require(
            authorization.can_manage_project(principal, project), 'Project manager role is required.'
        )
        candidate = StorageOperation.objects.filter(pk=operation_id, project=project).values('target_key').first()
        key = candidate['target_key'] if candidate else None
        if not isinstance(key, str) or not key:
            raise ValidationError('A bound S3 preflight is required.')
        # Never lock the row before this key: request preparation takes the
        # advisory lock first and would otherwise deadlock with cancellation.
        lock_s3_export_key(key)
        record = (
            StorageOperation.objects.select_for_update()
            .filter(
                pk=operation_id,
                project=project,
                storage_model='io_storages.s3exportstorage',
                operation='annotation_export',
                state__in=(StorageOperation.State.RUNNING, StorageOperation.State.UNCERTAIN),
                effect_payload={'issue86_s3_preflight': 'v2'},
                target_key=key,
                subject_id__isnull=False,
                target_object_token__isnull=True,
                target_version_id__isnull=True,
            )
            .first()
        )
        if record is None:
            raise ValidationError('The S3 preflight cannot be proven unsubmitted.')
        record.state = StorageOperation.State.CANCELLED
        record.resolution_reason = reason.strip()
        record.resolved_by = actor
        record.resolved_by_id_snapshot = actor.pk
        record.finished_at = timezone.now()
        record.save(
            update_fields=['state', 'resolution_reason', 'resolved_by', 'resolved_by_id_snapshot', 'finished_at']
        )


def recover_unsubmitted_webhook(project_id, operation_id, principal, *, reason):
    """Requeue a provably unsent v4 webhook while fencing its original claimant."""
    _require_postgres()
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValidationError('A cancellation reason is required.')
    from webhooks.durable import PROJECT_EVENTS, WEBHOOK_PRE_SEND, dispatch_project_event, dispatch_webhook

    with transaction.atomic():
        project, actor = lock_project_operation(project_id, principal, SECURITY_CANCEL)
        authorization.require(authorization.can_manage_project(principal, project), 'Project manager role is required.')
        record = StorageOperation.objects.select_for_update().filter(
            pk=operation_id,
            project=project,
            storage_model='webhooks.webhook',
            operation__in={'webhook_task_created', *PROJECT_EVENTS.values()},
            state=StorageOperation.State.RUNNING,
            effect_phase=WEBHOOK_PRE_SEND,
            effect_claim_token__isnull=False,
        ).first()
        if record is None:
            raise ValidationError('A provably unsubmitted webhook is required.')
        require_project_operation(project, SYNC_STORAGE)
        if record.lifecycle_revision != project.lifecycle_revision:
            raise ValidationError({'reason': 'STALE_LIFECYCLE_GENERATION'})
        StorageOperationRecovery.objects.create(
            operation=record, claim_token=record.effect_claim_token, reason=reason.strip(),
            actor=actor, actor_id_snapshot=actor.pk,
        )
        record.state = StorageOperation.State.QUEUED
        record.effect_phase = None
        record.effect_claim_token = None
        record.next_dispatch_at = None
        record.save(update_fields=['state', 'effect_phase', 'effect_claim_token', 'next_dispatch_at'])
        dispatch = dispatch_webhook if record.operation == 'webhook_task_created' else dispatch_project_event
        transaction.on_commit(lambda pk=record.pk: dispatch(pk), robust=True)


def recover_unstarted_storage(project_id, operation_id, principal, *, reason):
    """Requeue only a fenced storage claim whose provider work has not started."""
    _require_postgres()
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValidationError('A recovery reason is required.')
    from django.apps import apps
    from io_storages.base_models import ExportStorage, ImportStorage
    from io_storages.dispatch_recovery import redispatch_queued_operation
    from io_storages.localfiles import models as _localfiles_models  # noqa: F401

    with transaction.atomic():
        project, actor = lock_project_operation(project_id, principal, SECURITY_CANCEL)
        authorization.require(authorization.can_manage_project(principal, project), 'Project manager role is required.')
        record = StorageOperation.objects.select_for_update().filter(
            pk=operation_id, project=project, state=StorageOperation.State.RUNNING,
            effect_phase=STORAGE_PRE_EFFECT, effect_claim_token__isnull=False,
        ).first()
        if record is None:
            raise ValidationError('A provably unstarted storage operation is required.')
        valid_import = record.operation == 'import_sync' and record.storage_model in VERSIONED_IMPORT_MODELS
        valid_export = record.operation in {'export_sync', 'export_sync_new', 'annotation_export_v3_queued'} and (
            record.storage_model in VERSIONED_EXPORT_MODELS
        )
        if not (valid_import or valid_export):
            raise ValidationError('A version-fenced storage operation is required.')
        require_project_operation(project, SYNC_STORAGE)
        if record.lifecycle_revision != project.lifecycle_revision:
            raise ValidationError({'reason': 'STALE_LIFECYCLE_GENERATION'})
        storage_class = apps.get_model(record.storage_model)
        expected_base = ImportStorage if valid_import else ExportStorage
        if storage_class is None or not issubclass(storage_class, expected_base):
            raise ValidationError('Storage type does not match the operation.')
        storage = storage_class.objects.select_for_update().filter(pk=record.storage_id, project=project).first()
        if storage is None:
            raise ValidationError('The original storage no longer exists.')
        require_no_pending_deletion(project, record.storage_model, record.storage_id)
        if record.operation in SYNC_OPERATIONS:
            owner = storage.meta.get(QUEUE_OWNER_META_KEY) if isinstance(storage.meta, dict) else None
            if storage.status not in {'queued', 'failed'} or owner != str(record.pk):
                raise ValidationError({'reason': 'STORAGE_QUEUE_OWNERSHIP_UNVERIFIED'})
            # A previous RQ job receipt must not let a health check mark the
            # recovered attempt failed before the new enqueue returns its ID.
            storage.last_sync_job = None
            storage.status = 'queued'
            storage.traceback = None
            # Change the receipt epoch even if a broker later reuses an old ID;
            # an in-flight health observation must not match the new attempt.
            from uuid import uuid4

            storage.meta = {
                **{key: value for key, value in storage.meta.items() if key != QUEUE_JOB_ATTEMPT_META_KEY},
                '_issue86_health_epoch': str(uuid4()),
            }
            storage.save(update_fields=['last_sync_job', 'status', 'traceback', 'meta'])
            record.dispatch_attempts += 1
        StorageOperationRecovery.objects.create(
            operation=record, claim_token=record.effect_claim_token, reason=reason.strip(),
            actor=actor, actor_id_snapshot=actor.pk,
        )
        record.state = StorageOperation.State.QUEUED
        record.effect_phase = None
        record.effect_claim_token = None
        record.next_dispatch_at = None
        record.save(update_fields=['state', 'effect_phase', 'effect_claim_token', 'next_dispatch_at', 'dispatch_attempts'])
        transaction.on_commit(
            lambda pk=record.pk, attempt=record.dispatch_attempts: redispatch_queued_operation(pk, expected_attempt=attempt),
            robust=True,
        )


@contextmanager
def admitted_storage_operation(
    project_id,
    storage_model,
    storage_id,
    operation,
    *,
    expected_revision=None,
    s3_identity_pending=False,
    s3_preflight_key=None,
    s3_subject_id=None,
):
    record = begin_storage_operation(
        project_id,
        storage_model,
        storage_id,
        operation,
        expected_revision=expected_revision,
        s3_identity_pending=s3_identity_pending,
        s3_preflight_key=s3_preflight_key,
        s3_subject_id=s3_subject_id,
    )
    try:
        yield record
    except BaseException as exc:
        if getattr(record, '_s3_preflight_blocked', False):
            # This flag is set only around the pre-send identity transaction.
            # A v2 row without a token proves this attempt was not submitted.
            updated = StorageOperation.objects.filter(
                pk=record.pk,
                state=StorageOperation.State.RUNNING,
                operation='annotation_export',
                target_key=record.target_key,
                subject_id=record.subject_id,
                effect_payload={'issue86_s3_preflight': 'v2'},
                target_object_token__isnull=True,
            ).update(
                state=StorageOperation.State.CANCELLED,
                finished_at=timezone.now(),
                resolution_reason='S3 target blocked before provider submission',
            )
            already_cancelled = (
                updated == 0
                and StorageOperation.objects.filter(
                    pk=record.pk,
                    state=StorageOperation.State.CANCELLED,
                    operation='annotation_export',
                    target_key=record.target_key,
                    subject_id=record.subject_id,
                    effect_payload={'issue86_s3_preflight': 'v2'},
                    target_object_token__isnull=True,
                    target_version_id__isnull=True,
                ).exists()
            )
            if updated != 1 and not already_cancelled:
                raise RuntimeError('Cannot cancel an unverified S3 preflight attempt.')
            exc._s3_cancelled_attempt = str(record.pk)
        else:
            finish_storage_operation(record.pk, succeeded=False)
        raise
    else:
        finish_storage_operation(record.pk, succeeded=True)


@contextmanager
def storage_effect(storage, operation, *, s3_identity_pending=False, s3_preflight_key=None, s3_subject_id=None):
    """Opt-in until lifecycle transitions and all producer routes use the ledger."""
    if not getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
        yield
        return
    with admitted_storage_operation(
        storage.project_id,
        storage._meta.label_lower,
        storage.pk,
        operation,
        expected_revision=getattr(storage, '_storage_lifecycle_revision', None),
        s3_identity_pending=s3_identity_pending,
        s3_preflight_key=s3_preflight_key,
        s3_subject_id=s3_subject_id,
    ) as record:
        yield record


def admitted_annotation_effect(operation):
    def decorate(method):
        @wraps(method)
        def wrapped(storage, *args, **kwargs):
            if operation == 'annotation_delete' and getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
                # The legacy delete methods address only a mutable object key.
                # No provider version has been pinned, so they cannot be safely
                # retried or distinguished from a replacement object.
                raise PermissionDenied('Version-bound storage deletion is not available.')
            s3_identity_pending = (
                operation == 'annotation_export' and storage._meta.label_lower == 'io_storages.s3exportstorage'
            )
            s3_preflight_key = s3_subject_id = None
            if s3_identity_pending and getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
                if '_issue86_export_identity' in kwargs or not (
                    (len(args) == 1 and 'annotation' not in kwargs) or (not args and 'annotation' in kwargs)
                ):
                    raise ValueError('A single annotation is required for the durable S3 export.')
                from io_storages.s3.version_provenance import s3_export_target_key

                annotation = args[0] if args else kwargs['annotation']
                s3_preflight_key, s3_subject_id = s3_export_target_key(storage, annotation), annotation.pk
            with storage_effect(
                storage,
                operation,
                s3_identity_pending=s3_identity_pending,
                s3_preflight_key=s3_preflight_key,
                s3_subject_id=s3_subject_id,
            ) as record:
                if (
                    record is not None
                    and operation == 'annotation_export'
                    and storage._meta.label_lower == 'io_storages.s3exportstorage'
                ):
                    if '_issue86_export_identity' in kwargs or not (
                        (len(args) == 1 and 'annotation' not in kwargs) or (not args and 'annotation' in kwargs)
                    ):
                        raise ValueError('A single annotation is required for the durable S3 export.')
                    from io_storages.s3.version_provenance import (
                        S3ExportTargetBusy,
                        prepare_s3_export_request,
                        record_s3_export_response,
                    )

                    annotation = args[0] if args else kwargs['annotation']
                    try:
                        identity = prepare_s3_export_request(storage, annotation, record)
                    except S3ExportTargetBusy:
                        record._s3_preflight_blocked = True
                        raise
                    result = method(storage, *args, _issue86_export_identity=identity, **kwargs)
                    record_s3_export_response(record, identity)
                    return result
                return method(storage, *args, **kwargs)

        return wrapped

    return decorate


def reconcile_uncertain_storage_operation(project_id, operation_id, principal, *, reason, attested_quiescent):
    """Record an operator's external reconciliation; caller must authenticate principal.

    The operator is responsible for stopping the original worker and checking
    the provider outcome. This API does not itself prove either condition.
    """
    _require_postgres()
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000 or attested_quiescent is not True:
        raise ValidationError('A reason and explicit quiescence attestation are required.')
    with transaction.atomic():
        project, actor = lock_project_operation(project_id, principal, SECURITY_CANCEL)
        if not authorization.can_manage_project(principal, project):
            raise PermissionDenied('Project manager role is required.')
        record = StorageOperation.objects.select_for_update().filter(pk=operation_id, project=project).first()
        # RUNNING may still be executing, even if a caller believes its worker
        # has died. An assertion from an internal caller is not proof of drain.
        if record is None or record.state != StorageOperation.State.UNCERTAIN:
            raise ValidationError('An uncertain operation in this project is required.')
        require_manual_reconciliation_supported(record)
        record.state = StorageOperation.State.RECONCILED
        record.resolution_reason = reason.strip()
        record.resolved_by = actor
        record.resolved_by_id_snapshot = actor.pk
        record.finished_at = timezone.now()
        record.save(
            update_fields=['state', 'resolution_reason', 'resolved_by', 'resolved_by_id_snapshot', 'finished_at']
        )
