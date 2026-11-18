"""Experimental project lifecycle command; activation is intentionally gated."""
from access_control.authorization import authorization
from access_control.project_operations import SECURITY_CANCEL, lock_project_operation
from django.conf import settings
from django.db import connection, transaction
from io_storages.admission import lock_drained_project
from projects.models import ProjectLifecycleEvent
from rest_framework.exceptions import PermissionDenied, ValidationError

TRANSITIONS = {
    'legacy': {'draft', 'active'},
    'draft': {'active'},
    'active': {'paused', 'completed'},
    'paused': {'active', 'completed'},
    'completed': {'active', 'archived'},
    'archived': {'active'},
}


def transition_project(project_id, principal, *, expected_revision, target, reason):
    if not getattr(settings, 'ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED', False):
        raise PermissionDenied('Project lifecycle transitions are disabled.')
    if not getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
        raise PermissionDenied('Storage admission is required for lifecycle transitions.')
    if getattr(settings, 'ANNOTATION_PROJECT_OPERATION_PROVIDER', None) != (
        'access_control.project_operations.LifecycleProjectOperationProvider'
    ):
        raise PermissionDenied('The lifecycle operation provider is required.')
    if connection.vendor != 'postgresql':
        raise ValidationError('Project lifecycle requires PostgreSQL row locks.')
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValidationError('A non-negative expected revision is required.')
    if not isinstance(target, str) or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValidationError('A target and non-empty reason are required.')
    with transaction.atomic():
        project, actor = lock_project_operation(project_id, principal, SECURITY_CANCEL)
        authorization.require(
            authorization.can_manage_project(principal, project), 'Project manager role is required.'
        )
        if project.lifecycle_revision != expected_revision:
            raise ValidationError({'reason': 'LIFECYCLE_REVISION_CONFLICT'})
        if target not in TRANSITIONS.get(project.lifecycle_state, set()):
            raise ValidationError({'reason': 'INVALID_LIFECYCLE_TRANSITION'})
        # Project-first lock is already held and stays held through the event.
        lock_drained_project(project.pk)
        old_state = project.lifecycle_state
        project.lifecycle_state = target
        project.lifecycle_revision += 1
        project.save(update_fields=['lifecycle_state', 'lifecycle_revision'])
        ProjectLifecycleEvent.objects.create(
            project=project,
            previous_state=old_state,
            next_state=target,
            revision=project.lifecycle_revision,
            actor=actor,
            actor_id_snapshot=actor.pk,
            reason=reason.strip(),
        )
        return project
