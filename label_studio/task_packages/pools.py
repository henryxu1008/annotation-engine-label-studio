"""Project-fenced pool configuration and one-time, authenticated claims."""

from access_control.authorization import authorization
from access_control.project_operations import (
    ASSIGN,
    BATCH_ASSIGN,
    EDIT,
    lock_project_operation,
    require_project_operation,
)
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import ProjectMember
from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import get_object_or_404
from tasks.assignments import validate_assignment_target
from tasks.models import TaskAssignment
from users.models import User

from .access import fail
from .dispatch import DispatchConflict, dispatch_package, lock_package_tasks
from .models import TaskPackage, TaskPackageClaim, TaskPackageClaimHistory, TaskPackageDispatch, TaskPackagePool


def postgres_required():
    if connection.vendor != 'postgresql':
        fail(DispatchConflict, 'POOL_DATABASE_UNSUPPORTED', 'Claim pools require PostgreSQL locking.')


def local_automation_identity():
    return getattr(settings, 'ANNOTATION_IDENTITY_PROVIDER', None) in (
        None,
        'access_control.identity.LocalIdentityProvider',
    )


def configure_pool(
    *,
    project_id,
    package_id,
    principal,
    expected_revision,
    candidate_ids,
    due_at,
    enabled,
    priority=None,
    automatic=None,
):
    postgres_required()
    with transaction.atomic():
        project, _ = lock_project_operation(project_id, principal, BATCH_ASSIGN, related_user_ids=candidate_ids)
        require_project_operation(project, ASSIGN)
        if not authorization.can_manage_project(principal, project):
            raise PermissionDenied()
        if automatic and (
            not settings.ANNOTATION_PACKAGE_AUTOMATION_ENABLED
            or not settings.ANNOTATION_PACKAGE_ADVANCED_ENABLED
            or not settings.ANNOTATION_PACKAGE_DISPATCH_ENABLED
            or not settings.ANNOTATION_PACKAGE_CLAIM_ENABLED
            or not local_automation_identity()
            or principal.source != 'label_studio_local'
            or principal.principal_id != f'label_studio_local:{principal.local_user_id}'
        ):
            fail(DispatchConflict, 'POOL_AUTOMATION_UNAVAILABLE', 'Automatic allocation requires a local manager.')
        package = get_object_or_404(TaskPackage, pk=package_id, project=project)
        pool = TaskPackagePool.objects.select_for_update().filter(package=package).first()
        if (pool.revision if pool else 0) != expected_revision:
            fail(DispatchConflict, 'POOL_REVISION_CONFLICT', 'The pool configuration changed; refresh it.')
        if pool and pool.automatic and automatic is None:
            fail(DispatchConflict, 'POOL_AUTOMATION_CONFIRM_REQUIRED', 'Confirm the current automation setting.')
        if pool and pool.automatic and automatic and pool.automation_actor_id != principal.local_user_id:
            fail(DispatchConflict, 'POOL_AUTOMATION_OWNER_CHANGED', 'Disable automation before changing its manager.')
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
            pool = TaskPackagePool.objects.create(
                package=package,
                enabled=enabled,
                due_at=due_at,
                priority=priority or 0,
                automatic=bool(automatic),
                automation_actor_id=principal.local_user_id if automatic else None,
                automation_generation=1 if automatic else 0,
            )
        else:
            pool.enabled = enabled
            pool.due_at = due_at
            if priority is not None:
                pool.priority = priority
            if automatic is not None:
                if automatic and not pool.automatic:
                    pool.automation_generation += 1
                pool.automatic = automatic
                pool.automation_actor_id = principal.local_user_id if automatic else None
            pool.revision += 1
            pool.save(
                update_fields=[
                    'enabled',
                    'due_at',
                    'priority',
                    'automatic',
                    'automation_actor',
                    'automation_generation',
                    'revision',
                    'updated_at',
                ]
            )
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
        old_claim = TaskPackageClaimHistory.objects.filter(
            pool=pool, idempotency_key=key, actor_id_snapshot=actor.pk, principal=principal.principal_id
        ).first()
        if old_claim:
            return old_claim, True, True
        claim = TaskPackageClaim.objects.filter(pool=pool).first()
        if claim:
            if (
                claim.idempotency_key == key
                and claim.actor_id_snapshot == actor.pk
                and claim.principal == principal.principal_id
            ):
                return claim, True, False
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
        if pool.allocation_id is not None:
            pool.allocation = None
            pool.save(update_fields=['allocation', 'updated_at'])
        return claim, False, False


