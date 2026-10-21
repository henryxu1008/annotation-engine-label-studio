"""This file and its contents are licensed under the Apache License 2.0. Please see the included NOTICE for copyright information and LICENSE for a copy of the license."""

import logging

from access_control.authorization import authorization
from access_control.identity import resolve_actor, resolve_principal
from core.feature_flags import flag_set
from core.mixins import GetParentObjectMixin
from core.permissions import ViewClassPermission, all_permissions
from core.utils.common import is_community
from core.utils.params import bool_from_request
from data_manager.api import TaskListAPI as DMTaskListAPI
from data_manager.functions import evaluate_predictions
from data_manager.models import PrepareParams
from data_manager.serializers import DataManagerTaskSerializer
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.decorators import method_decorator
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from projects.functions.stream_history import fill_history_annotation
from organizations.models import OrganizationMember
from projects.models import Project, ProjectMember
from rest_framework import generics, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from tasks.drafts import check_submission_draft, lock_drafts, require_draft_revision
from tasks.exceptions import AssignmentConflictError, DraftConflictError
from tasks.models import Annotation, AnnotationDraft, Prediction, Submission, Task, TaskAssignment
from tasks.openapi_schema import (
    annotation_request_schema,
    annotation_response_example,
    dm_task_response_example,
    prediction_request_schema,
    prediction_response_example,
    task_request_schema,
    task_response_example,
)
from tasks.serializers import (
    AnnotationDraftSerializer,
    AnnotationSerializer,
    PredictionSerializer,
    ReviewSubmissionSerializer,
    SubmissionSerializer,
    TaskAssignmentSerializer,
    TaskSerializer,
    TaskSimpleSerializer,
)
from tasks.submissions import create_submission, review_submission
from users.models import User
from users.serializers import UserSimpleSerializer
from webhooks.models import WebhookAction
from webhooks.utils import (
    api_webhook,
    api_webhook_for_delete,
    emit_webhooks_for_instance,
)

logger = logging.getLogger(__name__)


def require_assignment_token(request, assignment):
    assignment_id = request.data.get('assignment_id')
    assignment_version = request.data.get('assignment_version')

    if (
        assignment_id is None
        or assignment_version is None
        or str(assignment_id) != str(assignment.id)
        or str(assignment_version) != str(assignment.version)
    ):
        raise AssignmentConflictError()


def lock_active_assignment(request, principal, task, *, require_token=True):
    """Lock and revalidate the assignment that authorizes a labeling write."""
    role = authorization.project_role(principal, task.project)
    if role not in (ProjectMember.Role.ANNOTATOR, ProjectMember.Role.MANAGER):
        raise PermissionDenied('An enabled labeling role is required for this task.')

    assignment_id = request.data.get('assignment_id') if require_token else None
    if assignment_id is not None:
        try:
            assignment_id = int(assignment_id)
        except (TypeError, ValueError):
            raise AssignmentConflictError()

    queryset = TaskAssignment.objects.select_for_update().filter(
        task=task,
        assignee_id=principal.local_user_id,
        status__in=TaskAssignment.ACTIVE_STATUSES,
    )
    if assignment_id is not None:
        queryset = queryset.filter(pk=assignment_id)

    assignment = queryset.order_by('-assigned_at', '-id').first()
    if assignment is None:
        raise AssignmentConflictError()
    if require_token:
        require_assignment_token(request, assignment)
    return assignment


# TODO: fix after switch to api/tasks from api/dm/tasks
@method_decorator(
    name='post',
    decorator=extend_schema(
        tags=['Tasks'],
        summary='Create task',
        description='Create a new labeling task in Label Studio.',
        request={
            'application/json': task_request_schema,
        },
        responses={
            '201': OpenApiResponse(
                description='Created task',
                response=TaskSerializer,
                examples=[OpenApiExample(name='response', value=task_response_example, media_type='application/json')],
            )
        },
        extensions={
            'x-fern-sdk-group-name': 'tasks',
            'x-fern-sdk-method-name': 'create',
            'x-fern-audiences': ['public'],
        },
    )
    if is_community()
    else lambda f: f,
)
@method_decorator(
    name='get',
    decorator=extend_schema(
        tags=['Tasks'],
        summary='Get tasks list',
        description="""
    Retrieve a list of tasks with pagination for a specific view or project, by using filters and ordering.
    """,
        parameters=[
            OpenApiParameter(name='view', type=OpenApiTypes.INT, location='query', description='View ID'),
            OpenApiParameter(name='project', type=OpenApiTypes.INT, location='query', description='Project ID'),
            OpenApiParameter(
                name='resolve_uri',
                type=OpenApiTypes.BOOL,
                location='query',
                description='Resolve task data URIs using Cloud Storage',
            ),
            OpenApiParameter(
                name='fields',
                type=OpenApiTypes.STR,
                enum=['all', 'task_only'],
                default='task_only',
                location='query',
                description='Set to "all" if you want to include annotations and predictions in the response',
            ),
            OpenApiParameter(
                name='review',
                type=OpenApiTypes.BOOL,
                location='query',
                description='Get tasks for review',
            ),
            OpenApiParameter(
                name='include',
                type=OpenApiTypes.STR,
                location='query',
                description='Specify which fields to include in the response',
            ),
            OpenApiParameter(
                name='query',
                type=OpenApiTypes.STR,
                location='query',
                description='Additional query to filter tasks. It must be JSON encoded string of dict containing '
                'one of the following parameters: `{"filters": ..., "selectedItems": ..., "ordering": ...}`. Check '
                '[Data Manager > Create View > see `data` field](#tag/Data-Manager/operation/api_dm_views_create) '
                'for more details about filters, selectedItems and ordering.\n\n'
                '* **filters**: dict with `"conjunction"` string (`"or"` or `"and"`) and list of filters in `"items"` array. '
                'Each filter is a dictionary with keys: `"filter"`, `"operator"`, `"type"`, `"value"`. '
                '[Read more about available filters](https://labelstud.io/sdk/data_manager.html)<br/>'
                '                   Example: `{"conjunction": "or", "items": [{"filter": "filter:tasks:completed_at", "operator": "greater", "type": "Datetime", "value": "2021-01-01T00:00:00.000Z"}]}`\n'
                '* **selectedItems**: dictionary with keys: `"all"`, `"included"`, `"excluded"`. If "all" is `false`, `"included"` must be used. If "all" is `true`, `"excluded"` must be used.<br/>'
                '                   Examples: `{"all": false, "included": [1, 2, 3]}` or `{"all": true, "excluded": [4, 5]}`\n'
                '* **ordering**: list of fields to order by. Currently, ordering is supported by only one parameter. <br/>\n'
                '                   Example: `["completed_at"]`',
            ),
        ],
        responses={
            '200': OpenApiResponse(
                description='Tasks list',
                response={
                    'type': 'object',
                    'properties': {
                        'tasks': {
                            'description': 'List of tasks',
                            'type': 'array',
                            'items': {
                                'description': 'Task object',
                                'type': 'object',
                            },
                        },
                        'total': {
                            'description': 'Total number of tasks',
                            'type': 'integer',
                        },
                        'total_annotations': {
                            'description': 'Total number of annotations',
                            'type': 'integer',
                        },
                        'total_predictions': {
                            'description': 'Total number of predictions',
                            'type': 'integer',
                        },
                    },
                },
            )
        },
        extensions={
            'x-fern-sdk-group-name': 'tasks',
            'x-fern-sdk-method-name': 'list',
            'x-fern-pagination': {
                'offset': '$request.page',
                'results': '$response.tasks',
            },
            'x-fern-audiences': ['public'],
        },
    )
    if is_community()
    else lambda f: f,
)
class TaskListAPI(DMTaskListAPI):
    serializer_class = TaskSerializer
    permission_required = ViewClassPermission(
        GET=all_permissions.tasks_view,
        POST=all_permissions.tasks_create,
    )
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['project']

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        allowed_task_ids = Task.objects.for_user(self.request.user).values_list('id', flat=True)
        return queryset.filter(id__in=allowed_task_ids)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        project_id = self.request.data.get('project')
        if project_id:
            project = generics.get_object_or_404(Project.objects.for_user(self.request.user), pk=project_id)
            principal = resolve_principal(self.request)
            authorization.require(
                authorization.can_manage_project(principal, project),
                'Project manager role is required to create tasks.',
            )
            context['project'] = project
        return context

    def perform_create(self, serializer):
        project_id = self.request.data.get('project')
        project = generics.get_object_or_404(Project.objects.for_user(self.request.user), pk=project_id)
        principal = resolve_principal(self.request)
        authorization.require(
            authorization.can_manage_project(principal, project),
            'Project manager role is required to create tasks.',
        )
        instance = serializer.save(project=project)
        emit_webhooks_for_instance(
            self.request.user.active_organization, project, WebhookAction.TASKS_CREATED, [instance]
        )


