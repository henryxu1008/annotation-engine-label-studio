"""This file and its contents are licensed under the Apache License 2.0. Please see the included NOTICE for copyright information and LICENSE for a copy of the license.
"""
import logging

from access_control.authorization import authorization
from access_control.identity import resolve_actor, resolve_principal
from access_control.project_access import (
    get_visible_project_or_404,
    require_project_manager,
    visible_projects_for_actor,
)
from asgiref.sync import async_to_sync, sync_to_async
from core.api_permissions import HasActorObjectPermission
from core.feature_flags import flag_set
from core.permissions import ViewClassPermission, all_permissions
from core.utils.common import int_from_request, load_func
from core.utils.params import bool_from_request
from data_manager.actions import get_action_form, get_all_actions, perform_action
from data_manager.functions import evaluate_predictions, get_prepare_params
from data_manager.managers import get_fields_for_evaluation
from data_manager.models import View
from data_manager.prepare_params import filters_schema, ordering_schema, prepare_params_schema
from data_manager.serializers import (
    DataManagerTaskSerializer,
    ViewOrderSerializer,
    ViewResetSerializer,
    ViewSerializer,
)
from django.conf import settings
from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils.decorators import method_decorator
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from projects.models import Project
from projects.serializers import ProjectSerializer
from rest_framework import generics, viewsets
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from tasks.models import Annotation, Prediction, Task

logger = logging.getLogger(__name__)

_view_request_body = {
    'application/json': {
        'type': 'object',
        'properties': {
            'data': {
                'type': 'object',
                'description': 'Custom view data',
                'properties': {'filters': filters_schema, 'ordering': ordering_schema},
            },
            'project': {'type': 'integer', 'description': 'Project ID'},
        },
    },
}


