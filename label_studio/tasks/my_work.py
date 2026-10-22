"""Read-only personal work derived from existing assignment/draft/review facts.

No task payloads, annotation results or other assignees' answers leave this
projection. PostgreSQL JSON equality compares canonical object content while
preserving result-array order and region IDs.
"""

from access_control.authorization import authorization
from access_control.identity import resolve_actor
from access_control.project_operations import EDIT, READ_WORK, REVIEW, SUBMIT, require_project_operation
from core.permissions import all_permissions
from django.db.models import (
    BooleanField,
    Case,
    CharField,
    Count,
    F,
    Func,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from organizations.models import OrganizationMember
from projects.models import Project, ProjectMember
from rest_framework import generics
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from tasks.models import AnnotationDraft, Submission, TaskAssignment

WORK_STATES = ('rework', 'in_progress', 'assigned', 'submitted', 'approved', 'conflict')


def work_projects(actor, roles):
    """Repeat authority predicates in the actual list/count SQL, not just preflight."""
    projects = Project.objects.filter(
        organization__organizationmember__user_id=actor.pk,
        organization__organizationmember__deleted_at__isnull=True,
        organization__organizationmember__user__is_active=True,
        organization__organizationmember__user__active_organization_id=F('organization_id'),
    )
    membership = Q(members__user_id=actor.pk, members__enabled=True, members__role__in=roles)
    if ProjectMember.Role.MANAGER in roles:
        return projects.filter(Q(created_by_id=actor.pk) | membership).values('pk')
    # Creator has manager semantics even when an explicit reviewer row exists.
    return projects.exclude(created_by_id=actor.pk).filter(membership).values('pk')


class JSONContentEqual(Func):
    """Use each supported database's native JSON facilities, not text key order."""

    output_field = BooleanField()
    arity = 2

    def as_sql(self, compiler, connection, **extra_context):
        left, left_params = compiler.compile(self.source_expressions[0])
        right, right_params = compiler.compile(self.source_expressions[1])
        if connection.vendor == 'sqlite':
            # json_tree paths retain array indexes. Comparing paths/types/atoms
            # ignores object insertion order and preserves empty containers.
            columns = "fullkey, CASE WHEN type IN ('integer', 'real') THEN 'number' ELSE type END, atom"
            left_tree = f'SELECT {columns} FROM json_tree({left})'
            right_tree = f'SELECT {columns} FROM json_tree({right})'
            return (
                f'(NOT EXISTS ({left_tree} EXCEPT {right_tree}) AND NOT EXISTS ({right_tree} EXCEPT {left_tree}))',
                [*left_params, *right_params, *right_params, *left_params],
            )
        return f'({left} = {right})', [*left_params, *right_params]


def annotation_work(project, actor):
    latest = Submission.objects.filter(assignment_id=OuterRef('pk')).order_by('-revision')
    own_drafts = AnnotationDraft.objects.filter(assignment_id=OuterRef('pk'), user=actor)
    valid_drafts = own_drafts.filter(task_id=OuterRef('task_id')).filter(
        Q(annotation_id=OuterRef('annotation_id'))
        | Q(annotation_id__isnull=True, assignment__annotation_id__isnull=True)
    )

    def draft_count(query):
        return Coalesce(
            Subquery(query.order_by().values('assignment_id').annotate(n=Count('pk')).values('n')),
            Value(0),
            output_field=IntegerField(),
        )

    rows = TaskAssignment.objects.filter(
        project=project,
        project_id__in=work_projects(actor, ['annotator', 'manager']),
        assignee=actor,
        status__in=TaskAssignment.ACTIVE_STATUSES,
    ).annotate(
        latest_submission_id=Subquery(latest.values('pk')[:1]),
        latest_revision=Subquery(latest.values('revision')[:1]),
        latest_status=Subquery(latest.values('status')[:1]),
        latest_result=Subquery(latest.values('result_snapshot__annotation__result')[:1]),
        rejection_reason=Subquery(latest.values('review__reason')[:1]),
        draft_count=draft_count(own_drafts),
        valid_draft_count=draft_count(valid_drafts),
        draft_result=Subquery(valid_drafts.order_by('-pk').values('result')[:1]),
    )
    rows = rows.annotate(draft_matches_latest=JSONContentEqual(F('draft_result'), F('latest_result')))
    inconsistent = (
        ~Q(task__project_id=F('project_id'))
        | (
            Q(annotation_id__isnull=False)
            & (~Q(annotation__task_id=F('task_id')) | ~Q(annotation__project_id=F('project_id')))
        )
        | ~Q(draft_count=F('valid_draft_count'))
        | Q(valid_draft_count__gt=1)
        | (
            Q(latest_submission_id__isnull=False)
            & ~Q(
                latest_status__in=[
                    Submission.Status.PENDING,
                    Submission.Status.APPROVED,
                    Submission.Status.REJECTED,
                ]
            )
        )
    )
    return (
        rows.annotate(
            work_state=Case(
                When(inconsistent, then=Value('conflict')),
                When(latest_status=Submission.Status.REJECTED, then=Value('rework')),
                When(
                    Q(valid_draft_count=1) & (Q(latest_submission_id__isnull=True) | Q(draft_matches_latest=False)),
                    then=Value('in_progress'),
                ),
                When(latest_status=Submission.Status.PENDING, then=Value('submitted')),
                When(latest_status=Submission.Status.APPROVED, then=Value('approved')),
                When(status=TaskAssignment.Status.IN_PROGRESS, then=Value('in_progress')),
                default=Value('assigned'),
                output_field=CharField(),
            )
        )
        .annotate(
            work_order=Case(
                *[When(work_state=state, then=Value(index)) for index, state in enumerate(WORK_STATES)],
                output_field=IntegerField(),
            )
        )
        .order_by('work_order', 'pk')
    )


class MyWorkPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 100

    def paginate_queryset(self, queryset, request, view=None):
        try:
            return super().paginate_queryset(queryset, request, view)
        except NotFound as exc:
            # DRF translates the detail. Scope this code to paginator errors so
            # the client never mistakes revoked project access for an empty page.
            exc.display_context = {'reason': 'WORK_PAGE_OUT_OF_RANGE'}
            raise


class MyWorkAPI(generics.GenericAPIView):
    permission_required = all_permissions.tasks_view
    pagination_class = MyWorkPagination

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'private, no-store'
        return response

    @extend_schema(responses={200: OpenApiTypes.OBJECT}, summary='Read current user project work')
    def get(self, request, pk):
        if set(request.query_params) - {'kind', 'state', 'page', 'page_size'}:
            raise ValidationError({'detail': 'Only kind, state and pagination filters are supported.'})
        principal, actor = resolve_actor(request)
        project = generics.get_object_or_404(Project.objects.for_user(actor), pk=pk)
        if not OrganizationMember.objects.filter(
            organization_id=project.organization_id,
            user=actor,
            deleted_at__isnull=True,
        ).exists():
            raise PermissionDenied('Current organization membership is required.')
        policy = require_project_operation(project, READ_WORK)
        role = authorization.project_role(principal, project)
        if role not in (ProjectMember.Role.MANAGER, ProjectMember.Role.ANNOTATOR, ProjectMember.Role.REVIEWER):
            raise PermissionDenied('An enabled project role is required.')
        available_kinds = ['review'] if role == ProjectMember.Role.REVIEWER else ['annotation']
        kind = request.query_params.get('kind', available_kinds[0])
        if kind not in {'annotation', 'review'}:
            raise ValidationError({'kind': 'Expected annotation or review.'})
        if kind not in available_kinds:
            raise PermissionDenied('This project role cannot access this work queue.')
        as_of = timezone.now()
        if kind == 'annotation':
            queryset = annotation_work(project, actor)
            counts = queryset.aggregate(**{state: Count('pk', filter=Q(work_state=state)) for state in WORK_STATES})
            state = request.query_params.get('state')
            if state:
                if state not in WORK_STATES:
                    raise ValidationError({'state': 'Unsupported annotation work state.'})
                queryset = queryset.filter(work_state=state)
            page = self.paginate_queryset(
                queryset.values(
                    'id',
                    'task_id',
                    'version',
                    'annotation_id',
                    'work_state',
                    'latest_submission_id',
                    'latest_revision',
                    'latest_status',
                    'rejection_reason',
                    'valid_draft_count',
                )
            )
            results = []
            for item in page:
                state = item.pop('work_state')
                conflict = state == 'conflict'
                actions = [] if conflict or not policy.allows(EDIT) else ['open_editor']
                results.append(
                    {
                        'assignment_id': item.pop('id'),
                        'assignment_version': item.pop('version'),
                        'state': state,
                        'has_draft': bool(item.pop('valid_draft_count')),
                        **item,
                        'rejection_reason': item['rejection_reason'] if state == 'rework' else None,
                        'source': 'direct_assignment',
                        'allowed_actions': actions,
                        'can_submit': bool(actions) and policy.allows(SUBMIT),
                        'blocked_reason': 'WORK_SOURCE_CONFLICT'
                        if conflict
                        else (None if actions else policy.reason or 'PROJECT_OPERATION_BLOCKED'),
                        'editor_url': f'/projects/{project.pk}/data?task={item["task_id"]}' if actions else None,
                    }
                )
        else:
            if request.query_params.get('state'):
                raise ValidationError({'state': 'Review work contains pending submissions only.'})
            queryset = (
                Submission.objects.filter(
                    assignment__project=project,
                    assignment__project_id__in=work_projects(actor, ['reviewer']),
                    status=Submission.Status.PENDING,
                )
                .exclude(submitted_by=actor)
                .order_by('-submitted_at', '-pk')
            )
            counts = {'pending': queryset.count()}
            page = self.paginate_queryset(
                queryset.values(
                    'id',
                    'assignment_id',
                    'assignment__task_id',
                    'assignment__status',
                    'revision',
                    'submitted_at',
                )
            )
            results = [
                {
                    'submission_id': item['id'],
                    'assignment_id': item['assignment_id'],
                    'task_id': item['assignment__task_id'],
                    'revision': item['revision'],
                    'state': 'pending',
                    'submitted_at': item['submitted_at'],
                    'source_cancelled': item['assignment__status'] == TaskAssignment.Status.CANCELLED,
                    'allowed_actions': ['open_review_queue'] if policy.allows(REVIEW) else [],
                    'blocked_reason': None if policy.allows(REVIEW) else policy.reason or 'PROJECT_OPERATION_BLOCKED',
                }
                for item in page
            ]
        response = self.get_paginated_response(results)
        response.data.update(
            {
                'project_id': project.pk,
                'kind': kind,
                'available_kinds': available_kinds,
                'counts': counts,
                'as_of': as_of,
                'server_now': as_of,
                'policy': {'version': policy.version, 'state': policy.state, 'reason': policy.reason},
            }
        )
        return response