@method_decorator(
    name='get',
    decorator=extend_schema(
        tags=['Tasks'],
        summary='Get task',
        description="""
        Get task data, metadata, annotations and other attributes for a specific labeling task by task ID.
        """,
        parameters=[
            OpenApiParameter(name='id', type=OpenApiTypes.STR, location='path', description='Task ID'),
        ],
        request=None,
        responses={
            '200': OpenApiResponse(
                description='Task',
                response=DataManagerTaskSerializer,
                examples=[
                    OpenApiExample(name='response', value=dm_task_response_example, media_type='application/json')
                ],
            )
        },
        extensions={
            'x-fern-sdk-group-name': 'tasks',
            'x-fern-sdk-method-name': 'get',
            'x-fern-audiences': ['public'],
        },
    ),
)
@method_decorator(
    name='patch',
    decorator=extend_schema(
        tags=['Tasks'],
        summary='Update task',
        description='Update the attributes of an existing labeling task.',
        parameters=[
            OpenApiParameter(name='id', type=OpenApiTypes.STR, location='path', description='Task ID'),
        ],
        request={
            'application/json': task_request_schema,
        },
        responses={
            '200': OpenApiResponse(
                description='Updated task',
                response=TaskSerializer,
                examples=[OpenApiExample(name='response', value=task_response_example, media_type='application/json')],
            )
        },
        extensions={
            'x-fern-sdk-group-name': 'tasks',
            'x-fern-sdk-method-name': 'update',
            'x-fern-audiences': ['public'],
        },
    ),
)
@method_decorator(
    name='delete',
    decorator=extend_schema(
        tags=['Tasks'],
        summary='Delete task',
        description='Delete a task in Label Studio. This action cannot be undone!',
        parameters=[
            OpenApiParameter(name='id', type=OpenApiTypes.STR, location='path', description='Task ID'),
        ],
        request=None,
        extensions={
            'x-fern-sdk-group-name': 'tasks',
            'x-fern-sdk-method-name': 'delete',
            'x-fern-audiences': ['public'],
        },
    ),
)
class TaskAPI(generics.RetrieveUpdateDestroyAPIView):
    parser_classes = (JSONParser, FormParser, MultiPartParser)
    permission_required = ViewClassPermission(
        GET=all_permissions.tasks_view,
        PUT=all_permissions.tasks_change,
        PATCH=all_permissions.tasks_change,
        DELETE=all_permissions.tasks_delete,
    )

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.task = self.get_object()

    def prefetch(self, queryset):
        return queryset.prefetch_related(
            'annotations',
            'predictions',
            'annotations__completed_by',
            'project',
            'io_storages_azureblobimportstoragelink',
            'io_storages_gcsimportstoragelink',
            'io_storages_localfilesimportstoragelink',
            'io_storages_redisimportstoragelink',
            'io_storages_s3importstoragelink',
            'file_upload',
            'project__ml_backends',
        )

    def get_retrieve_serializer_context(self, request):
        """Build serializer context for task retrieval.

        The resolve_uri parameter controls whether storage URLs (e.g., s3://bucket/file.jpg)
        are converted to proxy URLs (/tasks/<id>/resolve/?fileuri=...). This is useful for:
        - resolve_uri=True (default): URLs are proxied through Label Studio for security
        - resolve_uri=False: Original storage URLs are preserved, useful for debugging
          or when users need to see the actual source paths in task preview
        """
        fields = ['drafts', 'predictions', 'annotations']

        # Lazy load annotations behind feature flag (FIT-720)
        annotations_stub = False
        if flag_set('fflag_fix_all_fit_720_lazy_load_annotations', user=request.user):
            annotations_stub = bool_from_request(request.GET, 'annotations_stub', False)

        return {
            'resolve_uri': bool_from_request(request.GET, 'resolve_uri', True),
            'predictions': 'predictions' in fields,
            'annotations': 'annotations' in fields,
            'drafts': 'drafts' in fields,
            'annotations_stub': annotations_stub,
            'request': request,
        }

    def get(self, request, pk):
        context = self.get_retrieve_serializer_context(request)
        context['project'] = project = self.task.project

        # get prediction
        if (
            project.evaluate_predictions_automatically or project.show_collab_predictions
        ) and not self.task.predictions.exists():
            evaluate_predictions([self.task])
            # refresh task from db with prefetches
            self.task = self.get_object()

        # Expansion bypasses DataManagerTaskSerializer.get_annotations(). Only managers
        # may use the unfiltered expandable relation. Assignees must go through
        # get_annotations(), which scopes results to their assignment.
        principal = resolve_principal(request)
        is_manager = authorization.can_manage_project(principal, project)
        expand = (
            ['annotations.completed_by']
            if is_manager and not context.get('annotations_stub')
            else []
        )
        serializer = self.get_serializer_class()(self.task, many=False, context=context, expand=expand)
        data = serializer.data
        return Response(data)

    def get_excluded_fields_for_evaluation(self):
        return ['annotations_results', 'predictions_results']

    def get_queryset(self):
        task_id = self.request.parser_context['kwargs'].get('pk')
        task = generics.get_object_or_404(Task.objects.for_user(self.request.user), pk=task_id)
        review = bool_from_request(self.request.GET, 'review', False)
        selected = {'all': False, 'included': [self.kwargs.get('pk')]}
        if review:
            kwargs = {'fields_for_evaluation': ['annotators', 'reviewed']}
        else:
            kwargs = {
                'all_fields': True,
                'excluded_fields_for_evaluation': self.get_excluded_fields_for_evaluation(),
            }
        project = self.request.query_params.get('project') or self.request.data.get('project')
        if not project:
            project = task.project.id
        return self.prefetch(
            Task.prepared.get_queryset(
                prepare_params=PrepareParams(project=project, selectedItems=selected, request=self.request), **kwargs
            )
        )

    def get_object(self):
        """
        Override to check permissions on a lightweight task first.

        This avoids executing the expensive PreparedTaskManager query
        when the user doesn't have permission to access the task.
        """
        task_id = self.kwargs.get('pk')

        # First check permissions using a lightweight query
        # select_related('project') avoids extra query when permission check accesses task.project
        lean_task = generics.get_object_or_404(
            Task.objects.for_user(self.request.user).select_related('project'),
            pk=task_id,
        )
        self.check_object_permissions(self.request, lean_task)

        # Now fetch full task with heavy queryset (prefetches, annotations, etc.)
        queryset = self.filter_queryset(self.get_queryset())
        return generics.get_object_or_404(queryset, pk=task_id)

    def get_serializer_class(self):
        # GET => task + annotations + predictions + drafts
        if self.request.method == 'GET':
            return DataManagerTaskSerializer

        # POST, PATCH, PUT
        else:
            return TaskSimpleSerializer

    def _require_manager(self, request):
        principal = resolve_principal(request)
        authorization.require(
            authorization.can_manage_task(principal, self.task),
            'Project manager role is required to modify tasks.',
        )

    def patch(self, request, *args, **kwargs):
        self._require_manager(request)
        return super(TaskAPI, self).patch(request, *args, **kwargs)

    @api_webhook_for_delete(WebhookAction.TASKS_DELETED)
    def delete(self, request, *args, **kwargs):
        self._require_manager(request)
        return super(TaskAPI, self).delete(request, *args, **kwargs)

    @extend_schema(exclude=True)
    def put(self, request, *args, **kwargs):
        self._require_manager(request)
        return super(TaskAPI, self).put(request, *args, **kwargs)