@method_decorator(
    name='list',
    decorator=extend_schema(
        tags=['Data Manager'],
        summary='List views',
        description='List all views for a specific project.',
        parameters=[
            OpenApiParameter(name='project', type=OpenApiTypes.INT, location='query', description='Project ID'),
        ],
        extensions={
            'x-fern-sdk-group-name': 'views',
            'x-fern-sdk-method-name': 'list',
            'x-fern-audiences': ['public'],
        },
    ),
)
@method_decorator(
    name='create',
    decorator=extend_schema(
        tags=['Data Manager'],
        summary='Create view',
        description='Create a view for a specific project.',
        request=_view_request_body,
        responses={201: ViewSerializer},
        extensions={
            'x-fern-sdk-group-name': 'views',
            'x-fern-sdk-method-name': 'create',
            'x-fern-audiences': ['public'],
        },
    ),
)
@method_decorator(
    name='retrieve',
    decorator=extend_schema(
        tags=['Data Manager'],
        summary='Get view details',
        description='Get the details about a specific view in the data manager',
        parameters=[
            OpenApiParameter(name='id', type=OpenApiTypes.STR, location='path', description='View ID'),
        ],
        extensions={
            'x-fern-sdk-group-name': 'views',
            'x-fern-sdk-method-name': 'get',
            'x-fern-audiences': ['public'],
        },
    ),
)
@method_decorator(
    name='update',
    decorator=extend_schema(
        tags=['Data Manager'],
        summary='Put view',
        description='Overwrite view data with updated filters and other information for a specific project.',
        request=_view_request_body,
        parameters=[
            OpenApiParameter(name='id', type=OpenApiTypes.STR, location='path', description='View ID'),
        ],
        extensions={
            'x-fern-audiences': ['internal'],
        },
    ),
)
@method_decorator(
    name='partial_update',
    decorator=extend_schema(
        tags=['Data Manager'],
        summary='Update view',
        description='Update view data with additional filters and other information for a specific project.',
        parameters=[
            OpenApiParameter(name='id', type=OpenApiTypes.STR, location='path', description='View ID'),
        ],
        request=_view_request_body,
        responses={200: ViewSerializer},
        extensions={
            'x-fern-sdk-group-name': 'views',
            'x-fern-sdk-method-name': 'update',
            'x-fern-audiences': ['public'],
        },
    ),
)
@method_decorator(
    name='destroy',
    decorator=extend_schema(
        tags=['Data Manager'],
        summary='Delete view',
        description='Delete a specific view by ID.',
        parameters=[
            OpenApiParameter(name='id', type=OpenApiTypes.STR, location='path', description='View ID'),
        ],
        extensions={
            'x-fern-sdk-group-name': 'views',
            'x-fern-sdk-method-name': 'delete',
            'x-fern-audiences': ['public'],
        },
    ),
)
class ViewAPI(viewsets.ModelViewSet):
    permission_classes = (HasActorObjectPermission, IsAuthenticated)
    serializer_class = ViewSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['project']
    permission_required = ViewClassPermission(
        GET=all_permissions.views_view,
        POST=all_permissions.views_create,
        PATCH=all_permissions.views_change,
        PUT=all_permissions.views_change,
        DELETE=all_permissions.views_delete,
    )

    @transaction.atomic
    def list(self, request, *args, **kwargs):
        project_id = request.query_params.get('project')
        if project_id:
            from access_control.project_operations import READ_WORK, lock_project_operation
            from classification_disagreements.blind_participant_projection import sensitive_project

            principal, actor = resolve_actor(request)
            try:
                project = visible_projects_for_actor(actor).filter(pk=project_id).first()
            except (TypeError, ValueError, OverflowError):
                raise NotFound('Project is unavailable.')
            if project is None:
                return Response([])
            if sensitive_project(project):
                lock_project_operation(project.pk, principal, READ_WORK)
        return super().list(request, *args, **kwargs)

    @transaction.atomic
    def retrieve(self, request, *args, **kwargs):
        from access_control.project_operations import READ_WORK, lock_project_operation
        from classification_disagreements.blind_participant_projection import sensitive_project

        view = self.get_object()
        if sensitive_project(view.project):
            lock_project_operation(view.project_id, resolve_principal(request), READ_WORK)
        return super().retrieve(request, *args, **kwargs)

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        from access_control.project_operations import READ_WORK, lock_project_operation
        from classification_disagreements.blind_participant_projection import sensitive_project

        view = self.get_object()
        raw_target = request.data.get('project', view.project_id)
        if (isinstance(raw_target, bool) or not isinstance(raw_target, (int, str))
                or not str(raw_target).isascii() or not str(raw_target).isdecimal()
                or len(str(raw_target)) > 18 or int(raw_target) <= 0):
            raise ValidationError({'project': 'Invalid project.'})
        principal, actor = resolve_actor(request)
        target = generics.get_object_or_404(visible_projects_for_actor(actor), pk=int(raw_target))
        if sensitive_project(view.project) or sensitive_project(target):
            # A view can move between projects. Lock both in a stable order,
            # then let get_object and perform_update recheck the live role.
            for project_id in sorted({view.project_id, target.pk}):
                lock_project_operation(project_id, principal, READ_WORK)
            current = View.objects.select_for_update().filter(pk=view.pk).values_list('project_id', flat=True).first()
            if current != view.project_id:
                raise NotFound('Work is unavailable.')
        return super().update(request, *args, **kwargs)

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        _, actor = resolve_actor(self.request)
        project = generics.get_object_or_404(
            visible_projects_for_actor(actor), pk=serializer.validated_data['project'].pk
        )
        from classification_disagreements.blind_participant_projection import sensitive_nonmanager

        principal = resolve_principal(self.request)
        if sensitive_nonmanager(project, principal):
            from access_control.project_operations import READ_WORK, lock_project_operation

            project, _ = lock_project_operation(project.pk, principal, READ_WORK)
        if sensitive_nonmanager(project, principal) and not authorization.can_review_project(principal, project):
            raise NotFound('Work is unavailable.')
        serializer.save(user=actor, project=project)

    def perform_update(self, serializer):
        _, actor = resolve_actor(self.request)
        project = serializer.validated_data.get('project', serializer.instance.project)
        project = generics.get_object_or_404(visible_projects_for_actor(actor), pk=project.pk)
        from classification_disagreements.blind_participant_projection import sensitive_nonmanager

        principal = resolve_principal(self.request)
        if sensitive_nonmanager(project, principal) and not authorization.can_review_project(principal, project):
            raise NotFound('Work is unavailable.')
        serializer.save(project=project, user=serializer.instance.user)

    @extend_schema(
        tags=['Data Manager'],
        summary='Delete all project views',
        description='Delete all views for a specific project.',
        parameters=[
            OpenApiParameter(
                name='project',
                type=OpenApiTypes.INT,
                location='query',
                description='Project ID',
                required=True,
            ),
        ],
        extensions={
            'x-fern-sdk-group-name': 'views',
            'x-fern-sdk-method-name': 'delete_all',
            'x-fern-audiences': ['public'],
        },
    )
    @action(detail=False, methods=['delete'], permission_required=all_permissions.views_reset)
    def reset(self, request):
        # Note: OpenAPI 3.0 does not support request body for DELETE requests
        # see https://github.com/tfranzel/drf-spectacular/issues/431#issuecomment-862738643
        # as a hack for the SDK, fallback to query params if request body is empty
        serializer = ViewResetSerializer(
            data=request.data if 'project' in request.data else {'project': request.query_params.get('project')}
        )
        serializer.is_valid(raise_exception=True)
        _, actor = resolve_actor(request)
        project = generics.get_object_or_404(visible_projects_for_actor(actor), pk=serializer.validated_data['project'].id)
        queryset = self.filter_queryset(self.get_queryset()).filter(project=project)
        queryset.all().delete()
        return Response(status=204)

    @extend_schema(
        tags=['Data Manager'],
        summary='Update order of views',
        description='Update the order field of views based on the provided list of view IDs',
        request=ViewOrderSerializer,
        responses={200: OpenApiResponse(description='View order updated successfully')},
        extensions={
            'x-fern-sdk-group-name': 'views',
            'x-fern-sdk-method-name': 'update_order',
            'x-fern-audiences': ['public'],
        },
    )
    @action(detail=False, methods=['post'], url_path='order')
    def update_order(self, request):
        serializer = ViewOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        project_id = serializer.validated_data['project']
        view_ids = serializer.validated_data['ids']

        _, actor = resolve_actor(request)
        project = generics.get_object_or_404(visible_projects_for_actor(actor), pk=project_id)

        queryset = self.filter_queryset(self.get_queryset()).filter(project=project)
        views = list(queryset.filter(id__in=view_ids))

        # Update the order field for each view
        view_dict = {view.id: view for view in views}
        for order, view_id in enumerate(view_ids):
            if view_id in view_dict:
                view_dict[view_id].order = order

        # Bulk update views
        View.objects.bulk_update(views, ['order'])

        return Response(status=200)

    def get_queryset(self):
        from classification_disagreements.blind_participant_projection import sensitive_project_filter

        _, actor = resolve_actor(self.request)
        roles = ['manager', 'reviewer']
        if self.action == 'list' and not self.request.query_params.get('project'):
            roles = ['manager']
        privileged = Project.objects.filter(
            Q(created_by=actor) | Q(members__user=actor, members__enabled=True, members__role__in=roles)
        ).values('pk')
        return (View.objects.filter(project_id__in=visible_projects_for_actor(actor).values('pk'))
                .exclude(sensitive_project_filter('project') & ~Q(project_id__in=privileged))
                .order_by('order', 'id'))


