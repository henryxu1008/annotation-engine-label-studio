import logging

from access_control import authorization
from access_control.identity import resolve_actor, resolve_principal
from access_control.project_operations import MANAGE_TASKS, lifecycle_background_fence_enabled, lock_project_operation
from core.permissions import ViewClassPermission, all_permissions
from django.conf import settings
from django.db import transaction
from django.db.models import CharField, Count, Q
from django.db.models.functions import Cast
from django.utils.decorators import method_decorator
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from labels_manager.serializers import (
    LabelBulkUpdateSerializer,
    LabelCreateSerializer,
    LabelLinkSerializer,
    LabelSerializer,
)
from rest_framework import views, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from webhooks.utils import api_webhook, api_webhook_for_delete

from .functions import bulk_update_label
from .models import Label, LabelLink

logger = logging.getLogger(__name__)


class BlindLabelExposureFence:
    """Capture global label metadata exposure before serving any response."""

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        from classification_disagreements.blind_provenance import record_label_metadata_access

        _, actor = resolve_actor(request)
        record_label_metadata_access(account_id=actor.pk)


class LifecycleLabelWriteFence:
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and lifecycle_background_fence_enabled():
            enrolled = getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False) and (
                (
                    isinstance(self, LabelLinkAPI)
                    and getattr(self, 'action', None) in ('update', 'partial_update', 'destroy')
                )
                or (isinstance(self, LabelAPI) and getattr(self, 'action', None) == 'create')
            )
            if not enrolled:
                raise PermissionDenied('Label changes are unavailable while lifecycle admission is enabled.')


@method_decorator(
    name='create',
    decorator=extend_schema(
        tags=['Labels'],
        summary='Create labels',
        description='Add labels to your project without updating the labeling configuration.',
        extensions={
            'x-fern-sdk-group-name': 'labels',
            'x-fern-sdk-method-name': 'create',
            'x-fern-audiences': ['internal'],
        },
    ),
)
@method_decorator(
    name='destroy',
    decorator=extend_schema(
        tags=['Labels'],
        summary='Remove labels',
        description='Remove labels from your project without updating the labeling configuration.',
        extensions={
            'x-fern-sdk-group-name': 'labels',
            'x-fern-sdk-method-name': 'delete',
            'x-fern-audiences': ['internal'],
        },
    ),
)
@method_decorator(
    name='partial_update',
    decorator=extend_schema(
        tags=['Labels'],
        summary='Update labels',
        description='Update labels used for your project without updating the labeling configuration.',
        extensions={
            'x-fern-sdk-group-name': 'labels',
            'x-fern-sdk-method-name': 'update',
            'x-fern-audiences': ['internal'],
        },
    ),
)
@method_decorator(
    name='retrieve',
    decorator=extend_schema(
        tags=['Labels'],
        summary='Get label',
        description="""
        Retrieve a specific custom label used for your project by its ID.
        """,
        extensions={
            'x-fern-sdk-group-name': 'labels',
            'x-fern-sdk-method-name': 'get',
            'x-fern-audiences': ['internal'],
        },
    ),
)
@method_decorator(
    name='list',
    decorator=extend_schema(
        tags=['Labels'],
        summary='List labels',
        description='List all custom labels added to your project separately from the labeling configuration.',
        extensions={
            'x-fern-sdk-group-name': 'labels',
            'x-fern-sdk-method-name': 'list',
            'x-fern-audiences': ['internal'],
        },
    ),
)
@method_decorator(name='update', decorator=extend_schema(exclude=True))
class LabelAPI(BlindLabelExposureFence, LifecycleLabelWriteFence, viewsets.ModelViewSet):
    pagination_class = PageNumberPagination
    serializer_class = LabelSerializer
    permission_required = ViewClassPermission(
        GET=all_permissions.labels_view,
        POST=all_permissions.labels_create,
        PATCH=all_permissions.labels_change,
        DELETE=all_permissions.labels_delete,
    )

    def get_serializer(self, *args, **kwargs):
        """POST request is bulk by default"""
        if self.action == 'create':
            kwargs['many'] = True
        return super().get_serializer(*args, **kwargs)

    def create(self, request, *args, **kwargs):
        if not getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
            return super().create(request, *args, **kwargs)
        with transaction.atomic():
            return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        if getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
            items = serializer.validated_data
            if not items:
                raise ValidationError('At least one label is required.')
            project_id = items[0]['project'].pk
            if any(item['project'].pk != project_id for item in items):
                raise ValidationError('Creating labels across projects is unavailable.')
            principal = resolve_principal(self.request)
            project, actor = lock_project_operation(project_id, principal, MANAGE_TASKS)
            authorization.require(
                authorization.can_manage_project(principal, project), 'Project manager role is required.'
            )
            for item in items:
                item['project'] = project
            serializer.save(created_by=actor, organization=project.organization)
        else:
            serializer.save(created_by=self.request.user, organization=self.request.user.active_organization)

    def get_queryset(self):
        return Label.objects.filter(organization=self.request.user.active_organization).prefetch_related('links')

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return LabelCreateSerializer

        return self.serializer_class