def reopen_pool(*, project_id, package_id, principal, expected_revision, claim_id, key):
    postgres_required()
    with transaction.atomic():
        project, actor = lock_project_operation(project_id, principal, BATCH_ASSIGN)
        require_project_operation(project, ASSIGN)
        if not authorization.can_manage_project(principal, project):
            raise PermissionDenied()
        pool = get_object_or_404(
            TaskPackagePool.objects.select_for_update(), package_id=package_id, package__project=project
        )
        previous = TaskPackageClaimHistory.objects.filter(pool=pool, reopen_key=key).first()
        if previous:
            if (
                previous.original_claim_id != claim_id
                or previous.reopen_revision != expected_revision
                or previous.reopened_by_id_snapshot != actor.pk
                or previous.reopened_by_principal != principal.principal_id
            ):
                fail(DispatchConflict, 'POOL_KEY_CONFLICT', 'The key belongs to another reopening.')
            return previous, True
        if pool.revision != expected_revision:
            fail(DispatchConflict, 'POOL_REVISION_CONFLICT', 'The pool configuration changed; refresh it.')
        claim = get_object_or_404(TaskPackageClaim.objects.select_for_update(), pool=pool, pk=claim_id)
        tasks = lock_package_tasks(pool.package, project)
        task_ids = {task.pk for task in tasks}
        assignments = list(
            TaskAssignment.objects.select_for_update().filter(pk__in=claim.assignment_ids).order_by('pk')
        )
        if (
            len(assignments) != len(claim.assignment_ids)
            or len(assignments) != len(tasks)
            or {a.task_id for a in assignments} != task_ids
            or any(
                a.project_id != project.pk
                or a.assignee_id != claim.actor_id_snapshot
                or a.status != TaskAssignment.Status.CANCELLED
                for a in assignments
            )
            or TaskAssignment.objects.filter(task_id__in=task_ids, status__in=TaskAssignment.ACTIVE_STATUSES).exists()
        ):
            fail(DispatchConflict, 'POOL_DRAIN_INCOMPLETE', 'All original assignments must be verifiably cancelled.')
        history = TaskPackageClaimHistory.objects.create(
            pool=pool,
            original_claim_id=claim.pk,
            actor_id_snapshot=claim.actor_id_snapshot,
            principal=claim.principal,
            idempotency_key=claim.idempotency_key,
            assignment_ids=claim.assignment_ids,
            claimed_at=claim.created_at,
            reopened_by_id_snapshot=actor.pk,
            reopened_by_principal=principal.principal_id,
            reopen_key=key,
            reopen_revision=expected_revision,
        )
        claim.delete()
        pool.revision += 1
        # A reopened pool must not be silently reassigned before a manager
        # deliberately enables automation for the new generation.
        pool.automatic = False
        pool.automation_actor = None
        pool.save(update_fields=['revision', 'automatic', 'automation_actor', 'updated_at'])
        return history, False