class TaskPagination(PageNumberPagination):
    page_size = 100
    page_size_query_param = 'page_size'
    total_annotations = 0
    total_predictions = 0
    max_page_size = settings.TASK_API_PAGE_SIZE_MAX

    def _annotations_count_queryset(self, queryset, request):
        annotations = Annotation.objects.filter(task_id__in=queryset, was_cancelled=False)
        project_ids = list(queryset.order_by().values_list('project_id', flat=True).distinct())
        principal = resolve_principal(request)
        managed_project_ids = [
            project.id
            for project in Project.objects.filter(id__in=project_ids).only('id', 'created_by_id')
            if authorization.can_manage_project(principal, project)
        ]
        if len(managed_project_ids) != len(project_ids):
            annotations = annotations.filter(
                Q(task__project_id__in=managed_project_ids)
                | Q(
                    task_assignment__assignee_id=principal.local_user_id,
                    task_assignment__status__in=['assigned', 'in_progress'],
                )
            )
        return annotations.distinct()

    @async_to_sync
    async def async_paginate_queryset(self, queryset, request, view=None):
        from classification_disagreements.blind_participant_projection import restrict_prediction_queryset

        self.total_predictions = await sync_to_async(
            lambda: restrict_prediction_queryset(Prediction.objects.filter(task_id__in=queryset), request).count(),
            thread_sensitive=True,
        )()

        annotations_count_qs = self._annotations_count_queryset(queryset, request)
        self.total_annotations = await sync_to_async(annotations_count_qs.count, thread_sensitive=True)()
        # Use .only('id') to avoid loading heavy task.data fields during pagination
        # Full task objects are loaded later with proper annotations
        id_only_queryset = queryset.only('id')
        return await sync_to_async(super().paginate_queryset, thread_sensitive=True)(id_only_queryset, request, view)

    def sync_paginate_queryset(self, queryset, request, view=None):
        from classification_disagreements.blind_participant_projection import restrict_prediction_queryset

        self.total_predictions = restrict_prediction_queryset(Prediction.objects.filter(task_id__in=queryset), request).count()
        self.total_annotations = self._annotations_count_queryset(queryset, request).count()
        # Use .only('id') to avoid loading heavy task.data fields during pagination
        # Full task objects are loaded later with proper annotations
        id_only_queryset = queryset.only('id')
        return super().paginate_queryset(id_only_queryset, request, view)

    def paginate_totals_queryset(self, queryset, request, view=None):
        project_ids = list(queryset.order_by().values_list('project_id', flat=True).distinct())
        principal = resolve_principal(request)
        if any(
            not authorization.can_manage_project(principal, project)
            for project in Project.objects.filter(id__in=project_ids).only('id', 'created_by_id')
        ):
            return self.sync_paginate_queryset(queryset, request, view)

        totals = queryset.values('id').aggregate(
            total_annotations=Coalesce(Sum('total_annotations'), 0),
            total_predictions=Coalesce(Sum('total_predictions'), 0),
        )
        self.total_annotations = totals['total_annotations']
        self.total_predictions = totals['total_predictions']
        # Use .only('id') to avoid loading heavy task.data fields during pagination
        # Full task objects are loaded later with proper annotations
        id_only_queryset = queryset.only('id')
        return super().paginate_queryset(id_only_queryset, request, view)

    def paginate_queryset(self, queryset, request, view=None):
        if flag_set('fflag_fix_back_optic_1407_optimize_tasks_api_pagination_counts'):
            return self.paginate_totals_queryset(queryset, request, view)
        return self.sync_paginate_queryset(queryset, request, view)

    def get_paginated_response_schema(self, schema):
        return {
            'type': 'object',
            'properties': {
                'tasks': schema,
                'total': {
                    'type': 'integer',
                    'description': 'Total number of tasks',
                    'example': 123,
                },
                'total_annotations': {
                    'type': 'integer',
                    'description': 'Total number of annotations',
                    'example': 456,
                },
                'total_predictions': {
                    'type': 'integer',
                    'description': 'Total number of predictions',
                    'example': 78,
                },
            },
            'required': ['tasks', 'total', 'total_annotations', 'total_predictions'],
        }

    def get_paginated_response(self, data):
        return Response(
            {
                'total_annotations': self.total_annotations,
                'total_predictions': self.total_predictions,
                'total': self.page.paginator.count,
                'tasks': data,
            }
        )