@method_decorator(
    name='get',
    decorator=extend_schema(
        tags=['Tasks'],
        summary='Get task label distribution',
        description='Get aggregated label distribution across all annotations for a task. '
        'Returns counts of each label value grouped by control tag. '
        'This is an efficient endpoint that avoids N+1 queries.',
        responses={
            '200': OpenApiResponse(
                description='Label distribution data',
                examples=[
                    OpenApiExample(
                        name='response',
                        value={
                            'total_annotations': 100,
                            'distributions': {
                                'label': {
                                    'type': 'rectanglelabels',
                                    'labels': {'Car': 45, 'Person': 30, 'Dog': 25},
                                },
                            },
                        },
                        media_type='application/json',
                    )
                ],
            )
        },
        extensions={
            'x-fern-audiences': ['internal'],
        },
    ),
)
class TaskAgreementAPI(generics.RetrieveAPIView):
    """
    Efficient endpoint for getting label distribution without fetching all annotations.

    This endpoint aggregates annotation results at the database level to avoid N+1 queries.
    It returns pre-computed label counts for the Distribution row in the Summary view.
    """

    permission_required = ViewClassPermission(GET=all_permissions.tasks_view)
    queryset = Task.objects.all()

    def get(self, request, pk):
        # This endpoint is gated by feature flag
        if not flag_set('fflag_fix_all_fit_720_lazy_load_annotations', user=request.user):
            raise PermissionDenied('Feature not enabled')

        try:
            task = Task.objects.for_user(request.user).get(pk=pk)
        except Task.DoesNotExist:
            return Response({'error': 'Task not found'}, status=404)

        principal = resolve_principal(request)
        authorization.require(
            authorization.can_manage_project(principal, task.project),
            'Project manager role is required to view annotation agreement.',
        )

        # Get all annotations for this task with their results in a single query
        annotations = Annotation.objects.filter(
            task=task,
            was_cancelled=False,
        ).values_list('result', flat=True)

        total_annotations = len(annotations)
        distributions = {}

        def merge_result_into_distributions(result):
            """Merge a single result (list of labeling items) into distributions in place."""
            if not result or not isinstance(result, list):
                return
            for item in result:
                if not isinstance(item, dict):
                    continue
                from_name = item.get('from_name', '')
                result_type = item.get('type', '')
                value = item.get('value', {})

                if from_name not in distributions:
                    distributions[from_name] = {
                        'type': result_type,
                        'labels': {},
                        'values': [],
                    }

                if result_type.endswith('labels'):
                    labels = value.get(result_type, [])
                    if isinstance(labels, list):
                        for label in labels:
                            if label not in distributions[from_name]['labels']:
                                distributions[from_name]['labels'][label] = 0
                            distributions[from_name]['labels'][label] += 1

                elif result_type == 'choices':
                    choices = value.get('choices', [])
                    if isinstance(choices, list):
                        for choice in choices:
                            if choice not in distributions[from_name]['labels']:
                                distributions[from_name]['labels'][choice] = 0
                            distributions[from_name]['labels'][choice] += 1

                elif result_type == 'rating':
                    rating = value.get('rating')
                    if rating is not None:
                        distributions[from_name]['values'].append(rating)

                elif result_type == 'number':
                    number = value.get('number')
                    if number is not None:
                        distributions[from_name]['values'].append(number)

                elif result_type == 'taxonomy':
                    taxonomy = value.get('taxonomy', [])
                    if isinstance(taxonomy, list):
                        for path in taxonomy:
                            if isinstance(path, list) and path:
                                leaf = path[-1]
                                if leaf not in distributions[from_name]['labels']:
                                    distributions[from_name]['labels'][leaf] = 0
                                distributions[from_name]['labels'][leaf] += 1

                elif result_type == 'pairwise':
                    selected = value.get('selected')
                    if selected:
                        if selected not in distributions[from_name]['labels']:
                            distributions[from_name]['labels'][selected] = 0
                        distributions[from_name]['labels'][selected] += 1

        # Process annotation results
        for result in annotations:
            merge_result_into_distributions(result)

        # Include prediction results in distribution counts so aggregate matches
        # client-side (develop / FF off). total_annotations stays annotation count only.
        predictions = Prediction.objects.filter(task=task).values_list('result', flat=True)
        for result in predictions:
            # Prediction.result can be list (same as annotation) or dict
            if isinstance(result, list):
                merge_result_into_distributions(result)

        # Post-process: calculate averages for numeric types
        for from_name, dist in distributions.items():
            if dist['values']:
                dist['average'] = sum(dist['values']) / len(dist['values'])
                dist['count'] = len(dist['values'])
            # Remove raw values from response to keep it lightweight
            del dist['values']

        return Response(
            {
                'total_annotations': total_annotations,
                'distributions': distributions,
            }
        )


