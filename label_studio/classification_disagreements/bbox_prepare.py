"""Pre-assignment media pinning service, behind an explicit project opt-in."""

import logging
from copy import deepcopy
from urllib.parse import urlsplit

from access_control.authorization import authorization
from access_control.project_operations import ASSIGN, lock_project_operation
from data_export.snapshots import upload_for
from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac
from io_storages.gcs.models import GCSImportStorage, GCSImportStorageLink
from io_storages.s3.models import S3ImportStorage, S3ImportStorageLink
from rest_framework.exceptions import ValidationError
from tasks.models import Task, TaskAssignment

from . import bbox, bbox_attempts, bbox_media, bbox_storage
from .models import BboxMediaAttempt, BboxMediaPin

logger = logging.getLogger(__name__)


def object_source(task, reference, *, lock=False):
    parsed = urlsplit(reference)
    key = parsed.path.lstrip('/')
    if (parsed.scheme not in ('gs', 's3') or not parsed.netloc or not key or parsed.query or parsed.fragment
            or parsed.path.startswith('//') or parsed.username or parsed.password
            or any(part in ('', '.', '..') for part in key.split('/'))):
        raise ValidationError({'detail': 'Unsupported storage reference.'})
    storage_class, link_class = ((GCSImportStorage, GCSImportStorageLink) if parsed.scheme == 'gs'
                                 else (S3ImportStorage, S3ImportStorageLink))
    query = link_class.objects.filter(task=task, key=key)
    if lock:
        query = query.select_for_update()
    link = query.first()
    if link is None:
        raise ValidationError({'detail': 'A task-linked storage is required.'})
    storages = storage_class.objects.filter(pk=link.storage_id, project_id=task.project_id, bucket=parsed.netloc)
    if lock:
        storages = storages.select_for_update()
    storage = storages.first()
    if storage is None or storage.prefix and not key.startswith(storage.prefix.rstrip('/') + '/'):
        raise ValidationError({'detail': 'Storage does not match the task source.'})
    credentials = ([storage.google_project_id, storage.google_application_credentials] if parsed.scheme == 'gs' else
                   [storage.aws_access_key_id, storage.aws_secret_access_key, storage.aws_session_token,
                    storage.region_name, storage.s3_endpoint])
    private_config = bbox.digest([parsed.scheme, storage.pk, storage.bucket, storage.prefix, *credentials])
    fingerprint = salted_hmac('bbox-object-config/v1', private_config, algorithm='sha256').hexdigest()
    return storage, key, fingerprint


def prepare_managed(task, principal):
    """Capture outside locks, then publish only after authoritative rechecks."""
    project = task.project
    authorization.require(authorization.can_manage_project(principal, project), 'Manager role is required.')
    existing = BboxMediaPin.objects.filter(task=task, project=project).first()
    if existing:
        # Recovery after a successful commit whose response was lost must not
        # create a replacement generation or infer identity from the source.
        task.refresh_from_db()
        project.refresh_from_db()
        authorization.require(authorization.can_manage_project(principal, project), 'Manager role is required.')
        if (task.project_id != project.pk or not 0 < existing.size_bytes <= bbox.MAX_IMAGE_BYTES
                or bbox.digest(task.data) != existing.task_data_hash
                or bbox.digest(project.label_config) != existing.config_hash
                or existing.artifact_key != str(existing.token)):
            raise ValidationError({'detail': 'Existing media binding is unavailable or has changed.'})
        with bbox_storage.open_verified(existing.token, existing.storage_binding, existing.sha256,
                                        existing.size_bytes) as reader:
            identity = bbox.image_identity(reader.read(bbox.MAX_IMAGE_BYTES + 1))
        if identity != {'sha256': existing.sha256, 'width': existing.width, 'height': existing.height}:
            raise ValidationError({'detail': 'Existing media binding is unavailable or has changed.'})
        with transaction.atomic():
            project, _ = lock_project_operation(project.pk, principal, ASSIGN)
            authorization.require(authorization.can_manage_project(principal, project), 'Manager role is required.')
            task = Task.objects.select_for_update().get(pk=task.pk, project=project)
            if (bbox.digest(task.data) != existing.task_data_hash
                    or bbox.digest(project.label_config) != existing.config_hash
                    or not BboxMediaPin.objects.filter(pk=existing.pk, task=task, project=project).exists()):
                raise ValidationError({'detail': 'Existing media binding is unavailable or has changed.'})
            return existing
    if (TaskAssignment.objects.filter(task=task).exists() or task.annotations.exists()
            or task.drafts.exists()):
        raise ValidationError({'detail': 'Task already has prior work or a media pin.'})
    try:
        control = bbox.controls(project.label_config)
        original = deepcopy(task.data)
        field = control['field']
        undefined = settings.DATA_UNDEFINED_NAME
        if field in original and undefined in original:
            raise bbox.Incomparable('ambiguous_image_reference')
        source_field = undefined if undefined in original else field
        reference = original[source_field]
        if not isinstance(reference, str):
            raise bbox.Incomparable('missing_image_reference')
        original_hash, config_hash = bbox.digest(original), bbox.digest(project.label_config)
    except (bbox.Incomparable, TypeError, KeyError, ValueError) as exc:
        raise ValidationError({'detail': 'Unsupported bbox source or configuration.'}) from exc
    attempt = bbox_attempts.reserve(task, principal, original_hash, config_hash)
    try:
        return capture_and_publish(task, principal, project, reference, field, source_field,
                                   original_hash, config_hash, attempt)
    except Exception:
        try:
            bbox_attempts.reconcile(attempt.token, abandon_active=True)
        except Exception:
            # Unknown commit or unavailable storage: retain the durable attempt
            # so a later reconciliation can safely recheck it.
            logger.warning('Unable to reconcile bbox capture attempt', exc_info=True)
        raise


