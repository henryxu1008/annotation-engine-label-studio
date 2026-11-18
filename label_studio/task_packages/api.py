"""Fixed membership plus live, authorized read projections over existing work."""

import hashlib
import json
import re
from uuid import UUID

from access_control.authorization import authorization
from access_control.identity import resolve_actor
from access_control.project_operations import (
    ASSIGN,
    BATCH_ASSIGN,
    EDIT,
    ORGANIZE_WORK,
    READ_WORK,
    REVIEW,
    lock_project_operation,
    require_project_operation,
)
from core.permissions import all_permissions
from django.conf import settings
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone
from projects.models import Project, ProjectMember
from rest_framework import generics, serializers
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from tasks.models import Task, TaskAssignment
from tasks.my_work import annotate_assignment_work, work_projects

from .access import current_read_policy, fail, visible_packages
from .dispatch import dispatch_package
from .models import TaskPackage, TaskPackageItem, TaskPackageOverdueNotice, TaskPackagePool
from .pools import (
    balanced_dispatch,
    claim_pool,
    configure_pool,
    local_automation_identity,
    postgres_required,
    reopen_pool,
)
from .progress import PackageWork, filter_states, requested_state


class Conflict(APIException):
    status_code = 409


class CreatePackage(serializers.Serializer):
    name = serializers.CharField(max_length=160)
    instructions = serializers.CharField(max_length=4000, allow_blank=True, default='')
    source_note = serializers.CharField(max_length=500, allow_blank=True, default='')
    task_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), min_length=1, max_length=1000)

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            fail(ValidationError, 'PACKAGE_INVALID_INPUT', 'Only package metadata and explicit task_ids are accepted.')
        return super().to_internal_value(data)

    def validate_task_ids(self, value):
        if len(value) != len(set(value)):
            fail(ValidationError, 'PACKAGE_INVALID_INPUT', 'Task IDs must be unique.')
        return sorted(value)


class PoolConfiguration(serializers.Serializer):
    expected_revision = serializers.IntegerField(min_value=0)
    candidate_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), min_length=1, max_length=100)
    due_at = serializers.DateTimeField(allow_null=True)
    enabled = serializers.BooleanField()
    priority = serializers.IntegerField(min_value=0, max_value=100, required=False)
    automatic = serializers.BooleanField(required=False)

    def to_internal_value(self, data):
        if (
            not isinstance(data, dict)
            or set(data) - set(self.fields)
            or not {'expected_revision', 'candidate_ids', 'due_at', 'enabled'} <= set(data)
        ):
            fail(ValidationError, 'POOL_INVALID_INPUT', 'All pool configuration fields are required.')
        due = data.get('due_at')
        if due is not None and (not isinstance(due, str) or not re.search(r'(Z|[+-]\d\d:\d\d)$', due)):
            fail(ValidationError, 'POOL_INVALID_INPUT', 'Deadline must include a UTC offset.')
        if type(data['expected_revision']) is not int or type(data['enabled']) is not bool:
            fail(ValidationError, 'POOL_INVALID_INPUT', 'Invalid revision or enabled value.')
        if 'priority' in data and type(data['priority']) is not int:
            fail(ValidationError, 'POOL_INVALID_INPUT', 'Priority must be an integer.')
        if 'automatic' in data and type(data['automatic']) is not bool:
            fail(ValidationError, 'POOL_INVALID_INPUT', 'Automatic allocation must be a boolean.')
        ids = data['candidate_ids']
        if not isinstance(ids, list) or any(type(value) is not int for value in ids) or len(ids) != len(set(ids)):
            fail(ValidationError, 'POOL_INVALID_INPUT', 'Candidate IDs must be distinct positive integers.')
        return super().to_internal_value(data)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    ).hexdigest()


def package_metadata(package, role):
    data = {
        'id': package.pk,
        'name': package.name,
        'instructions': package.instructions,
        'created_at': package.created_at,
    }
    if role == ProjectMember.Role.MANAGER:
        data.update(source_note=package.source_note, membership_hash=package.membership_hash)
    return data


class PackagePagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 100

    def paginate_queryset(self, queryset, request, view=None):
        try:
            return super().paginate_queryset(queryset, request, view)
        except NotFound as exc:
            exc.display_context = {'reason': 'PACKAGE_PAGE_OUT_OF_RANGE'}
            raise


class PackageAPIBase(generics.GenericAPIView):
    permission_required = all_permissions.tasks_view
    pagination_class = PackagePagination

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'private, no-store'
        return response

    def context(self, request, pk, *, capability=False, filters=()):
        if set(request.query_params) - {'page', 'page_size', *filters}:
            fail(ValidationError, 'PACKAGE_INVALID_INPUT', 'Only pagination filters are supported.')
        principal, actor = resolve_actor(request)
        project = generics.get_object_or_404(
            Project.objects.filter(pk__in=work_projects(actor, ['manager', 'annotator', 'reviewer'])),
            pk=pk,
        )
        role = authorization.project_role(principal, project)
        if role not in ('manager', 'annotator', 'reviewer'):
            raise PermissionDenied()
        policy = require_project_operation(project, READ_WORK)
        if not capability and not settings.ANNOTATION_FIXED_PACKAGES_ENABLED:
            raise NotFound()
        return principal, actor, project, role, policy


class PackageCapabilityAPI(PackageAPIBase):
    def get(self, request, pk):
        _, actor, project, role, _ = self.context(request, pk, capability=True)
        policy = current_read_policy(project, actor, role)
        enabled = settings.ANNOTATION_FIXED_PACKAGES_ENABLED
        return Response(
            {'enabled': enabled, 'can_create': enabled and role == 'manager' and policy.allows(ORGANIZE_WORK)}
        )


