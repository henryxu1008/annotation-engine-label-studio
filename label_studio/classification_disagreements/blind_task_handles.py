"""Opaque handles for participant work; the read/UI contract is not exposed.

Both ordinary and quality assignments must use this same projection before it
can be exposed. Project statistics, ordering, timing and all nested resource
identifiers still need a uniform participant-facing contract.
The labeling write adapter accepts these handles but still uses existing URLs.
"""

from secrets import randbits

from access_control.authorization import authorization
from access_control.identity import LOCAL_IDENTITY_SOURCE
from django.db import IntegrityError, connections, transaction
from rest_framework.exceptions import NotFound
from tasks.models import TaskAssignment

from .models import BlindTaskHandle

HANDLE_BASE = 1 << 51
HANDLE_LIMIT = 1 << 52  # Precisely representable by JavaScript numbers.


def _unavailable():
    raise NotFound('Work is unavailable.')


def _token():
    return HANDLE_BASE | randbits(51)


def _lock_bounded_task(*, project_id, task_id):
    """Lock a text task and check database bytes before loading its JSON."""
    from tasks.models import Task
    from .gold_api import MAX_BYTES, bounded_query

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('Bounded task reads require an atomic transaction.')
    task = bounded_query(Task.objects.select_for_update(), 'data').filter(
        pk=task_id, project_id=project_id,
    ).first()
    if task is None or type(task._gold_bytes) is not int or not 0 <= task._gold_bytes <= MAX_BYTES:
        _unavailable()
    return task


def _validate(principal, assignment, record=None):
    from users.models import User

    actor_id = principal.local_user_id
    if (type(actor_id) is not int or principal.source != LOCAL_IDENTITY_SOURCE
            or principal.principal_id != f'{LOCAL_IDENTITY_SOURCE}:{actor_id}'
            or assignment is None or assignment.assignee_id != actor_id
            or assignment.status not in TaskAssignment.ACTIVE_STATUSES
            or assignment.task_id is None or assignment.project_id != assignment.task.project_id
            or not User.objects.filter(pk=actor_id, is_active=True,
                                       active_organization_id=assignment.project.organization_id).exists()):
        _unavailable()
    live = authorization.active_task_assignment(principal, assignment.task)
    if live is None or live.pk != assignment.pk or live.project_id != assignment.project_id:
        _unavailable()
    if record is not None and (record.assignment_id_frozen != assignment.pk
                               or record.task_id_frozen != assignment.task_id
                               or record.project_id_frozen != assignment.project_id
                               or record.actor_id_frozen != actor_id):
        _unavailable()
    return assignment


def validate_locked_handle(*, principal, record, assignment):
    """Validate an immutable handle against the caller's already-locked work."""
    db = transaction.get_connection()
    if not db.in_atomic_block:
        raise RuntimeError('Handle validation requires an open transaction.')
    if (db.vendor not in ('postgresql', 'sqlite')
            or not HANDLE_BASE <= record.task_handle < HANDLE_LIMIT
            or not HANDLE_BASE <= record.assignment_handle < HANDLE_LIMIT):
        _unavailable()
    return _validate(principal, assignment, record)


@transaction.atomic
def issue_handle(*, principal, assignment_id):
    """Issue one stable, randomly distributed pair after checking live access."""
    if type(assignment_id) is not int or assignment_id <= 0 or type(principal.local_user_id) is not int:
        _unavailable()
    using = 'default'
    if connections[using].vendor not in ('postgresql', 'sqlite'):
        _unavailable()
    # Lock in the same project -> task -> assignment order as assignment writes.
    from projects.models import Project
    scope = TaskAssignment.objects.filter(pk=assignment_id).values('project_id', 'task_id').first()
    if scope is None:
        _unavailable()
    project = Project.objects.select_for_update().filter(pk=scope['project_id']).first()
    if project is None:
        _unavailable()
    task = _lock_bounded_task(task_id=scope['task_id'], project_id=scope['project_id'])
    assignment = TaskAssignment.objects.select_for_update().filter(
        pk=assignment_id, project_id=scope['project_id'], task_id=scope['task_id'],
    ).first()
    if assignment is None:
        _unavailable()
    task.project = project
    assignment.task, assignment.project = task, project
    existing = BlindTaskHandle.objects.filter(assignment_id_frozen=assignment_id).first()
    # Check the live assignment once while its project, task and assignment are
    # locked. Existing handles still have all frozen bindings checked here.
    _validate(principal, assignment, existing)
    if existing is not None:
        return existing
    for _ in range(16):
        try:
            with transaction.atomic():
                return BlindTaskHandle.objects.create(
                    assignment_id_frozen=assignment.pk, task_id_frozen=assignment.task_id,
                    project_id_frozen=assignment.project_id, actor_id_frozen=principal.local_user_id,
                    task_handle=_token(), assignment_handle=_token(),
                )
        except IntegrityError:
            existing = BlindTaskHandle.objects.filter(assignment_id_frozen=assignment_id).first()
            if existing is not None:
                _validate(principal, assignment, existing)
                return existing
    _unavailable()