class TaskListAPI(generics.ListCreateAPIView):
    permission_classes = (HasActorObjectPermission, IsAuthenticated)
    task_serializer_class = DataManagerTaskSerializer
    permission_required = ViewClassPermission(
        GET=all_permissions.tasks_view,
        POST=all_permissions.tasks_change,
        PATCH=all_permissions.tasks_change,
        PUT=all_permissions.tasks_change,
        DELETE=all_permissions.tasks_delete,
    )
    pagination_class = TaskPagination

    def get_task_serializer_context(self, request, project, queryset):
        all_fields = request.GET.get('fields', None) == 'all'  # false by default

        return {
            'resolve_uri': bool_from_request(request.GET, 'resolve_uri', True),
            'request': request,
            'project': project,
            'drafts': all_fields,
            'predictions': all_fields,
            'annotations': all_fields,
        }

    def get_task_queryset(self, request, prepare_params):
        queryset = Task.prepared.only_filtered(prepare_params=prepare_params)
        _, actor = resolve_actor(request)
        allowed_task_ids = Task.objects.for_user(actor).filter(
            project_id__in=visible_projects_for_actor(actor).values('pk')
        ).values_list('id', flat=True)
        return queryset.filter(id__in=allowed_task_ids)

    @staticmethod
    def prefetch(queryset):
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
        )

    @transaction.atomic
    def get(self, request):
        # get project
        view_pk = int_from_request(request.GET, 'view', 0) or int_from_request(request.data, 'view', 0)
        project_pk = int_from_request(request.GET, 'project', 0) or int_from_request(request.data, 'project', 0)
        if project_pk:
            project = generics.get_object_or_404(Project, pk=project_pk)
            self.check_object_permissions(request, project)
        elif view_pk:
            view = generics.get_object_or_404(View, pk=view_pk)
            project = view.project
            self.check_object_permissions(request, project)
        else:
            return Response({'detail': 'Neither project nor view id specified'}, status=404)
        # The legacy queue serializes raw task IDs and arbitrary task data.
        # Resolve the project from either input before applying the same rule.
        from classification_disagreements.blind_participant_projection import sensitive_nonmanager
        from access_control.project_operations import lock_project_actor

        principal = resolve_principal(request)
        project, _ = lock_project_actor(project.pk, principal)
        self.check_object_permissions(request, project)
        if sensitive_nonmanager(project, principal):
            from access_control.project_operations import READ_WORK, lock_project_operation

            # Reviewers need the legacy shell to open the ordinary review
            # workspace. Their task queryset is empty; hold membership locks
            # until serialization so a concurrent role change cannot turn
            # this request into an annotator's raw-ID task listing.
            project, _ = lock_project_operation(project.pk, principal, READ_WORK)
            if not authorization.can_review_project(principal, project):
                raise NotFound('Work is unavailable.')
        # get prepare params (from view or from payload directly)
        prepare_params = get_prepare_params(request, project)
        queryset = self.get_task_queryset(request, prepare_params)

        # paginated tasks
        page = self.paginate_queryset(queryset)

        # get request params
        all_fields = 'all' if request.GET.get('fields', None) == 'all' else None
        fields_for_evaluation = get_fields_for_evaluation(prepare_params, request.user)
        review = bool_from_request(self.request.GET, 'review', False)

        if review:
            fields_for_evaluation = ['annotators', 'reviewed']
            all_fields = None
        if page is not None:
            ids = [task.id for task in page]  # page is a list already
            tasks = self.prefetch(
                Task.prepared.annotate_queryset(
                    Task.objects.filter(id__in=ids),
                    fields_for_evaluation=fields_for_evaluation,
                    all_fields=all_fields,
                    request=request,
                )
            )

            tasks_by_ids = {task.id: task for task in tasks}
            # keep ids ordering
            page = [tasks_by_ids[_id] for _id in ids]

            # retrieve ML predictions if tasks don't have them
            if not review and project.evaluate_predictions_automatically:
                # TODO MM TODO this needs a discussion, because I'd expect
                # people to retrieve manually instead on DM load, plus it
                # will slow down initial DM load
                # if project.retrieve_predictions_automatically is deprecated now and no longer used
                tasks_for_predictions = Task.objects.filter(id__in=ids, predictions__isnull=True)
                evaluate_predictions(tasks_for_predictions)
                [tasks_by_ids[_id].refresh_from_db() for _id in ids]

            context = self.get_task_serializer_context(self.request, project, tasks)
            serializer = self.task_serializer_class(page, many=True, context=context)
            return self.get_paginated_response(serializer.data)
        # all tasks
        if project.evaluate_predictions_automatically:
            evaluate_predictions(queryset.filter(predictions__isnull=True))
        queryset = Task.prepared.annotate_queryset(
            queryset, fields_for_evaluation=fields_for_evaluation, all_fields=all_fields, request=request
        )
        context = self.get_task_serializer_context(self.request, project, queryset)
        serializer = self.task_serializer_class(queryset, many=True, context=context)
        return Response(serializer.data)