class PackageListAPI(PackageAPIBase):
    def get(self, request, pk):
        _, actor, project, role, _ = self.context(request, pk)
        page = self.paginate_queryset(visible_packages(project, actor, role).order_by('-pk'))
        ids = [package.pk for package in page]
        counts = PackageWork(project, actor, role, ids).summaries(ids) if ids else {}
        response = self.get_paginated_response(
            [{**package_metadata(package, role), 'counts': counts[package.pk]} for package in page]
        )
        policy = current_read_policy(project, actor, role, package_ids=ids)
        response.data.update(
            role=role,
            can_create=role == 'manager' and policy.allows(ORGANIZE_WORK),
            as_of=timezone.now(),
            can_discover_pools=settings.ANNOTATION_PACKAGE_CLAIM_ENABLED and role in ('annotator', 'manager'),
        )
        return response

    def post(self, request, pk):
        principal, _, _, _, _ = self.context(request, pk)
        serializer = CreatePackage(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            key = UUID(request.headers.get('Idempotency-Key', ''))
        except (ValueError, AttributeError):
            fail(ValidationError, 'PACKAGE_INVALID_INPUT', 'A UUID Idempotency-Key header is required.')
        payload = serializer.validated_data
        request_hash = digest(payload)
        with transaction.atomic():
            project, actor = lock_project_operation(pk, principal, ORGANIZE_WORK)
            if not authorization.can_manage_project(principal, project):
                raise PermissionDenied()
            previous = TaskPackage.objects.filter(project=project, idempotency_key=key).first()
            if previous:
                if (
                    previous.request_hash != request_hash
                    or previous.creator_principal != principal.principal_id
                    or previous.created_by_id != actor.pk
                ):
                    fail(Conflict, 'PACKAGE_KEY_CONFLICT', 'This key already identifies another creation request.')
                return Response({**package_metadata(previous, 'manager'), 'replayed': True}, status=200)
            tasks = list(
                Task.objects.select_for_update().filter(project=project, pk__in=payload['task_ids']).order_by('pk')
            )
            if len(tasks) != len(payload['task_ids']):
                fail(
                    ValidationError,
                    'PACKAGE_TASKS_UNAVAILABLE',
                    'Some selected tasks are unavailable in this project.',
                )
            package = TaskPackage.objects.create(
                project=project,
                created_by=actor,
                creator_principal=principal.principal_id,
                idempotency_key=key,
                request_hash=request_hash,
                membership_hash=digest(payload['task_ids']),
                **{field: payload[field] for field in ('name', 'instructions', 'source_note')},
            )
            TaskPackageItem.objects.bulk_create(
                [
                    TaskPackageItem(
                        package=package, task=task, original_task_id=task.pk, original_created_at=task.created_at
                    )
                    for task in tasks
                ]
            )
        return Response({**package_metadata(package, 'manager'), 'replayed': False}, status=201)


class PackageDispatchAPI(PackageAPIBase):
    def post(self, request, pk, package_id):
        principal, _, _, _, _ = self.context(request, pk)
        if not settings.ANNOTATION_PACKAGE_DISPATCH_ENABLED:
            raise NotFound()
        if (
            not isinstance(request.data, dict)
            or set(request.data) != {'assignee_id'}
            or type(request.data['assignee_id']) is not int
            or request.data['assignee_id'] < 1
        ):
            fail(ValidationError, 'DISPATCH_INVALID_INPUT', 'Provide only a positive integer assignee_id.')
        try:
            key = UUID(request.headers.get('Idempotency-Key', ''))
        except (ValueError, AttributeError):
            fail(ValidationError, 'DISPATCH_INVALID_INPUT', 'A UUID Idempotency-Key header is required.')
        assignee_id = request.data['assignee_id']
        receipt, replayed = dispatch_package(
            project_id=pk,
            package_id=package_id,
            assignee_id=assignee_id,
            principal=principal,
            key=key,
            request_hash=digest({'package_id': package_id, 'assignee_id': assignee_id}),
        )
        return Response(
            {
                'dispatch_id': receipt.pk,
                'package_id': receipt.package_id,
                'assignee_id': receipt.assignee_id_snapshot,
                'assignment_ids': receipt.assignment_ids,
                'assignment_count': len(receipt.assignment_ids),
                'replayed': replayed,
            },
            status=200 if replayed else 201,
        )


class PackagePoolAPI(PackageAPIBase):
    def get(self, request, pk, package_id):
        principal, _, project, _, _ = self.context(request, pk)
        if not settings.ANNOTATION_PACKAGE_CLAIM_ENABLED:
            raise NotFound()
        if not authorization.can_manage_project(principal, project):
            raise PermissionDenied()
        package = generics.get_object_or_404(TaskPackage, pk=package_id, project=project)
        pool = TaskPackagePool.objects.filter(package=package).first()
        overdue = overdue_counts([pool], project) if pool else {}
        if not authorization.can_manage_project(principal, project):
            raise PermissionDenied()
        return Response(
            {
                'revision': pool.revision if pool else 0,
                'enabled': pool.enabled if pool else False,
                'candidate_ids': list(pool.candidates.order_by('pk').values_list('pk', flat=True)) if pool else [],
                'due_at': pool.due_at if pool else None,
                'claimed': hasattr(pool, 'claim') if pool else False,
                'claim_id': pool.claim.pk if pool and hasattr(pool, 'claim') else None,
                'priority': pool.priority if pool else 0,
                'advanced_enabled': settings.ANNOTATION_PACKAGE_ADVANCED_ENABLED,
                'automation_available': settings.ANNOTATION_PACKAGE_AUTOMATION_ENABLED
                and settings.ANNOTATION_PACKAGE_ADVANCED_ENABLED
                and settings.ANNOTATION_PACKAGE_DISPATCH_ENABLED
                and local_automation_identity(),
                'automatic': pool.automatic if pool else False,
                'overdue_count': overdue.get(pool.pk, 0) if pool else 0,
            }
        )

    def put(self, request, pk, package_id):
        principal, _, _, _, _ = self.context(request, pk)
        if not settings.ANNOTATION_PACKAGE_CLAIM_ENABLED:
            raise NotFound()
        serializer = PoolConfiguration(data=request.data)
        serializer.is_valid(raise_exception=True)
        if 'priority' in serializer.validated_data and not settings.ANNOTATION_PACKAGE_ADVANCED_ENABLED:
            raise NotFound()
        if serializer.validated_data.get('automatic') and not settings.ANNOTATION_PACKAGE_AUTOMATION_ENABLED:
            raise NotFound()
        pool = configure_pool(project_id=pk, package_id=package_id, principal=principal, **serializer.validated_data)
        return Response(
            {
                'revision': pool.revision,
                'enabled': pool.enabled,
                'priority': pool.priority,
                'automatic': pool.automatic,
                'due_at': pool.due_at,
                'candidate_ids': sorted(serializer.validated_data['candidate_ids']),
            }
        )


def overdue_source(pool):
    """Return the current pool-owned receipt, never an arbitrary direct dispatch."""
    if hasattr(pool, 'claim'):
        claim = pool.claim
        if claim.actor_id is not None and claim.actor_id == claim.actor_id_snapshot:
            return claim.actor_id, claim.assignment_ids, {'claim_id_snapshot': claim.pk}
        return None
    if pool.allocation_id:
        allocation = pool.allocation
        if (
            allocation.package_id == pool.package_id
            and allocation.project_id == pool.package.project_id
            and allocation.assignee_id is not None
            and allocation.assignee_id == allocation.assignee_id_snapshot
        ):
            return allocation.assignee_id, allocation.assignment_ids, {'dispatch_id_snapshot': allocation.pk}
    return None


def overdue_counts(pools, project):
    """Count unfinished work belonging to the current pool receipt."""
    now = timezone.now()
    by_assignment = {}
    for pool in pools:
        source = overdue_source(pool) if pool.due_at and pool.due_at <= now else None
        if source:
            actor_id, assignment_ids, _ = source
            for assignment_id in assignment_ids:
                by_assignment.setdefault(assignment_id, []).append((pool.pk, actor_id))
    counts = {}
    if by_assignment:
        assignments = TaskAssignment.objects.filter(
            project=project, pk__in=by_assignment, status__in=TaskAssignment.ACTIVE_STATUSES
        )
        unfinished = annotate_assignment_work(assignments).filter(
            work_state__in=['assigned', 'in_progress', 'rework', 'conflict']
        )
        for assignment_id, assignee_id in unfinished.values_list('pk', 'assignee_id'):
            for pool_id, owner_id in by_assignment[assignment_id]:
                if owner_id == assignee_id:
                    counts[pool_id] = counts.get(pool_id, 0) + 1
    return counts


class AvailablePoolsAPI(PackageAPIBase):
    def get(self, request, pk):
        principal, actor, project, role, policy = self.context(request, pk)
        if not settings.ANNOTATION_PACKAGE_CLAIM_ENABLED:
            raise NotFound()
        if role not in ('annotator', 'manager'):
            raise PermissionDenied()
        occupied = TaskAssignment.objects.filter(
            task__package_items__package_id=OuterRef('package_id'), status__in=TaskAssignment.ACTIVE_STATUSES
        )
        personal_work = occupied.filter(assignee=actor)
        visible = (
            Q(candidates=actor, enabled=True, claim__isnull=True, occupied=False)
            | Q(candidates=actor, claim__actor=actor)
            | Q(claim__isnull=True, allocation__assignee=actor, personal_work=True)
        )
        pools = (
            TaskPackagePool.objects.filter(
                package__project=project, package__project_id__in=work_projects(actor, [role])
            )
            .annotate(occupied=Exists(occupied), personal_work=Exists(personal_work))
            .filter(visible)
            .distinct()
            .select_related('package', 'claim', 'allocation')
            .order_by('-priority', 'pk')
        )
        page = self.paginate_queryset(pools)
        now = timezone.now()
        overdue = overdue_counts(page, project)
        current = set(
            TaskPackagePool.objects.filter(
                pk__in=[pool.pk for pool in page],
                package__project_id__in=work_projects(actor, [role]),
            )
            .annotate(occupied=Exists(occupied), personal_work=Exists(personal_work))
            .filter(visible)
            .distinct()
            .values_list('pk', flat=True)
        )
        if current != {pool.pk for pool in page} or authorization.project_role(principal, project) != role:
            fail(PermissionDenied, 'POOL_ACCESS_CHANGED', 'Claim pool access changed; refresh the page.')
        return self.get_paginated_response(
            [
                {
                    'package_id': pool.package_id,
                    'name': pool.package.name,
                    'due_at': pool.due_at,
                    'priority': pool.priority,
                    'claimed': hasattr(pool, 'claim'),
                    'allocated': not hasattr(pool, 'claim')
                    and pool.allocation_id is not None
                    and pool.allocation.assignee_id == actor.pk
                    and pool.personal_work,
                    'deadline_passed': bool(pool.due_at and pool.due_at <= now),
                    'overdue_count': overdue.get(pool.pk, 0),
                    'can_claim': not hasattr(pool, 'claim')
                    and not pool.occupied
                    and pool.enabled
                    and (not pool.due_at or pool.due_at > now)
                    and policy.allows(BATCH_ASSIGN)
                    and policy.allows(ASSIGN)
                    and policy.allows(EDIT),
                }
                for pool in page
            ]
        )


class PackageClaimAPI(PackageAPIBase):
    def post(self, request, pk, package_id):
        principal, _, _, _, _ = self.context(request, pk)
        if not settings.ANNOTATION_PACKAGE_CLAIM_ENABLED:
            raise NotFound()
        if request.data:
            fail(ValidationError, 'POOL_INVALID_INPUT', 'No claim fields are accepted.')
        try:
            key = UUID(request.headers.get('Idempotency-Key', ''))
        except (ValueError, AttributeError):
            fail(ValidationError, 'POOL_INVALID_INPUT', 'A UUID Idempotency-Key header is required.')
        claim, replayed, released = claim_pool(project_id=pk, package_id=package_id, principal=principal, key=key)
        return Response(
            {
                'package_id': package_id,
                'assignment_ids': claim.assignment_ids,
                'assignment_count': len(claim.assignment_ids),
                'replayed': replayed,
                'released': released,
            },
            status=200 if replayed else 201,
        )


class PackageReopenAPI(PackageAPIBase):
    def post(self, request, pk, package_id):
        principal, _, _, _, _ = self.context(request, pk)
        if not settings.ANNOTATION_PACKAGE_CLAIM_ENABLED or not settings.ANNOTATION_PACKAGE_ADVANCED_ENABLED:
            raise NotFound()
        data = request.data
        if (
            not isinstance(data, dict)
            or set(data) != {'expected_revision', 'claim_id'}
            or type(data['expected_revision']) is not int
            or data['expected_revision'] < 1
            or type(data['claim_id']) is not int
            or data['claim_id'] < 1
        ):
            fail(ValidationError, 'POOL_INVALID_INPUT', 'Provide the current revision and claim ID.')
        try:
            key = UUID(request.headers.get('Idempotency-Key', ''))
        except (ValueError, AttributeError):
            fail(ValidationError, 'POOL_INVALID_INPUT', 'A UUID Idempotency-Key header is required.')
        history, replayed = reopen_pool(
            project_id=pk,
            package_id=package_id,
            principal=principal,
            expected_revision=data['expected_revision'],
            claim_id=data['claim_id'],
            key=key,
        )
        return Response({'claim_id': history.original_claim_id, 'replayed': replayed})


class PackageBalancedDispatchAPI(PackageAPIBase):
    def post(self, request, pk, package_id):
        principal, _, _, _, _ = self.context(request, pk)
        if (
            not settings.ANNOTATION_PACKAGE_CLAIM_ENABLED
            or not settings.ANNOTATION_PACKAGE_DISPATCH_ENABLED
            or not settings.ANNOTATION_PACKAGE_ADVANCED_ENABLED
        ):
            raise NotFound()
        if request.data:
            fail(ValidationError, 'POOL_INVALID_INPUT', 'No allocation fields are accepted.')
        try:
            key = UUID(request.headers.get('Idempotency-Key', ''))
        except (ValueError, AttributeError):
            fail(ValidationError, 'POOL_INVALID_INPUT', 'A UUID Idempotency-Key header is required.')
        receipt, replayed = balanced_dispatch(
            project_id=pk,
            package_id=package_id,
            principal=principal,
            key=key,
            request_hash=digest({'package_id': package_id, 'mode': 'balanced'}),
        )
        return Response(
            {
                'dispatch_id': receipt.pk,
                'assignee_id': receipt.assignee_id_snapshot,
                'assignment_ids': receipt.assignment_ids,
                'assignment_count': len(receipt.assignment_ids),
                'replayed': replayed,
            },
            status=200 if replayed else 201,
        )


class PackageOverdueNoticeAPI(PackageAPIBase):
    def post(self, request, pk):
        principal, _, _, _, _ = self.context(request, pk, filters=('after',))
        if not settings.ANNOTATION_PACKAGE_CLAIM_ENABLED:
            raise NotFound()
        if request.data:
            fail(ValidationError, 'POOL_INVALID_INPUT', 'No notification fields are accepted.')
        after = request.query_params.get('after', '0')
        if not after.isdigit() or len(after) > 18:
            fail(ValidationError, 'POOL_INVALID_INPUT', 'Invalid notification cursor.')
        postgres_required()
        with transaction.atomic():
            project, actor = lock_project_operation(pk, principal, READ_WORK)
            if authorization.project_role(principal, project) not in ('annotator', 'manager'):
                raise PermissionDenied()
            pools = list(
                TaskPackagePool.objects.filter(
                    Q(claim__actor=actor) | Q(claim__isnull=True, allocation__assignee=actor),
                    package__project=project,
                    due_at__lte=timezone.now(),
                    pk__gt=int(after),
                )
                .select_related('claim', 'allocation', 'package')
                .order_by('pk')[:51]
            )
            page, has_next = pools[:50], len(pools) > 50
            counts = overdue_counts(page, project)
            notices = []
            for pool in page:
                count = counts.get(pool.pk, 0)
                source = overdue_source(pool)
                if not count or not source or source[0] != actor.pk:
                    continue
                notice, _ = TaskPackageOverdueNotice.objects.get_or_create(
                    pool=pool,
                    **source[2],
                    due_at=pool.due_at,
                    defaults={'recipient': actor, 'overdue_count': count},
                )
                if notice.recipient_id != actor.pk:
                    raise PermissionDenied()
                notices.append(
                    {
                        'id': notice.pk,
                        'package_id': pool.package_id,
                        'name': pool.package.name,
                        'due_at': pool.due_at,
                        'overdue_count': count,
                    }
                )
        return Response({'results': notices, 'next_after': page[-1].pk if has_next else None})


class PackageDetailAPI(PackageAPIBase):
    def get(self, request, pk, package_id):
        _, actor, project, role, policy = self.context(request, pk, filters=('state',))
        state = requested_state(request, role)
        packages = visible_packages(project, actor, role)
        package = generics.get_object_or_404(packages, pk=package_id)
        projection = PackageWork(project, actor, role, [package.pk])
        live, assignments, pending = projection.live, projection.assignments, projection.pending
        counts = projection.summaries([package.pk])[package.pk]
        if role == 'reviewer':
            page = self.paginate_queryset(
                pending.order_by('-submitted_at', '-pk').values(
                    'id',
                    'assignment__task_id',
                    'revision',
                    'assignment__status',
                )
            )
            results = [
                {
                    'task_id': row['assignment__task_id'],
                    'submission_id': row['id'],
                    'revision': row['revision'],
                    'state': 'pending',
                    'source_cancelled': row['assignment__status'] == 'cancelled',
                }
                for row in page
            ]
        else:
            work = projection.work
            if role == 'manager':
                page = self.paginate_queryset(projection.manager_items(state).order_by('original_task_id'))
                ids = [item.original_task_id for item in page]
                available = set(live.filter(original_task_id__in=ids).values_list('original_task_id', flat=True))
                states = list(
                    work.filter(task_id__in=ids).values('task_id', 'work_state').annotate(n=Count('pk')).order_by()
                )
                results = [
                    {
                        'task_id': item.original_task_id,
                        'available': item.original_task_id in available,
                        'states': {
                            row['work_state']: row['n'] for row in states if row['task_id'] == item.original_task_id
                        },
                    }
                    for item in page
                ]
            else:
                if state:
                    work = work.filter(work_state=state)
                page = self.paginate_queryset(work.values('id', 'task_id', 'work_state', 'version'))
                results = [
                    {
                        'task_id': row['task_id'],
                        'assignment_id': row['id'],
                        'assignment_version': row['version'],
                        'state': row['work_state'],
                        'editor_url': f'/projects/{pk}/data?task={row["task_id"]}&task_package={package.pk}'
                        if row['work_state'] != 'conflict' and policy.allows(EDIT)
                        else None,
                    }
                    for row in page
                ]
        policy = current_read_policy(project, actor, role, package_ids=[package.pk])
        # Another eligible row can keep the package visible after a returned row
        # is revoked. Recheck this page's row identities using the same live SQL
        # scope, without freezing progress values or taking a project write lock.
        if role == 'annotator':
            expected = {(row['assignment_id'], row['assignment_version'], row['task_id']) for row in results}
            current = set(
                assignments.filter(pk__in=[row['assignment_id'] for row in results]).values_list(
                    'pk', 'version', 'task_id'
                )
            )
            if current != expected:
                fail(PermissionDenied, 'PACKAGE_ACCESS_CHANGED', 'Current assignment access has changed.')
        elif role == 'reviewer':
            expected = {(row['submission_id'], row['revision'], row['task_id']) for row in results}
            current = set(
                pending.filter(pk__in=[row['submission_id'] for row in results]).values_list(
                    'pk', 'revision', 'assignment__task_id'
                )
            )
            if current != expected:
                fail(PermissionDenied, 'PACKAGE_ACCESS_CHANGED', 'Current pending review access has changed.')
        if role == 'annotator' and not policy.allows(EDIT):
            for row in results:
                row['editor_url'] = None
        response = self.get_paginated_response(results)
        response.data.update(
            package=package_metadata(package, role),
            role=role,
            counts=counts,
            state=state,
            filter_states=filter_states(role),
            as_of=timezone.now(),
            can_review=role == 'reviewer' and policy.allows(REVIEW),
            can_manage_pool=role == 'manager'
            and settings.ANNOTATION_PACKAGE_CLAIM_ENABLED
            and policy.allows(BATCH_ASSIGN)
            and policy.allows(ASSIGN),
            can_dispatch=role == 'manager'
            and settings.ANNOTATION_PACKAGE_DISPATCH_ENABLED
            and policy.allows(BATCH_ASSIGN)
            and policy.allows(ASSIGN),
            continue_url=f'/projects/{pk}/data?labeling=1&task_package={package.pk}'
            if role == 'annotator' and policy.allows(EDIT) and not state
            else None,
            policy={'version': policy.version, 'state': policy.state, 'reason': policy.reason},
        )
        return response
