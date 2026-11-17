"""Atomic dispatch of one immutable package to one eligible assignee."""

from access_control.authorization import authorization
from access_control.project_operations import ASSIGN, BATCH_ASSIGN, lock_project_operation, require_project_operation
from django.db import connection, transaction
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.generics import get_object_or_404
from tasks.assignments import validate_assignment_target
from tasks.models import Task, TaskAssignment
from users.models import User

from .access import fail
from .models import TaskPackage, TaskPackageDispatch, TaskPackageItem


class DispatchConflict(APIException):
    status_code = 409


def lock_package_tasks(package, project):
    """Lock the immutable membership and reject changed or unavailable tasks."""
    snapshot = list(
        package.items.order_by('original_task_id').values_list(
            'pk', 'task_id', 'original_task_id', 'original_created_at'
        )
    )
    if not snapshot or len(snapshot) > 1000 or any(row[1] != row[2] for row in snapshot):
        fail(DispatchConflict, 'DISPATCH_TASKS_UNAVAILABLE', 'The fixed package contains unavailable tasks.')
    tasks = list(
        Task.objects.select_for_update().filter(project=project, pk__in=[row[2] for row in snapshot]).order_by('pk')
    )
    locked_items = list(
        TaskPackageItem.objects.select_for_update()
        .filter(package=package)
        .order_by('original_task_id')
        .values_list('pk', 'task_id', 'original_task_id', 'original_created_at')
    )
    if (
        snapshot != locked_items
        or len(tasks) != len(snapshot)
        or any(task.pk != row[2] or task.created_at != row[3] for task, row in zip(tasks, snapshot))
    ):
        fail(DispatchConflict, 'DISPATCH_TASKS_UNAVAILABLE', 'The fixed package contains unavailable tasks.')
    for task in tasks:
        task.project = project
    return tasks


def dispatch_package(*, project_id, package_id, assignee_id, principal, key, request_hash):
    if connection.vendor != 'postgresql':
        fail(DispatchConflict, 'DISPATCH_DATABASE_UNSUPPORTED', 'Package dispatch requires PostgreSQL locking.')
    with transaction.atomic():
        project, actor = lock_project_operation(project_id, principal, BATCH_ASSIGN, related_user_ids=[assignee_id])
        require_project_operation(project, ASSIGN)
        if not authorization.can_manage_project(principal, project):
            raise PermissionDenied()
        package = get_object_or_404(TaskPackage, project=project, pk=package_id)
        previous = TaskPackageDispatch.objects.filter(project=project, idempotency_key=key).first()
        if previous:
            if (
                previous.request_hash != request_hash
                or previous.creator_principal != principal.principal_id
                or previous.actor_id_snapshot != actor.pk
            ):
                fail(DispatchConflict, 'DISPATCH_KEY_CONFLICT', 'This key identifies a different request.')
            return previous, True
        assignee = get_object_or_404(User, pk=assignee_id)
        tasks = lock_package_tasks(package, project)
        if TaskAssignment.objects.filter(
            task_id__in=[task.pk for task in tasks], assignee=assignee, status__in=TaskAssignment.ACTIVE_STATUSES
        ).exists():
            fail(DispatchConflict, 'DISPATCH_ALREADY_ASSIGNED', 'An active assignment already exists in this package.')
        validate_assignment_target(tasks[0], assignee)
        assignments = TaskAssignment.objects.bulk_create(
            [TaskAssignment(project=project, task=task, assignee=assignee, assigned_by=actor) for task in tasks],
            batch_size=200,
        )
        if any(item.pk is None for item in assignments):
            raise RuntimeError('Database did not return assignment IDs')
        receipt = TaskPackageDispatch.objects.create(
            project=project,
            package=package,
            assignee=assignee,
            created_by=actor,
            assignee_id_snapshot=assignee.pk,
            actor_id_snapshot=actor.pk,
            creator_principal=principal.principal_id,
            idempotency_key=key,
            request_hash=request_hash,
            assignment_ids=[item.pk for item in assignments],
        )
        return receipt, False