@method_decorator(
    name='get',
    decorator=extend_schema(
        tags=['Annotations'],
        summary='Get annotation by its ID',
        description='Retrieve a specific annotation for a task using the annotation result ID.',
        request=None,
        responses={
            '200': OpenApiResponse(
                description='Retrieved annotation',
                response=AnnotationSerializer,
                examples=[
                    OpenApiExample(name='response', value=annotation_response_example, media_type='application/json')
                ],
            )
        },
        extensions={
            'x-fern-sdk-group-name': 'annotations',
            'x-fern-sdk-method-name': 'get',
            'x-fern-audiences': ['public'],
        },
    ),
)
@method_decorator(
    name='patch',
    decorator=extend_schema(
        tags=['Annotations'],
        summary='Update annotation',
        description='Update existing attributes on an annotation.',
        request={
            'application/json': annotation_request_schema,
        },
        responses={
            '200': OpenApiResponse(
                description='Updated annotation',
                response=AnnotationSerializer,
                examples=[
                    OpenApiExample(name='response', value=annotation_response_example, media_type='application/json')
                ],
            )
        },
        extensions={
            'x-fern-sdk-group-name': 'annotations',
            'x-fern-sdk-method-name': 'update',
            'x-fern-audiences': ['public'],
        },
    ),
)
@method_decorator(
    name='delete',
    decorator=extend_schema(
        tags=['Annotations'],
        summary='Delete annotation',
        description="Delete an annotation. This action can't be undone!",
        request=None,
        extensions={
            'x-fern-sdk-group-name': 'annotations',
            'x-fern-sdk-method-name': 'delete',
            'x-fern-audiences': ['public'],
        },
    ),
)
class AnnotationAPI(generics.RetrieveUpdateDestroyAPIView):
    parser_classes = (JSONParser, FormParser, MultiPartParser)
    permission_required = ViewClassPermission(
        GET=all_permissions.annotations_view,
        PUT=all_permissions.annotations_change,
        PATCH=all_permissions.annotations_change,
        DELETE=all_permissions.annotations_delete,
    )

    serializer_class = AnnotationSerializer
    queryset = Annotation.objects.all()

    @transaction.atomic
    def perform_destroy(self, annotation):
        principal, actor = resolve_actor(self.request)
        assignment = lock_active_assignment(self.request, principal, annotation.task, require_token=False)
        if assignment.annotation_id != annotation.id:
            raise PermissionDenied('Only the active assignment owner can delete this annotation.')
        check_submission_draft(self.request, assignment, actor, annotation)
        task_id = annotation.task_id
        annotation.delete()
        if task_id is not None:
            Task.objects.filter(id=task_id).update(updated_by=actor)

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        # Resolve the trusted server-side actor before touching audit fields.
        principal, actor = resolve_actor(request)
        user = request.user

        # save user history with annotator_id, time & annotation result
        annotation = self.get_object()
        authorization.require(
            authorization.can_update_annotation(principal, annotation),
            'Only the active assignment owner can modify this annotation.',
        )
        assignment = lock_active_assignment(request, principal, annotation.task)
        if assignment.annotation_id != annotation.id:
            raise AssignmentConflictError()
        check_submission_draft(request, assignment, actor, annotation)
        # use updated instead of save to avoid duplicated signals
        Annotation.objects.filter(id=annotation.id).update(updated_by=actor)

        task = annotation.task
        if self.request.data.get('ground_truth'):
            task.ensure_unique_groundtruth(annotation_id=annotation.id)
        task.update_is_labeled()
        task.save()  # refresh task metrics

        partial = kwargs.pop('partial', False)
        serializer = self.get_serializer(annotation, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        submit_for_review = serializer.validated_data.get('submit_for_review', False)
        self.perform_update(serializer)
        result = Response(serializer.data)
        # Annotation.save() preserves upstream CurrentContext behavior and may
        # rewrite updated_by from request.user. Reassert the provider-mapped
        # audit actor after serializer persistence without changing global model semantics.
        Annotation.objects.filter(id=annotation.id).update(updated_by=actor)
        Task.objects.filter(id=task.id).update(updated_by=actor)
        task.updated_by = actor
        annotation.refresh_from_db()

        if submit_for_review:
            create_submission(assignment=assignment, annotation=annotation, actor=actor)

        task.update_is_labeled()
        task.save(update_fields=['updated_at'])  # refresh task metrics

        # super().update() serialized before the mapped actor was reasserted.
        # Re-serialize the refreshed instance so the write response matches
        # the authoritative persisted audit identity.
        result.data = self.get_serializer(annotation).data
        return result

    def get(self, request, *args, **kwargs):
        return super(AnnotationAPI, self).get(request, *args, **kwargs)

    @api_webhook(WebhookAction.ANNOTATION_UPDATED)
    @extend_schema(exclude=True)
    def put(self, request, *args, **kwargs):
        return super(AnnotationAPI, self).put(request, *args, **kwargs)

    @api_webhook(WebhookAction.ANNOTATION_UPDATED)
    def patch(self, request, *args, **kwargs):
        return super(AnnotationAPI, self).patch(request, *args, **kwargs)

    @api_webhook_for_delete(WebhookAction.ANNOTATIONS_DELETED)
    def delete(self, request, *args, **kwargs):
        return super(AnnotationAPI, self).delete(request, *args, **kwargs)


@method_decorator(
    name='get',
    decorator=extend_schema(
        tags=['Annotations'],
        summary='Get all task annotations',
        description='List all annotations for a task.',
        parameters=[
            OpenApiParameter(name='id', type=OpenApiTypes.INT, location='path', description='Task ID'),
        ],
        request=None,
        responses={
            '200': OpenApiResponse(
                description='Annotation',
                response=AnnotationSerializer(many=True),
                examples=[
                    OpenApiExample(name='response', value=[annotation_response_example], media_type='application/json')
                ],
            )
        },
        extensions={
            'x-fern-sdk-group-name': 'annotations',
            'x-fern-sdk-method-name': 'list',
            'x-fern-audiences': ['public'],
        },
    ),
)
@method_decorator(
    name='post',
    decorator=extend_schema(
        tags=['Annotations'],
        summary='Create annotation',
        description="""
        Add annotations to a task like an annotator does. The content of the result field depends on your
        labeling configuration. For example, send the following data as part of your POST
        request to send an empty annotation with the ID of the user who completed the task:

        ```json
        {
        "result": {},
        "was_cancelled": true,
        "ground_truth": true,
        "lead_time": 0,
        "task": 0
        "completed_by": 123
        }
        ```
        """,
        parameters=[
            OpenApiParameter(name='id', type=OpenApiTypes.INT, location='path', description='Task ID'),
        ],
        request={
            'application/json': annotation_request_schema,
        },
        responses={
            '201': OpenApiResponse(
                description='Created annotation',
                response=AnnotationSerializer,
                examples=[
                    OpenApiExample(name='response', value=annotation_response_example, media_type='application/json')
                ],
            )
        },
        extensions={
            'x-fern-sdk-group-name': 'annotations',
            'x-fern-sdk-method-name': 'create',
            'x-fern-audiences': ['public'],
        },
    ),
)
class AnnotationsListAPI(GetParentObjectMixin, generics.ListCreateAPIView):
    parser_classes = (JSONParser, FormParser, MultiPartParser)
    permission_required = ViewClassPermission(
        GET=all_permissions.annotations_view,
        POST=all_permissions.annotations_create,
    )
    parent_queryset = Task.objects.all()

    serializer_class = AnnotationSerializer

    def get(self, request, *args, **kwargs):
        return super(AnnotationsListAPI, self).get(request, *args, **kwargs)

    @api_webhook(WebhookAction.ANNOTATION_CREATED)
    def post(self, request, *args, **kwargs):
        return super(AnnotationsListAPI, self).post(request, *args, **kwargs)

    def get_queryset(self):
        task = generics.get_object_or_404(Task.objects.for_user(self.request.user), pk=self.kwargs.get('pk', 0))
        annotations = Annotation.objects.filter(Q(task=task) & Q(was_cancelled=False))
        principal = resolve_principal(self.request)
        if not authorization.can_manage_project(principal, task.project):
            annotations = annotations.filter(
                task_assignment__assignee=self.request.user,
                task_assignment__status__in=TaskAssignment.ACTIVE_STATUSES,
            )
        return annotations.order_by('pk')

    def delete_draft(self, draft_id, annotation_id):
        try:
            draft = AnnotationDraft.objects.get(id=draft_id)
            # We call delete on the individual draft object because
            # AnnotationDraft#delete has special behavior (updating created_labels_drafts).
            # This special behavior won't be triggered if we call delete on the queryset.
            # Only for drafts with empty annotation_id, other ones deleted by signal
            draft.delete()
        except AnnotationDraft.DoesNotExist:
            pass

    @transaction.atomic
    def perform_create(self, ser):
        task = self.parent_object
        principal, actor = resolve_actor(self.request)
        authorization.require(
            authorization.can_label_task(principal, task),
            'An active task assignment is required to create an annotation.',
        )
        assignment = authorization.active_task_assignment(principal, task)
        assignment = TaskAssignment.objects.select_for_update().get(pk=assignment.pk)
        require_assignment_token(self.request, assignment)
        if assignment.annotation_id is not None:
            raise ValidationError({'detail': 'This assignment already owns an annotation; update it instead.'})

        # Session-bound concerns such as activity tracking, task locks, and
        # label-stream history remain on request.user. Draft/annotation ownership
        # checks use the trusted mapped actor explicitly below.
        user = self.request.user

        # Check if task is being skipped and if it's allowed
        was_cancelled_get = bool_from_request(self.request.GET, 'was_cancelled', False)
        was_cancelled_data = self.request.data.get('was_cancelled', False)
        is_skipping = was_cancelled_get or was_cancelled_data

        if is_skipping and not task.can_be_skipped():
            raise ValidationError({'detail': 'This task cannot be skipped.'})

        # updates history
        result = ser.validated_data.get('result')
        # Interactive annotation authorship always comes from the authenticated
        # server-side actor. Client-supplied completed_by / updated_by cannot
        # override these values.
        extra_args = {
            'task_id': self.kwargs['pk'],
            'project_id': task.project_id,
            'completed_by': actor,
            'updated_by': actor,
        }

        # save stats about how well annotator annotations coincide with current prediction
        # only for finished task annotations
        if result is not None:
            prediction = Prediction.objects.filter(task=task, model_version=task.project.model_version)
            if prediction.exists():
                prediction = prediction.first()
                prediction_ser = PredictionSerializer(prediction).data
            else:
                logger.debug(f'User={self.request.user}: there are no predictions for task={task}')
                prediction_ser = {}
            # serialize annotation
            extra_args.update({'prediction': prediction_ser, 'updated_by': actor})

        if 'was_cancelled' in self.request.GET:
            extra_args['was_cancelled'] = bool_from_request(self.request.GET, 'was_cancelled', False)

        draft_id = self.request.data.get('draft_id')
        draft = AnnotationDraft.objects.filter(id=draft_id).first()
        if draft:
            # draft permission check
            if draft.task_id != task.id or not draft.has_permission(actor) or draft.user_id != actor.id:
                raise PermissionDenied(f'You have no permission to draft id:{draft_id}')

        draft = check_submission_draft(self.request, assignment, actor)

        if draft is not None:
            # if the annotation will be created from draft - get created_at from draft to keep continuity of history
            extra_args['draft_created_at'] = draft.created_at

        # create annotation
        logger.debug(f'User={self.request.user}: save annotation')
        submit_for_review = bool(ser.validated_data.get('submit_for_review', False))
        annotation = ser.save(**extra_args)
        # Annotation.save() may apply CurrentContext.request.user after DRF's
        # explicit save kwargs; make the trusted provider mapping authoritative
        # for the persisted annotation audit actor.
        Annotation.objects.filter(id=annotation.id).update(updated_by=actor)
        Task.objects.filter(id=task.id).update(updated_by=actor)
        annotation.updated_by = actor
        task.updated_by = actor
        assignment.annotation = annotation
        assignment.status = TaskAssignment.Status.IN_PROGRESS
        assignment.save(update_fields=['annotation', 'status', 'updated_at'])

        if submit_for_review:
            create_submission(assignment=assignment, annotation=annotation, actor=actor)

        logger.debug(f'Save activity for user={self.request.user}')
        self.request.user.activity_at = timezone.now()
        self.request.user.save()

        # Release task if it has been taken at work (it should be taken by the same user, or it makes sentry error
        logger.debug(f'User={user} releases task={task}')
        task.release_lock(user)

        # if annotation created from draft - remove this draft
        if draft_id is not None:
            logger.debug(f'Remove draft {draft_id} after creating annotation {annotation.id}')
            self.delete_draft(draft_id, annotation.id)

        if self.request.data.get('ground_truth'):
            annotation.task.ensure_unique_groundtruth(annotation_id=annotation.id)

        fill_history_annotation(user, task, annotation)

        return annotation


@extend_schema(exclude=True)
class AnnotationDraftListAPI(generics.ListCreateAPIView):
    parser_classes = (JSONParser, MultiPartParser, FormParser)
    serializer_class = AnnotationDraftSerializer
    permission_required = ViewClassPermission(
        GET=all_permissions.annotations_view,
        POST=all_permissions.annotations_create,
    )
    queryset = AnnotationDraft.objects.all()

    def filter_queryset(self, queryset):
        task_id = self.kwargs['pk']
        principal, actor = resolve_actor(self.request)
        task = generics.get_object_or_404(Task.objects.for_user(actor), pk=task_id)
        assignment = authorization.active_task_assignment(principal, task)
        if assignment is None:
            return queryset.none()
        return queryset.filter(task_id=task_id, user=actor, assignment=assignment)

    @transaction.atomic
    def perform_create(self, serializer):
        task_id = self.kwargs['pk']
        principal, actor = resolve_actor(self.request)
        task = generics.get_object_or_404(Task.objects.for_user(actor), pk=task_id)
        assignment = lock_active_assignment(self.request, principal, task)
        annotation_id = self.kwargs.get('annotation_id')
        user = actor
        if annotation_id is not None:
            annotation = generics.get_object_or_404(Annotation, pk=annotation_id, task=task)
            authorization.require(
                authorization.can_update_annotation(principal, annotation),
                'Only the active assignment owner can draft changes for this annotation.',
            )
        if lock_drafts(task).filter(user=actor, assignment=assignment, annotation_id=annotation_id).exists():
            raise DraftConflictError()
        logger.debug(f'User {user} is going to create draft for task={task_id}, annotation={annotation_id}')
        serializer.save(
            task_id=self.kwargs['pk'],
            annotation_id=annotation_id,
            user=actor,
            assignment=assignment,
        )


@extend_schema(exclude=True)
class AnnotationDraftAPI(generics.RetrieveUpdateDestroyAPIView):
    parser_classes = (JSONParser, MultiPartParser, FormParser)
    serializer_class = AnnotationDraftSerializer
    queryset = AnnotationDraft.objects.all()
    permission_required = ViewClassPermission(
        GET=all_permissions.annotations_view,
        PUT=all_permissions.annotations_change,
        PATCH=all_permissions.annotations_change,
        DELETE=all_permissions.annotations_delete,
    )

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        draft = self.get_object()
        principal = resolve_principal(request)
        assignment = lock_active_assignment(request, principal, draft.task)
        if draft.assignment_id != assignment.id:
            raise AssignmentConflictError()
        draft = generics.get_object_or_404(lock_drafts(draft.task), pk=draft.pk)
        require_draft_revision(request, draft)
        serializer = self.get_serializer(draft, data=request.data, partial=kwargs.get('partial', False))
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @transaction.atomic
    def perform_destroy(self, instance):
        principal = resolve_principal(self.request)
        assignment = lock_active_assignment(self.request, principal, instance.task)
        if instance.assignment_id != assignment.id:
            raise AssignmentConflictError()
        draft = generics.get_object_or_404(lock_drafts(instance.task), pk=instance.pk)
        require_draft_revision(self.request, draft)
        draft.delete()


class SubmissionPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 100


class SubmissionListAPI(generics.ListAPIView):
    serializer_class = SubmissionSerializer
    permission_required = all_permissions.annotations_view
    pagination_class = SubmissionPagination

    def paginate_queryset(self, queryset):
        if not (
            bool_from_request(self.request.GET, 'reviewable', False)
            or bool_from_request(self.request.GET, 'history', False)
            or self.request.query_params.get('page')
            or self.request.query_params.get('page_size')
        ):
            return None
        return super().paginate_queryset(queryset)

    def get_queryset(self):
        user = self.request.user
        queryset = Submission.objects.filter(assignment__project__organization=user.active_organization)
        queryset = queryset.filter(
            Q(assignment__assignee=user)
            | Q(assignment__project__created_by=user)
            | Q(
                assignment__project__members__user=user,
                assignment__project__members__enabled=True,
                assignment__project__members__role__in=['manager', 'reviewer'],
            )
        ).distinct()

        project_id = self.request.query_params.get('project')
        status_value = self.request.query_params.get('status')
        reviewable = bool_from_request(self.request.GET, 'reviewable', False)
        history = bool_from_request(self.request.GET, 'history', False)

        if reviewable:
            if not project_id:
                raise ValidationError({'project': 'Project is required for the review queue.'})
            project = generics.get_object_or_404(Project.objects.for_user(user), pk=project_id)
            principal = resolve_principal(self.request)
            authorization.require(
                authorization.can_review_project(principal, project),
                'Project reviewer role is required to access the review queue.',
            )
            queryset = queryset.filter(
                assignment__project=project,
                status=Submission.Status.PENDING,
            ).exclude(submitted_by=user)
        else:
            if project_id:
                queryset = queryset.filter(assignment__project_id=project_id)
            if history:
                queryset = queryset.exclude(status=Submission.Status.PENDING)
            elif status_value:
                queryset = queryset.filter(status=status_value)

        return queryset.select_related(
            'assignment',
            'assignment__project',
            'assignment__assignee',
            'annotation',
            'submitted_by',
            'review',
            'review__reviewer',
        ).order_by('-submitted_at', '-id')


class SubmissionAPI(generics.RetrieveAPIView):
    serializer_class = SubmissionSerializer
    permission_required = all_permissions.annotations_view

    def get_queryset(self):
        user = self.request.user
        return (
            Submission.objects.filter(assignment__project__organization=user.active_organization)
            .filter(
                Q(assignment__assignee=user)
                | Q(assignment__project__created_by=user)
                | Q(
                    assignment__project__members__user=user,
                    assignment__project__members__enabled=True,
                    assignment__project__members__role__in=['manager', 'reviewer'],
                )
            )
            .distinct()
        )


class SubmissionReviewAPI(generics.GenericAPIView):
    serializer_class = ReviewSubmissionSerializer
    permission_required = all_permissions.annotations_view
    queryset = Submission.objects.all()

    def post(self, request, *args, **kwargs):
        submission = generics.get_object_or_404(
            Submission.objects.select_related('assignment__project', 'submitted_by'),
            pk=self.kwargs['pk'],
        )
        principal = resolve_principal(request)
        authorization.require(
            authorization.can_review_submission(principal, submission),
            'Project reviewer role is required to review this submission.',
        )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        review_submission(
            submission=submission,
            reviewer=request.user,
            decision=serializer.validated_data['decision'],
            reason=serializer.validated_data.get('reason', ''),
        )
        submission.refresh_from_db()
        return Response(SubmissionSerializer(submission, context={'request': request}).data)


class SubmissionReleaseAPI(generics.GenericAPIView):
    permission_required = all_permissions.annotations_view
    queryset = Submission.objects.all()

    def get(self, request, *args, **kwargs):
        submission = generics.get_object_or_404(
            Submission.objects.select_related('assignment__project'),
            pk=self.kwargs['pk'],
        )
        principal = resolve_principal(request)
        authorization.require(
            authorization.can_manage_project(principal, submission.assignment.project),
            'Project manager role is required to release a submission.',
        )
        if not submission.is_releasable:
            raise ValidationError({'detail': 'Only approved submissions can be released.'})

        return Response(
            {
                'submission_id': submission.id,
                'revision': submission.revision,
                'result_hash': submission.result_hash,
                'result_snapshot': submission.result_snapshot,
            }
        )


class TaskAssignmentListCreateAPI(generics.ListCreateAPIView):
    serializer_class = TaskAssignmentSerializer
    permission_required = ViewClassPermission(
        GET=all_permissions.tasks_view,
        POST=all_permissions.tasks_change,
    )

    def get_queryset(self):
        user = self.request.user
        project_id = self.request.query_params.get('project')
        if project_id:
            project = generics.get_object_or_404(Project.objects.for_user(user), pk=project_id)
            principal = resolve_principal(self.request)
            authorization.require(
                authorization.can_manage_project(principal, project),
                'Project manager role is required to manage task assignments.',
            )

        queryset = TaskAssignment.objects.filter(project__organization=user.active_organization)
        queryset = queryset.filter(
            Q(project__created_by=user)
            | Q(
                project__members__user=user,
                project__members__enabled=True,
                project__members__role='manager',
            )
        ).distinct()

        task_id = self.request.query_params.get('task')
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        if task_id:
            queryset = queryset.filter(task_id=task_id)
        if bool_from_request(self.request.GET, 'active', False):
            queryset = queryset.filter(status__in=TaskAssignment.ACTIVE_STATUSES)
        return queryset.select_related('task', 'project', 'assignee', 'assigned_by', 'annotation').prefetch_related(
            'assignee__om_through'
        )

    def perform_create(self, serializer):
        task = serializer.validated_data['task']
        principal = resolve_principal(self.request)
        authorization.require(
            authorization.can_manage_task(principal, task),
            'Project manager role is required to assign tasks.',
        )
        serializer.save(project=task.project, assigned_by=self.request.user)


class TaskAssignmentEligibleAssigneePagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 100


class TaskAssignmentEligibleAssigneeListAPI(generics.ListAPIView):
    serializer_class = UserSimpleSerializer
    pagination_class = TaskAssignmentEligibleAssigneePagination
    permission_required = all_permissions.tasks_view

    def get_queryset(self):
        project_id = self.request.query_params.get('project')
        if not project_id:
            raise ValidationError({'project': 'Project is required.'})

        project = generics.get_object_or_404(Project.objects.for_user(self.request.user), pk=project_id)
        principal = resolve_principal(self.request)
        authorization.require(
            authorization.can_manage_project(principal, project),
            'Project manager role is required to manage task assignments.',
        )

        explicit_user_ids = ProjectMember.objects.filter(
            project=project,
            enabled=True,
            role__in=[ProjectMember.Role.ANNOTATOR, ProjectMember.Role.MANAGER],
        ).values('user_id')

        active_org_user_ids = OrganizationMember.objects.filter(
            organization=project.organization,
            deleted_at__isnull=True,
        ).values('user_id')

        eligible_users = User.objects.filter(
            Q(id__in=explicit_user_ids) | Q(id=project.created_by_id),
            id__in=active_org_user_ids,
            is_active=True,
        )
        return eligible_users.prefetch_related('om_through').order_by('email', 'id')


class TaskAssignmentAPI(generics.RetrieveDestroyAPIView):
    serializer_class = TaskAssignmentSerializer
    permission_required = ViewClassPermission(
        GET=all_permissions.tasks_view,
        DELETE=all_permissions.tasks_change,
    )

    def get_queryset(self):
        user = self.request.user
        return (
            TaskAssignment.objects.filter(project__organization=user.active_organization)
            .filter(
                Q(project__created_by=user)
                | Q(
                    project__members__user=user,
                    project__members__enabled=True,
                    project__members__role='manager',
                )
            )
            .distinct()
            .select_related('task', 'project', 'assignee', 'assigned_by', 'annotation')
            .prefetch_related('assignee__om_through')
        )

    def perform_destroy(self, instance):
        principal = resolve_principal(self.request)
        authorization.require(
            authorization.can_manage_task(principal, instance.task),
            'Project manager role is required to cancel task assignments.',
        )
        instance.cancel()


@method_decorator(
    name='list',
    decorator=extend_schema(
        tags=['Predictions'],
        summary='List predictions',
        description='List all predictions and their IDs.',
        parameters=[
            OpenApiParameter(
                name='task',
                type=OpenApiTypes.INT,
                location='query',
                description='Filter predictions by task ID',
            ),
            OpenApiParameter(
                name='project',
                type=OpenApiTypes.INT,
                location='query',
                description='Filter predictions by project ID',
            ),
        ],
        request=None,
        responses={
            '200': OpenApiResponse(
                description='Predictions list',
                response=PredictionSerializer(many=True),
                examples=[
                    OpenApiExample(name='response', value=[prediction_response_example], media_type='application/json')
                ],
            )
        },
        extensions={
            'x-fern-sdk-group-name': 'predictions',
            'x-fern-sdk-method-name': 'list',
            'x-fern-audiences': ['public'],
        },
    ),
)
@method_decorator(
    name='create',
    decorator=extend_schema(
        tags=['Predictions'],
        summary='Create prediction',
        description='Create a prediction for a specific task.',
        request={
            'application/json': prediction_request_schema,
        },
        responses={
            '201': OpenApiResponse(
                description='Created prediction',
                response=PredictionSerializer,
                examples=[
                    OpenApiExample(name='response', value=prediction_response_example, media_type='application/json')
                ],
            )
        },
        extensions={
            'x-fern-sdk-group-name': 'predictions',
            'x-fern-sdk-method-name': 'create',
            'x-fern-audiences': ['public'],
        },
    ),
)
@method_decorator(
    name='retrieve',
    decorator=extend_schema(
        tags=['Predictions'],
        summary='Get prediction details',
        description='Get details about a specific prediction by its ID.',
        parameters=[
            OpenApiParameter(name='id', type=OpenApiTypes.INT, location='path', description='Prediction ID'),
        ],
        request=None,
        responses={
            '200': OpenApiResponse(
                description='Prediction details',
                response=PredictionSerializer,
                examples=[
                    OpenApiExample(name='response', value=prediction_response_example, media_type='application/json')
                ],
            )
        },
        extensions={
            'x-fern-sdk-group-name': 'predictions',
            'x-fern-sdk-method-name': 'get',
            'x-fern-audiences': ['public'],
        },
    ),
)
@method_decorator(
    name='update',
    decorator=extend_schema(
        tags=['Predictions'],
        summary='Put prediction',
        description='Overwrite prediction data by prediction ID.',
        parameters=[
            OpenApiParameter(name='id', type=OpenApiTypes.INT, location='path', description='Prediction ID'),
        ],
        request={
            'application/json': prediction_request_schema,
        },
        responses={
            '200': OpenApiResponse(
                description='Updated prediction',
                response=PredictionSerializer,
                examples=[
                    OpenApiExample(name='response', value=prediction_response_example, media_type='application/json')
                ],
            )
        },
        extensions={
            'x-fern-audiences': ['internal'],
        },
    ),
)
@method_decorator(
    name='partial_update',
    decorator=extend_schema(
        tags=['Predictions'],
        summary='Update prediction',
        description='Update prediction data by prediction ID.',
        parameters=[
            OpenApiParameter(name='id', type=OpenApiTypes.INT, location='path', description='Prediction ID'),
        ],
        request={
            'application/json': prediction_request_schema,
        },
        responses={
            '200': OpenApiResponse(
                description='Updated prediction',
                response=PredictionSerializer,
                examples=[
                    OpenApiExample(name='response', value=prediction_response_example, media_type='application/json')
                ],
            )
        },
        extensions={
            'x-fern-sdk-group-name': 'predictions',
            'x-fern-sdk-method-name': 'update',
            'x-fern-audiences': ['public'],
        },
    ),
)
@method_decorator(
    name='destroy',
    decorator=extend_schema(
        tags=['Predictions'],
        summary='Delete prediction',
        description='Delete a prediction by prediction ID.',
        parameters=[
            OpenApiParameter(name='id', type=OpenApiTypes.INT, location='path', description='Prediction ID'),
        ],
        request=None,
        extensions={
            'x-fern-sdk-group-name': 'predictions',
            'x-fern-sdk-method-name': 'delete',
            'x-fern-audiences': ['public'],
        },
    ),
)
class PredictionAPI(viewsets.ModelViewSet):
    serializer_class = PredictionSerializer
    permission_required = all_permissions.predictions_any
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['task', 'task__project', 'project']

    def get_queryset(self):
        return Prediction.objects.filter(project__organization=self.request.user.active_organization)


@method_decorator(name='get', decorator=extend_schema(exclude=True))
@method_decorator(
    name='post',
    decorator=extend_schema(
        tags=['Annotations'],
        summary='Convert annotation to draft',
        description='Convert annotation to draft',
        extensions={
            'x-fern-audiences': ['internal'],
        },
    ),
)
class AnnotationConvertAPI(generics.RetrieveAPIView):
    permission_required = ViewClassPermission(POST=all_permissions.annotations_change)
    queryset = Annotation.objects.all()

    def process_intermediate_state(self, annotation, draft):
        pass

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        annotation = self.get_object()
        principal, actor = resolve_actor(request)
        assignment = lock_active_assignment(request, principal, annotation.task, require_token=False)
        if assignment.annotation_id != annotation.id:
            raise PermissionDenied('Only the active assignment owner can convert this annotation to a draft.')
        check_submission_draft(request, assignment, actor, annotation)
        if lock_drafts(annotation.task).filter(assignment=assignment, user=actor, annotation=None).exists():
            raise DraftConflictError()

        organization = annotation.project.organization
        project = annotation.project
        pk = annotation.pk

        draft = AnnotationDraft.objects.create(
            result=annotation.result,
            lead_time=annotation.lead_time,
            task=annotation.task,
            annotation=None,
            user=actor,
            assignment=assignment,
        )

        self.process_intermediate_state(annotation, draft)

        annotation.delete()

        emit_webhooks_for_instance(organization, project, WebhookAction.ANNOTATIONS_DELETED, [pk])
        data = AnnotationDraftSerializer(instance=draft).data
        return Response(status=201, data=data)
