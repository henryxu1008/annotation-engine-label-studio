"""Durable, generation-bound task-created webhook deliveries."""
import json
import uuid

import requests
from core.redis import start_job_async_or_sync
from core.utils.common import load_func
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.utils.crypto import constant_time_compare, salted_hmac
from io_storages.admission import _require_postgres, finish_storage_operation
from io_storages.admission_models import StorageOperation
from projects.models import Project
from rest_framework.exceptions import ValidationError
from webhooks.models import Webhook, WebhookAction


def _binding(webhook, payload, project_id, revision, operation=None):
    value = [
        webhook.pk,
        webhook.organization_id,
        webhook.project_id,
        webhook.url,
        webhook.headers,
        webhook.is_active,
        webhook.send_payload,
        webhook.send_for_all_actions,
        sorted(webhook.get_actions()),
        project_id,
        revision,
        payload,
    ]
    if operation is not None:
        value.append(operation)
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
    return salted_hmac('issue86.webhook-target.v1', encoded, algorithm='sha256').hexdigest()


PROJECT_EVENTS = {
    WebhookAction.PROJECT_CREATED: 'webhook_project_created',
    WebhookAction.PROJECT_UPDATED: 'webhook_project_updated',
    WebhookAction.ANNOTATION_CREATED: 'webhook_annotation_created',
    WebhookAction.ANNOTATION_UPDATED: 'webhook_annotation_updated',
    WebhookAction.ANNOTATIONS_DELETED: 'webhook_annotations_deleted',
    WebhookAction.TASKS_DELETED: 'webhook_tasks_deleted',
    WebhookAction.LABEL_LINK_CREATED: 'webhook_label_link_created',
    WebhookAction.LABEL_LINK_UPDATED: 'webhook_label_link_updated',
    WebhookAction.LABEL_LINK_DELETED: 'webhook_label_link_deleted',
}