def resolve_handle(*, principal, task_handle, assignment_handle=None):
    """Resolve only for the live owner; an opaque handle never grants access."""
    if (type(principal.local_user_id) is not int or type(task_handle) is not int
            or not HANDLE_BASE <= task_handle < HANDLE_LIMIT
            or (assignment_handle is not None and (type(assignment_handle) is not int
                                                    or not HANDLE_BASE <= assignment_handle < HANDLE_LIMIT))):
        _unavailable()
    record = BlindTaskHandle.objects.filter(task_handle=task_handle, actor_id_frozen=principal.local_user_id).first()
    if record is None or (assignment_handle is not None and record.assignment_handle != assignment_handle):
        _unavailable()
    assignment = TaskAssignment.objects.select_related('task__project', 'project').filter(
        pk=record.assignment_id_frozen,
    ).first()
    return _validate(principal, assignment, record)


def lock_handle_for_write(*, principal, task_handle, assignment_handle, assignment_version,
                          expected_task_id=None, expected_project_id=None):
    """Resolve a handle under the labeling-write locks and optimistic version.

    The caller must keep its transaction open through the actual write. The
    opaque identifiers are lookup keys; live authorization and the quality
    proof remain authoritative after every lock is acquired.
    """
    from access_control.project_operations import EDIT, lock_project_operation
    from tasks.exceptions import AssignmentConflictError
    from .blind_provenance import checked_quality_proof
    from .models import BlindQualityAssignment
    from rest_framework.exceptions import ValidationError

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('Handle-based writes require an open transaction.')
    if (type(task_handle) is not int or type(assignment_handle) is not int
            or type(assignment_version) is not int or assignment_version <= 0
            or not HANDLE_BASE <= task_handle < HANDLE_LIMIT
            or not HANDLE_BASE <= assignment_handle < HANDLE_LIMIT
            or type(principal.local_user_id) is not int):
        _unavailable()
    record = BlindTaskHandle.objects.filter(
        task_handle=task_handle, assignment_handle=assignment_handle, actor_id_frozen=principal.local_user_id,
    ).first()
    if record is None:
        _unavailable()
    # A caller that already holds a task lock must not acquire a different
    # task's lock: crossed handles could otherwise invert the lock order.
    if ((expected_task_id is not None and record.task_id_frozen != expected_task_id)
            or (expected_project_id is not None and record.project_id_frozen != expected_project_id)):
        raise AssignmentConflictError()
    project, _ = lock_project_operation(record.project_id_frozen, principal, EDIT)
    task = _lock_bounded_task(task_id=record.task_id_frozen, project_id=project.pk)
    assignment = TaskAssignment.objects.select_for_update().filter(
        pk=record.assignment_id_frozen, task=task, project=project,
    ).first()
    if assignment is None:
        _unavailable()
    task.project = project
    assignment.task, assignment.project = task, project
    _validate(principal, assignment, record)
    if not authorization.can_label_task(principal, task) or assignment.version != assignment_version:
        raise AssignmentConflictError()
    if BlindQualityAssignment.objects.filter(task_id=task.pk).exists():
        marker = BlindQualityAssignment.objects.filter(assignment_id=assignment.pk, task_id=task.pk).first()
        if marker is None:
            raise AssignmentConflictError()
        try:
            checked_quality_proof(assignment=assignment, marker=marker)
        except ValidationError:
            raise AssignmentConflictError()
    return assignment