@method_decorator(
    name='get',
    decorator=extend_schema(
        tags=['Data Manager'],
        summary='Get data manager columns',
        description=(
            'Retrieve the data manager columns available for the tasks in a specific project. '
            'For more details, see [GET api/actions](#/Data%20Manager/get_api_actions).'
        ),
        parameters=[
            OpenApiParameter(
                name='project',
                type=OpenApiTypes.INT,
                location='query',
                description='Project ID',
                required=True,
            )
        ],
        responses={
            200: OpenApiResponse(
                description='Columns retrieved successfully',
                examples=[
                    OpenApiExample(
                        name='response',
                        value={
                            'columns': [
                                {
                                    'id': 'id',
                                    'title': 'ID',
                                    'type': 'Number',
                                    'help': 'Task ID',
                                    'target': 'tasks',
                                    'visibility_defaults': {'explore': True, 'labeling': False},
                                    'project_defined': False,
                                },
                                {
                                    'id': 'completed_at',
                                    'title': 'Completed',
                                    'type': 'Datetime',
                                    'target': 'tasks',
                                    'help': 'Last annotation date',
                                    'visibility_defaults': {'explore': True, 'labeling': False},
                                    'project_defined': False,
                                },
                                # ... other columns ...
                            ]
                        },
                        media_type='application/json',
                    )
                ],
            ),
            400: OpenApiResponse(description='Invalid project ID supplied'),
            404: OpenApiResponse(description='Project not found'),
        },
        extensions={
            'x-fern-audiences': ['internal'],
        },
    ),
)
class ProjectColumnsAPI(APIView):
    permission_classes = (HasActorObjectPermission, IsAuthenticated)
    permission_required = all_permissions.projects_view

    def get(self, request):
        pk = int_from_request(request.GET, 'project', 1)
        _, actor = resolve_actor(request)
        project = generics.get_object_or_404(visible_projects_for_actor(actor), pk=pk)
        self.check_object_permissions(request, project)
        GET_ALL_COLUMNS = load_func(settings.DATA_MANAGER_GET_ALL_COLUMNS)
        data = GET_ALL_COLUMNS(project, actor)
        from classification_disagreements.blind_participant_projection import participant_for_project

        if participant_for_project(request, project) is not None:
            if not isinstance(data, dict) or not isinstance(data.get('columns'), list):
                raise NotFound('Work is unavailable.')
            configured = {key for key in (project.data_types or {}) if '[' not in key}
            columns = [column for column in data['columns']
                       if isinstance(column, dict) and (column.get('parent') != 'data'
                                                        or column.get('id') in configured)]
            for column in columns:
                if column.get('id') == 'data' and column.get('target') == 'tasks':
                    column['children'] = [child for child in column.get('children', []) if child in configured]
                if column.get('id') == 'predictions_model_versions':
                    column['schema'] = {'items': [], 'multiple': True}
            data = {**data, 'columns': columns}
        return Response(data)


