"""Project-fenced pool configuration and one-time, authenticated claims."""

from access_control.authorization import authorization
from access_control.project_operations import (
    ASSIGN,
    BATCH_ASSIGN,
    EDIT,
    lock_project_operation,
    require_project_operation,
)
from django.db import connection, transaction
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import ProjectMember
from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import get_object_or_404
from tasks.assignments import validate_assignment_target
from tasks.models import TaskAssignment
from users.models import User

from .access import fail
from .dispatch import DispatchConflict, lock_package_tasks
from .models import TaskPackage, TaskPackageClaim, TaskPackagePool


def postgres_required():
    if connection.vendor != 'postgresql':
        fail(DispatchConflict, 'POOL_DATABASE_UNSUPPORTED', 'Claim pools require PostgreSQL locking.')


def configure_pool(*, project_id, package_id, principal, expected_revision, candidate_ids, due_at, enabled):
    postgres_required()
    with transaction.atomic():
        project, _ = lock_project_operation(project_id, principal, BATCH_ASSIGN, related_user_ids=candidate_ids)
        require_project_operation(project, ASSIGN)
        if not authorization.can_manage_project(principal, project):
            raise PermissionDenied()
        package = get_object_or_404(TaskPackage, pk=package_id, project=project)
        pool = TaskPackagePool.objects.select_for_update().filter(package=package).first()
        if (pool.revision if pool else 0) != expected_revision:
            fail(DispatchConflict, 'POOL_REVISION_CONFLICT', 'The pool configuration changed; refresh it.')
        existing_ids = set(pool.candidates.values_list('pk', flat=True)) if pool else set()
        new_ids = set(candidate_ids) - existing_ids
        active = set(User.objects.filter(pk__in=new_ids, is_active=True).values_list('pk', flat=True))
        members = set(
            OrganizationMember.objects.filter(
                organization_id=project.organization_id, user_id__in=new_ids, deleted_at__isnull=True
            ).values_list('user_id', flat=True)
        )
        roles = set(
            ProjectMember.objects.filter(
                project=project,
                user_id__in=new_ids,
                enabled=True,
                role__in=[ProjectMember.Role.ANNOTATOR, ProjectMember.Role.MANAGER],
            ).values_list('user_id', flat=True)
        )
        roles.add(project.created_by_id)
        if new_ids - (active & members & roles):
            fail(DispatchConflict, 'POOL_CANDIDATE_UNAVAILABLE', 'A candidate is not an active project member.')
        if pool and TaskPackageClaim.objects.filter(pool=pool).exists():
            if existing_ids != set(candidate_ids):
                fail(DispatchConflict, 'POOL_ALREADY_CLAIMED', 'Candidates cannot change after a claim.')
        if pool is None:
            pool = TaskPackagePool.objects.create(package=package, enabled=enabled, due_at=due_at)
        else:
            pool.enabled = enabled
            pool.due_at = due_at
            pool.revision += 1
            pool.save(update_fields=['enabled', 'due_at', 'revision', 'updated_at'])
        pool.candidates.set(candidate_ids)
        return pool


def claim_pool(*, project_id, package_id, principal, key):
    postgres_required()
    with transaction.atomic():
        project, actor = lock_project_operation(project_id, principal, BATCH_ASSIGN)
        require_project_operation(project, ASSIGN)
        require_project_operation(project, EDIT)
        if authorization.project_role(principal, project) not in ('annotator', 'manager'):
            raise PermissionDenied()
        pool = get_object_or_404(
            TaskPackagePool.objects.select_for_update(),
            package_id=package_id,
            package__project=project,
            candidates=actor,
        )
        claim = TaskPackageClaim.objects.filter(pool=pool).first()
        if claim:
            if (
                claim.idempotency_key == key
                and claim.actor_id_snapshot == actor.pk
                and claim.principal == principal.principal_id
            ):
                return claim, True
            fail(DispatchConflict, 'POOL_ALREADY_CLAIMED', 'This package has already been claimed.')
        if not pool.enabled or (pool.due_at and pool.due_at <= timezone.now()):
            fail(DispatchConflict, 'POOL_CLOSED', 'This package is not open for claims.')
        tasks = lock_package_tasks(pool.package, project)
        if TaskAssignment.objects.filter(
            task_id__in=[task.pk for task in tasks], status__in=TaskAssignment.ACTIVE_STATUSES
        ).exists():
            fail(DispatchConflict, 'POOL_TASKS_ALREADY_ASSIGNED', 'The package contains active assignments.')
        validate_assignment_target(tasks[0], actor)
        assignments = TaskAssignment.objects.bulk_create(
            [TaskAssignment(project=project, task=task, assignee=actor, assigned_by=actor) for task in tasks],
            batch_size=200,
        )
        if any(item.pk is None for item in assignments):
            raise RuntimeError('Database did not return assignment IDs')
        claim = TaskPackageClaim.objects.create(
            pool=pool,
            actor=actor,
            actor_id_snapshot=actor.pk,
            principal=principal.principal_id,
            idempotency_key=key,
            assignment_ids=[item.pk for item in assignments],
        )
        return claim, False