def capture_and_publish(task, principal, project, reference, field, source_field, original_hash, config_hash, attempt):
    if reference.startswith(('gs://', 's3://')):
        storage, object_key, config_fingerprint = object_source(task, reference)
        if reference.startswith('gs://'):
            client = storage.get_client()
            content, image, generation = bbox_media.capture_gcs(client.bucket(storage.bucket).blob(object_key))
            source = {'kind': 'gcs', 'storage_id': storage.pk, 'reference': reference,
                      'generation': str(generation), 'config_fingerprint': config_fingerprint}
        else:
            stream, _, metadata = storage.get_bytes_stream(reference)
            content, image = bbox_media.capture_stream(stream, metadata)
            source = {'kind': 's3', 'storage_id': storage.pk, 'reference': reference,
                      'config_fingerprint': config_fingerprint}
    else:
        content, image, upload_id = bbox_media.capture_managed_upload(reference, project)
        source = {'kind': 'managed-upload', 'upload_id': upload_id, 'reference': reference}
    token = attempt.token
    key, storage_id = bbox_storage.publish(token, content, expected_binding=attempt.storage_binding)
    # A failed/ambiguous transaction retains the candidate for reconciliation.
    with transaction.atomic():
        project, actor = lock_project_operation(project.pk, principal, ASSIGN)
        authorization.require(authorization.can_manage_project(principal, project), 'Manager role is required.')
        task = Task.objects.select_for_update().get(pk=task.pk, project=project)
        attempt = BboxMediaAttempt.objects.select_for_update().get(pk=token)
        if (bbox.digest(task.data) != original_hash or bbox.digest(project.label_config) != config_hash
                or TaskAssignment.objects.filter(task=task).exists() or task.annotations.exists()
                or task.drafts.exists() or BboxMediaPin.objects.filter(task=task).exists()
                or attempt.status != 'active' or attempt.expires_at <= timezone.now()
                or attempt.task_id != task.pk or attempt.project_id != project.pk
                or attempt.actor_id_frozen != actor.pk or attempt.storage_binding != storage_id):
            raise ValidationError({'detail': 'Task, configuration or prior work changed before pinning.'})
        if source['kind'] in ('gcs', 's3'):
            current, _, current_fingerprint = object_source(task, reference, lock=True)
            if current.pk != source['storage_id'] or current_fingerprint != source['config_fingerprint']:
                raise ValidationError({'detail': 'GCS storage changed before pinning.'})
        else:
            upload, _ = upload_for(reference, project)
            if upload.pk != source['upload_id']:
                raise ValidationError({'detail': 'Managed upload changed before pinning.'})
        data = deepcopy(task.data)
        if source_field != field:
            del data[source_field]
        data[field] = reverse('projects:api:bbox-media', kwargs={'pk': project.pk, 'token': token})
        pin = BboxMediaPin.objects.create(
            task=task, project=project, token=token, actor_id_frozen=actor.pk, field=field,
            source=source,
            task_data_hash=bbox.digest(data), config_hash=config_hash, artifact_key=key,
            storage_binding=storage_id, sha256=image['sha256'], size_bytes=len(content),
            width=image['width'], height=image['height'],
        )
        task.data = data
        task.save(update_fields=['data'])
        attempt.status = 'published'
        attempt.save(update_fields=['status'])
        return pin