@method_decorator(
    name='get',
    decorator=extend_schema(
        tags=['Data Manager'],
        summary='Get project state',
        description='Retrieve the project state for the data manager.',
        extensions={
            'x-fern-audiences': ['internal'],
        },
    ),
)
class ProjectStateAPI(APIView):
    permission_classes = (HasActorObjectPermission, IsAuthenticated)
    permission_required = all_permissions.projects_view

    def get(self, request):
        pk = int_from_request(request.GET, 'project', 1)  # replace 1 to None, it's for debug only
        _, actor = resolve_actor(request)
        project = generics.get_object_or_404(visible_projects_for_actor(actor), pk=pk)
        self.check_object_permissions(request, project)
        data = ProjectSerializer(project, context={'request': request}).data

        from classification_disagreements.blind_participant_projection import participant_counts, participant_for_project

        participant = participant_for_project(request, project)
        counts = None
        if participant is not None:
            counts = data if {'task_number', 'total_annotations_number'} <= data.keys() else participant_counts(
                project, participant,
            )

        data.update(
            {
                'can_delete_tasks': participant is None,
                'can_manage_annotations': participant is None,
                'can_manage_tasks': participant is None,
                'source_syncing': False,
                'target_syncing': False,
                'task_count': counts['task_number'] if counts is not None else project.tasks.count(),
                'annotation_count': counts['total_annotations_number'] if counts is not None
                else Annotation.objects.filter(project=project).count(),
                'config_has_control_tags': len(project.get_parsed_config()) > 0,
            }
        )
        return Response(data)


