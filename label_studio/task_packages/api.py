"""Fixed membership plus live, authorized read projections over existing work."""

import hashlib
import json
from uuid import UUID

from access_control.authorization import authorization
from access_control.identity import resolve_actor
from access_control.project_operations import (
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
from django.db.models import Count
from django.utils import timezone
from projects.models import Project, ProjectMember
from rest_framework import generics, serializers
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from tasks.models import Task
from tasks.my_work import work_projects

from .access import current_read_policy, fail, visible_packages
from .models import TaskPackage, TaskPackageItem
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
            role=role, can_create=role == 'manager' and policy.allows(ORGANIZE_WORK), as_of=timezone.now()
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
            continue_url=f'/projects/{pk}/data?labeling=1&task_package={package.pk}'
            if role == 'annotator' and policy.allows(EDIT) and not state
            else None,
            policy={'version': policy.version, 'state': policy.state, 'reason': policy.reason},
        )
        return response