def _queue_webhooks(project, action, instance, operation, dispatch):
    """Called inside the source transaction, with the project lock already held."""
    from webhooks.utils import get_active_webhooks, get_nested_field

    _require_postgres()
    if not connection.in_atomic_block:
        raise RuntimeError('Webhook outbox requires the source transaction')
    project = Project.objects.select_for_update(nowait=True).get(pk=project.pk)
    meta = WebhookAction.ACTIONS[action]
    event_project = None if meta.get('organization-only') else project
    ids = list(get_active_webhooks(project.organization, event_project, action).values_list('pk', flat=True))
    hooks = list(Webhook.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
    if not hooks:
        return
    payload = {}
    if meta['many'] and any(hook.send_payload for hook in hooks):
        count = len(instance) if isinstance(instance, (list, tuple)) else instance.count()
        if count > 10000:
            raise ValidationError('Webhook batch exceeds the durable outbox limit.')
    if instance and any(hook.send_payload for hook in hooks):
        payload[meta['key']] = meta['serializer'](instance=instance, many=meta['many']).data
        payload['project'] = load_func(settings.WEBHOOK_SERIALIZERS['project'])(instance=project).data
        for key, value in meta.get('nested-fields', {}).items():
            payload[key] = value['serializer'](
                instance=get_nested_field(instance, value['field']), many=value['many']
            ).data
    payload = json.loads(json.dumps(payload, cls=DjangoJSONEncoder, allow_nan=False))
    if len(json.dumps(payload, cls=DjangoJSONEncoder).encode('utf-8')) > 1024 * 1024:
        raise ValidationError('Webhook payload exceeds the durable outbox limit.')
    for hook in hooks:
        if not hook.is_active or (not hook.send_for_all_actions and action not in hook.get_actions()):
            continue
        if any(key.lower() == 'x-label-studio-delivery-id' for key in hook.headers):
            raise ValidationError('Webhook headers reserve X-Label-Studio-Delivery-ID.')
        body = {'action': action, **(payload if hook.send_payload else {})}
        record = StorageOperation.objects.create(
            project=project,
            storage_model='webhooks.webhook',
            storage_id=hook.pk,
            operation=operation,
            lifecycle_revision=project.lifecycle_revision,
            target_storage_binding=_binding(
                hook,
                body,
                project.pk,
                project.lifecycle_revision,
                None if operation == 'webhook_task_created' else operation,
            ),
            effect_payload=body,
            state=StorageOperation.State.QUEUED,
        )
        transaction.on_commit(lambda pk=record.pk: dispatch(pk), robust=True)


def queue_task_created_webhooks(project, tasks):
    _queue_webhooks(project, WebhookAction.TASKS_CREATED, tasks, 'webhook_task_created', dispatch_webhook)


def queue_project_event(project, action, instance):
    operation = PROJECT_EVENTS.get(action)
    if operation is None:
        raise ValidationError('This webhook event is not enrolled in the durable outbox.')
    _queue_webhooks(project, action, instance, operation, dispatch_project_event)


def dispatch_webhook(operation_id):
    if not getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
        raise ValidationError('Storage admission is disabled.')
    return start_job_async_or_sync(deliver_task_created_v4, str(operation_id), queue_name='low')


def dispatch_project_event(operation_id):
    if not getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
        raise ValidationError('Storage admission is disabled.')
    return start_job_async_or_sync(deliver_project_event_v4, str(operation_id), queue_name='low')


def deliver_task_created_v2(operation_id, **kwargs):
    """A failed/abandoned send remains blocking and is never automatically retried."""
    return _deliver(operation_id, {'webhook_task_created'})


def deliver_project_event_v3(operation_id, **kwargs):
    return _deliver(operation_id, set(PROJECT_EVENTS.values()))


WEBHOOK_PRE_SEND = 'webhook_pre_send_v1'
WEBHOOK_SUBMITTING = 'webhook_submitting_v1'


def deliver_task_created_v4(operation_id, **kwargs):
    return _deliver(operation_id, {'webhook_task_created'}, fenced=True)


def deliver_project_event_v4(operation_id, **kwargs):
    return _deliver(operation_id, set(PROJECT_EVENTS.values()), fenced=True)


def _begin_fenced_send(operation_id, claim_token, project_id, revision, operations):
    # This transaction must commit before the HTTP request and outside the
    # webhook configuration transaction. A cancellation winning the same row
    # prevents the worker from ever entering requests.post.
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError('Webhook submission must commit before delivery')
    with transaction.atomic():
        updated = StorageOperation.objects.filter(
            pk=operation_id,
            project_id=project_id,
            storage_model='webhooks.webhook',
            operation__in=operations,
            lifecycle_revision=revision,
            state=StorageOperation.State.RUNNING,
            effect_phase=WEBHOOK_PRE_SEND,
            effect_claim_token=claim_token,
        ).update(effect_phase=WEBHOOK_SUBMITTING)
        if updated != 1:
            raise ValidationError('Webhook pre-send admission is no longer active.')


def _deliver(operation_id, operations, *, fenced=False):
    if not getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
        raise ValidationError('Storage admission is disabled.')
    _require_postgres()
    if connection.in_atomic_block:
        raise RuntimeError('Webhook claim must commit before delivery')
    initial = StorageOperation.objects.get(pk=operation_id)
    with transaction.atomic():
        project = Project.objects.select_for_update().get(pk=initial.project_id)
        record = StorageOperation.objects.select_for_update().get(pk=operation_id, project=project)
        if (
            record.operation not in operations
            or record.storage_model != 'webhooks.webhook'
            or record.state != StorageOperation.State.QUEUED
            or record.lifecycle_revision != project.lifecycle_revision
            or record.effect_phase is not None
            or record.effect_claim_token is not None
        ):
            raise ValidationError('The queued webhook operation does not match.')
        record.state = StorageOperation.State.RUNNING
        record.effect_phase = WEBHOOK_PRE_SEND if fenced else None
        record.effect_claim_token = uuid.uuid4() if fenced else None
        record.save(update_fields=['state', 'effect_phase', 'effect_claim_token'])
    if fenced:
        _begin_fenced_send(record.pk, record.effect_claim_token, record.project_id, record.lifecycle_revision, operations)
    try:
        with transaction.atomic():
            hook = (
                Webhook.objects.select_for_update()
                .filter(pk=record.storage_id, organization_id=project.organization_id)
                .first()
            )
            if (
                hook is None
                or hook.project_id not in (None, project.pk)
                or not hook.is_active
                or not record.target_storage_binding
                or not constant_time_compare(
                    _binding(
                        hook,
                        record.effect_payload,
                        record.project_id,
                        record.lifecycle_revision,
                        None if record.operation == 'webhook_task_created' else record.operation,
                    ),
                    record.target_storage_binding,
                )
            ):
                raise ValidationError('Webhook destination or configuration changed.')
            headers = dict(hook.headers)
            if any(key.lower() == 'x-label-studio-delivery-id' for key in headers):
                raise ValidationError('Webhook delivery header is reserved.')
            headers['X-Label-Studio-Delivery-ID'] = str(record.pk)
            response = requests.post(
                hook.url,
                headers=headers,
                json=record.effect_payload,
                timeout=settings.WEBHOOK_TIMEOUT,
                allow_redirects=False,
            )
            if not 200 <= response.status_code < 300:
                raise ValidationError('Webhook receiver did not acknowledge delivery.')
    except BaseException:
        finish_storage_operation(record.pk, succeeded=False)
        raise
    else:
        finish_storage_operation(record.pk, succeeded=True)