class BlindBootstrapMixin:
    """Expose only the already-projected bootstrap for a live participant."""

    @transaction.atomic
    def get(self, request):
        from access_control.project_operations import READ_WORK, lock_project_operation
        from classification_disagreements.blind_participant_projection import sensitive_nonmanager

        values = request.query_params.getlist('project')
        if (set(request.query_params) != {'project'} or len(values) != 1 or not values[0].isascii()
                or not values[0].isdecimal() or len(values[0]) > 18 or int(values[0]) <= 0):
            raise NotFound('Work is unavailable.')
        principal, actor = resolve_actor(request)
        candidate = generics.get_object_or_404(visible_projects_for_actor(actor), pk=int(values[0]))
        project, _ = lock_project_operation(candidate.pk, principal, READ_WORK)
        if not sensitive_nonmanager(project, principal):
            raise NotFound('Work is unavailable.')
        response = super().get(request)
        if not sensitive_nonmanager(project, principal):
            raise NotFound('Work is unavailable.')
        response['Cache-Control'] = 'private, no-store'
        return response


class BlindProjectStateAPI(BlindBootstrapMixin, ProjectStateAPI):
    pass


class BlindProjectColumnsAPI(BlindBootstrapMixin, ProjectColumnsAPI):
    pass


@method_decorator(
    name='get',
    decorator=extend_schema(
        tags=['Data Manager'],
        summary='Get actions',
        description='Retrieve all the registered actions with descriptions that data manager can use.',
        parameters=[
            OpenApiParameter(
                name='project',
                type=OpenApiTypes.INT,
                location='query',
                description='Project ID',
                required=True,
            ),
        ],
        responses={
            200: OpenApiResponse(
                description='Actions retrieved successfully',
                response={
                    'type': 'array',
                    'title': 'Action list',
                    'description': 'List of available actions',
                    'items': {
                        'type': 'object',
                        'title': 'Action',
                        'properties': {
                            'id': {'type': 'string', 'title': 'Action ID'},
                            'title': {'type': 'string', 'title': 'Title'},
                            'order': {'type': 'integer', 'title': 'Order'},
                            'permission': {
                                'oneOf': [
                                    {'type': 'string'},
                                    {
                                        'type': 'array',
                                        'items': {'type': 'string'},
                                        'description': 'List of permissions (user needs any all of them)',
                                    },
                                ]
                            },
                            'experimental': {'type': 'boolean', 'title': 'Experimental'},
                            'dialog': {
                                'type': 'object',
                                'title': 'Dialog',
                                'properties': {
                                    'title': {'type': 'string', 'title': 'Title', 'nullable': True},
                                    'text': {'type': 'string', 'title': 'Text', 'nullable': True},
                                    'type': {'type': 'string', 'title': 'Type', 'nullable': True},
                                    'form': {
                                        'type': 'array',
                                        'title': 'Form',
                                        'items': {'type': 'object'},
                                        'nullable': True,
                                    },
                                },
                            },
                        },
                    },
                },
                examples=[
                    OpenApiExample(
                        name='response',
                        value=[
                            {
                                'id': 'predictions_to_annotations',
                                'title': 'Create Annotations From Predictions',
                                'order': 91,
                                'permission': 'tasks.change',
                                'experimental': False,
                                'dialog': {
                                    'title': 'Create Annotations From Predictions',
                                    'text': 'Create annotations from predictions using selected predictions set for each selected task. Your account will be assigned as an owner to those annotations.',
                                    'type': 'confirm',
                                    'form': None,
                                },
                            }
                        ],
                        media_type='application/json',
                    ),
                ],
            )
        },
        extensions={
            'x-fern-sdk-group-name': 'actions',
            'x-fern-sdk-method-name': 'list',
            'x-fern-audiences': ['public'],
        },
    ),
)
@method_decorator(
    name='post',
    decorator=extend_schema(
        tags=['Data Manager'],
        summary='Post actions',
        description=(
            'Perform a Data Manager action with the selected tasks and filters. '
            'Note: More complex actions require additional parameters in the request body. '
            'Call `GET api/actions?project=<id>` to explore them. <br>'
            'Example: `GET api/actions?id=delete_tasks&project=1`'
        ),
        request={
            'application/json': prepare_params_schema,
        },
        parameters=[
            OpenApiParameter(
                name='id',
                type=OpenApiTypes.STR,
                location='query',
                description='Action name ID, see the full list of actions in the `GET api/actions` request',
                enum=[
                    'retrieve_tasks_predictions',
                    'predictions_to_annotations',
                    'remove_duplicates',
                    'delete_tasks',
                    'delete_ground_truths',
                    'delete_tasks_annotations',
                    'delete_tasks_reviews',
                    'delete_tasks_predictions',
                    'delete_reviewers',
                    'delete_annotators',
                ],
                required=True,
            ),
            OpenApiParameter(
                name='project',
                type=OpenApiTypes.INT,
                location='query',
                description='Project ID',
                required=True,
            ),
            OpenApiParameter(
                name='view',
                type=OpenApiTypes.INT,
                location='query',
                description='View ID (optional, it has higher priority than filters, '
                'selectedItems and ordering from the request body payload)',
            ),
        ],
        responses={200: OpenApiResponse(description='Action performed successfully')},
        extensions={
            'x-fern-sdk-group-name': 'actions',
            'x-fern-sdk-method-name': 'create',
            'x-fern-audiences': ['public'],
        },
    ),
)
class ProjectActionsAPI(APIView):
    permission_required = ViewClassPermission(
        GET=all_permissions.projects_view,
        POST=all_permissions.projects_view,
    )

    def get(self, request):
        pk = int_from_request(request.GET, 'project', 0)
        project = get_visible_project_or_404(request, pk)
        return Response(get_all_actions(request.user, project))

    def post(self, request):
        from task_packages.scope import PackageScope

        pk = int_from_request(request.GET, 'project', 0)
        action_id = request.GET.get('id', None)
        scope = PackageScope.from_project_id(request, pk) if action_id == 'next_task' else None
        project = scope.project if scope else get_visible_project_or_404(request, pk)
        # The labeling toolbar reads its queue through this action endpoint.
        # Keep mutation actions manager-only; next_task uses the existing
        # labeling role and server-side assignment scope instead.
        if action_id == 'next_task':
            authorization.require(
                authorization.project_role(resolve_principal(request), project) in ('manager', 'annotator'),
                'A project labeling role is required for this operation.',
            )
        else:
            require_project_manager(request, project)

        # keep ordering only when needed, otherwise drop to avoid expensive sorts/annotations
        prepare_params = get_prepare_params(request, project)
        if not flag_set(
            'fflag_root_223_optimize_delete_predictions', organization=project.organization
        ) or action_id in ['next_task', 'remove_duplicates']:
            queryset = Task.prepared.only_filtered(prepare_params=prepare_params)
        else:
            prepare_params.ordering = []
            queryset = Task.prepared.only_filtered(prepare_params=prepare_params)
            queryset = queryset.order_by()

        if action_id == 'next_task':
            queue_actor = scope.actor if scope else resolve_actor(request)[1]
            allowed_task_ids = Task.objects.for_user(queue_actor).filter(project=project).values_list('id', flat=True)
            queryset = queryset.filter(id__in=allowed_task_ids)

        # wrong action id
        if action_id is None:
            response = {'detail': 'No action id "' + str(action_id) + '", use ?id=<action-id>'}
            return Response(response, status=422)

        # perform action and return the result dict
        kwargs = {'request': request}  # pass advanced params to actions
        result = perform_action(action_id, project, queryset, resolve_actor(request)[1], **kwargs)
        code = result.pop('response_code', 200)

        return Response(result, status=code)


@method_decorator(
    name='get',
    decorator=extend_schema(
        tags=['Data Manager'],
        summary='Get action form',
        description='Get the form configuration for a specific action.',
        parameters=[
            OpenApiParameter(
                name='project',
                type=OpenApiTypes.INT,
                location='query',
                description='Project ID',
                required=True,
            )
        ],
        responses={
            200: OpenApiResponse(
                description='Action form configuration returned successfully',
                response={
                    'type': 'object',
                    'description': 'Form configuration object',
                },
            )
        },
        extensions={
            'x-fern-audiences': ['internal'],
        },
    ),
)
class ProjectActionsFormAPI(APIView):
    permission_required = ViewClassPermission(
        GET=all_permissions.projects_view,
    )

    def get(self, request, action_id):
        pk = int_from_request(request.GET, 'project', 0)
        project = get_visible_project_or_404(request, pk)

        form = get_action_form(action_id, project, request.user)
        return Response(form)