def balanced_dispatch(*, project_id, package_id, principal, key, request_hash, automation_revision=None):
    """Assign the whole package to the eligible member with the fewest active tasks."""
    postgres_required()
    with transaction.atomic():
        # Snapshot without acquiring identity locks; all identities must then be
        # locked together in the shared order, even across different projects.
        candidate_ids = list(
            TaskPackagePool.candidates.through.objects.filter(
                taskpackagepool__package_id=package_id, taskpackagepool__package__project_id=project_id
            )
            .order_by('user_id')
            .values_list('user_id', flat=True)
        )
        project, actor = lock_project_operation(project_id, principal, BATCH_ASSIGN, related_user_ids=candidate_ids)
        require_project_operation(project, ASSIGN)
        if not authorization.can_manage_project(principal, project):
            raise PermissionDenied()
        pool = get_object_or_404(
            TaskPackagePool.objects.select_for_update(), package_id=package_id, package__project=project
        )
        if candidate_ids != list(pool.candidates.order_by('pk').values_list('pk', flat=True)):
            fail(DispatchConflict, 'POOL_CANDIDATES_CHANGED', 'Candidate configuration changed; retry.')
        if automation_revision is not None and (
            not settings.ANNOTATION_PACKAGE_AUTOMATION_ENABLED
            or not settings.ANNOTATION_PACKAGE_ADVANCED_ENABLED
            or not settings.ANNOTATION_PACKAGE_DISPATCH_ENABLED
            or not settings.ANNOTATION_PACKAGE_CLAIM_ENABLED
            or not local_automation_identity()
            or not pool.automatic
            or pool.automation_actor_id != actor.pk
            or pool.revision != automation_revision
        ):
            fail(DispatchConflict, 'POOL_AUTOMATION_CHANGED', 'Automatic allocation configuration changed.')
        previous = TaskPackageDispatch.objects.filter(project=project, idempotency_key=key).first()
        if previous:
            if (
                previous.request_hash != request_hash
                or previous.creator_principal != principal.principal_id
                or previous.actor_id_snapshot != actor.pk
            ):
                fail(DispatchConflict, 'DISPATCH_KEY_CONFLICT', 'This key identifies a different request.')
            return previous, True
        if (
            not pool.enabled
            or (pool.due_at and pool.due_at <= timezone.now())
            or TaskPackageClaim.objects.filter(pool=pool).exists()
        ):
            fail(DispatchConflict, 'POOL_CLOSED', 'This package is not open for allocation.')
        eligible_org = OrganizationMember.objects.filter(
            organization_id=project.organization_id,
            user_id__in=candidate_ids,
            deleted_at__isnull=True,
            user__is_active=True,
        ).values('user_id')
        eligible_role = set(
            ProjectMember.objects.filter(
                project=project,
                enabled=True,
                role__in=[ProjectMember.Role.ANNOTATOR, ProjectMember.Role.MANAGER],
                user_id__in=eligible_org,
            ).values_list('user_id', flat=True)
        )
        if project.created_by_id in candidate_ids and project.created_by_id in set(
            eligible_org.values_list('user_id', flat=True)
        ):
            eligible_role.add(project.created_by_id)
        if not eligible_role:
            fail(DispatchConflict, 'POOL_CANDIDATE_UNAVAILABLE', 'No eligible candidate is available.')
        tasks = lock_package_tasks(pool.package, project)
        if TaskAssignment.objects.filter(
            task_id__in=[task.pk for task in tasks], status__in=TaskAssignment.ACTIVE_STATUSES
        ).exists():
            fail(DispatchConflict, 'POOL_TASKS_ALREADY_ASSIGNED', 'The package contains active assignments.')
        loads = {
            row['assignee_id']: row['total']
            for row in TaskAssignment.objects.filter(
                project=project, assignee_id__in=eligible_role, status__in=TaskAssignment.ACTIVE_STATUSES
            )
            .values('assignee_id')
            .annotate(total=Count('pk'))
        }
        target = min(eligible_role, key=lambda user_id: (loads.get(user_id, 0), user_id))
        receipt, replayed = dispatch_package(
            project_id=project_id,
            package_id=package_id,
            assignee_id=target,
            principal=principal,
            key=key,
            request_hash=request_hash,
        )
        if not replayed:
            pool.allocation = receipt
            pool.save(update_fields=['allocation', 'updated_at'])
        return receipt, replayed