@method_decorator(
    name='create',
    decorator=extend_schema(
        tags=['Labels'],
        summary='Create label links',
        description='Create label links to link new custom labels to your project labeling configuration.',
        extensions={
            'x-fern-sdk-group-name': ['projects', 'labels'],
            'x-fern-sdk-method-name': 'create',
            'x-fern-audiences': ['internal'],
        },
    ),
)
@method_decorator(
    name='destroy',
    decorator=extend_schema(
        tags=['Labels'],
        summary='Remove label link',
        description="""
        Remove a label link that links custom labels to your project labeling configuration. If you remove a label link,
        the label stops being available for the project it was linked to. You can add a new label link at any time. 
        """,
        extensions={
            'x-fern-sdk-group-name': ['projects', 'labels'],
            'x-fern-sdk-method-name': 'delete',
            'x-fern-audiences': ['internal'],
        },
    ),
)
@method_decorator(
    name='partial_update',
    decorator=extend_schema(
        tags=['Labels'],
        summary='Update label link',
        description="""
        Update a label link that links custom labels to a project labeling configuration, for example if the fromName,  
        toName, or name parameters for a tag in the labeling configuration change. 
        """,
        extensions={
            'x-fern-sdk-group-name': ['projects', 'labels'],
            'x-fern-sdk-method-name': 'update',
            'x-fern-audiences': ['internal'],
        },
    ),
)
@method_decorator(
    name='retrieve',
    decorator=extend_schema(
        tags=['Labels'],
        summary='Get label link',
        description='Get label links for a specific project configuration. ',
        extensions={
            'x-fern-sdk-group-name': ['projects', 'labels'],
            'x-fern-sdk-method-name': 'get',
            'x-fern-audiences': ['internal'],
        },
    ),
)
@method_decorator(
    name='list',
    decorator=extend_schema(
        tags=['Labels'],
        summary='List label links',
        description='List label links for a specific label and project.',
        extensions={
            'x-fern-sdk-group-name': ['projects', 'labels'],
            'x-fern-sdk-method-name': 'list',
            'x-fern-audiences': ['internal'],
        },
    ),
)
@method_decorator(name='update', decorator=extend_schema(exclude=True))
class LabelLinkAPI(BlindLabelExposureFence, LifecycleLabelWriteFence, viewsets.ModelViewSet):
    filter_backends = [DjangoFilterBackend]
    filterset_fields = {
        'project': ['exact'],
        'label__created_at': ['exact', 'gte', 'lte'],
        'label__created_by': ['exact'],
    }
    pagination_class = PageNumberPagination
    serializer_class = LabelLinkSerializer
    permission_required = ViewClassPermission(
        GET=all_permissions.labels_view,
        POST=all_permissions.labels_create,
        PATCH=all_permissions.labels_change,
        DELETE=all_permissions.labels_delete,
    )

    def get_queryset(self):
        return LabelLink.objects.filter(label__organization=self.request.user.active_organization).annotate(
            annotations_count=Count(
                'project__tasks__annotations',
                filter=Q(
                    project__tasks__annotations__result__icontains=Cast('label__value', output_field=CharField())
                ),
            )
        )

    def _lock_enrolled_link(self, request):
        link = self.get_object()
        principal = resolve_principal(request)
        project, _ = lock_project_operation(link.project_id, principal, MANAGE_TASKS)
        authorization.require(
            authorization.can_manage_project(principal, project), 'Project manager role is required.'
        )
        locked = LabelLink.objects.select_for_update().filter(pk=link.pk, project=project).first()
        if locked is None:
            raise ValidationError('The label link changed project; retry the request.')
        return locked

    def perform_update(self, serializer):
        if getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
            link = serializer.instance
            for field in ('project', 'label'):
                value = serializer.validated_data.get(field)
                if value is not None and value.pk != getattr(link, f'{field}_id'):
                    raise ValidationError('Changing a label link project or label is unavailable during admission.')
        super().perform_update(serializer)
        if getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
            from webhooks.durable import queue_project_event
            from webhooks.models import WebhookAction

            queue_project_event(serializer.instance.project, WebhookAction.LABEL_LINK_UPDATED, serializer.instance)

    def perform_destroy(self, instance):
        if getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
            from webhooks.durable import queue_project_event
            from webhooks.models import WebhookAction

            queue_project_event(instance.project, WebhookAction.LABEL_LINK_DELETED, {'id': instance.pk})
        super().perform_destroy(instance)

    @api_webhook('LABEL_LINK_UPDATED')
    def update(self, request, *args, **kwargs):
        if not getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
            return super().update(request, *args, **kwargs)
        with transaction.atomic():
            self._lock_enrolled_link(request)
            return super().update(request, *args, **kwargs)

    @api_webhook_for_delete('LABEL_LINK_DELETED')
    def destroy(self, request, *args, **kwargs):
        if not getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
            return super().destroy(request, *args, **kwargs)
        with transaction.atomic():
            self._lock_enrolled_link(request)
            return super().destroy(request, *args, **kwargs)


