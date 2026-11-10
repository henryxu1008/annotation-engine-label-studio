"""Fenced candidate leases and repeatable cleanup for bbox media capture."""

import logging
from datetime import timedelta
from uuid import uuid4

from access_control.authorization import authorization
from access_control.project_operations import ASSIGN, lock_project_operation
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from tasks.models import Task, TaskAssignment

from . import bbox, bbox_storage
from .models import BboxMediaAttempt, BboxMediaPin

LEASE = timedelta(minutes=15)
logger = logging.getLogger(__name__)


def reserve(task, principal, data_hash, config_hash):
    authorization.require(authorization.can_manage_project(principal, task.project), 'Manager role is required.')
    # Opportunistic cleanup supplements the operator-scheduled global sweep.
    # Tombstones remain for delayed writers and are revisited round-robin.
    namespace = bbox_storage.reserve_namespace()
    candidates = BboxMediaAttempt.objects.filter(task=task, storage_binding=namespace).filter(
        Q(status='abandoned') | Q(status='active', expires_at__lte=timezone.now())
    ).order_by(F('last_reconciled_at').asc(nulls_first=True), 'created_at').values_list('token', flat=True)[:10]
    for token in candidates:
        try:
            reconcile(token)
        except Exception:
            logger.warning('Unable to reconcile an earlier bbox capture attempt', exc_info=True)
    with transaction.atomic():
        project, actor = lock_project_operation(task.project_id, principal, ASSIGN)
        authorization.require(authorization.can_manage_project(principal, project), 'Manager role is required.')
        current = Task.objects.select_for_update().get(pk=task.pk, project=project)
        if (bbox.digest(current.data) != data_hash or bbox.digest(project.label_config) != config_hash
                or TaskAssignment.objects.filter(task=current).exists() or current.annotations.exists()
                or current.drafts.exists() or BboxMediaPin.objects.filter(task=current).exists()):
            raise ValidationError({'detail': 'Task, configuration or prior work changed before capture.'})
        attempts = BboxMediaAttempt.objects.select_for_update().filter(task=current, status='active')
        attempts.filter(expires_at__lte=timezone.now()).update(status='abandoned')
        if attempts.exists():
            raise ValidationError({'detail': 'A media capture is already in progress.'})
        return BboxMediaAttempt.objects.create(
            token=uuid4(), task=current, project=project, task_id_frozen=current.pk,
            project_id_frozen=project.pk, actor_id_frozen=actor.pk, storage_binding=namespace,
            expires_at=timezone.now() + LEASE,
        )


def reconcile(token, *, abandon_active=False):
    """Revoke an expired attempt before removing its candidate, repeatably.

    Tombstones are retained: a delayed writer may still create its candidate
    after one cleanup. Later reconciliations remove it, but it can never publish.
    """
    with transaction.atomic():
        attempt = BboxMediaAttempt.objects.select_for_update().get(pk=token)
        if attempt.status == 'published' or BboxMediaPin.objects.filter(token=token).exists():
            return False
        if attempt.status == 'active':
            if not abandon_active and attempt.expires_at > timezone.now() and attempt.task_id is not None:
                return False
            attempt.status = 'abandoned'
            attempt.save(update_fields=['status'])
        if attempt.status != 'abandoned':
            return False
        namespace = attempt.storage_binding
    try:
        return bbox_storage.discard_candidate(token, namespace)
    finally:
        BboxMediaAttempt.objects.filter(pk=token, status='abandoned').update(last_reconciled_at=timezone.now())