@method_decorator(
    name='post',
    decorator=extend_schema(
        tags=['Labels'],
        summary='Bulk update labels',
        description="""
        If you want to update the labels in saved annotations, use this endpoint.
        """,
        extensions={
            'x-fern-sdk-group-name': ['projects', 'labels'],
            'x-fern-sdk-method-name': 'update_many',
            'x-fern-audiences': ['internal'],
        },
    ),
)
class LabelBulkUpdateAPI(BlindLabelExposureFence, LifecycleLabelWriteFence, views.APIView):
    permission_required = all_permissions.labels_change

    @transaction.atomic
    def post(self, request):
        from access_control.authorization import authorization
        from access_control.project_operations import EDIT, lock_project_operation
        from projects.models import Project

        serializer = LabelBulkUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = serializer.validated_data['project']
        principal, actor = resolve_actor(request)
        if project is not None and project.organization_id != actor.active_organization_id:
            from rest_framework.exceptions import NotFound

            raise NotFound('Project is unavailable.')
        project_ids = ([project.pk] if project is not None else list(Project.objects.filter(
            organization_id=actor.active_organization_id,
        ).order_by('pk').values_list('pk', flat=True)))
        for project_id in project_ids:
            locked, _ = lock_project_operation(project_id, principal, EDIT)
            authorization.require(authorization.can_manage_project(principal, locked),
                                  'Project manager role is required to update labels.')

        updated_count = bulk_update_label(
            old_label=serializer.validated_data['old_label'],
            new_label=serializer.validated_data['new_label'],
            organization=self.request.user.active_organization,
            project=project,
            allowed_project_ids=project_ids,
        )
        return Response({'annotations_updated': updated_count})
