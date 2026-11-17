"""This file and its contents are licensed under the Apache License 2.0. Please see the included NOTICE for copyright information and LICENSE for a copy of the license.
"""
import inspect
import logging
import os
import time

from access_control.identity import resolve_principal
from access_control.project_access import (
    get_managed_project_or_404,
    managed_projects_for_user,
    require_project_manager,
)
from access_control.project_operations import SYNC_STORAGE, lock_project_operation
from core.permissions import ViewClassPermission, all_permissions
from core.utils.io import read_yaml
from django.conf import settings
from django.db import connection, transaction
from drf_spectacular.utils import extend_schema
from io_storages.admission import LEGACY_STORAGE_SETTLED_STATUSES, bind_queued_storage_status
from io_storages.admission_models import StorageOperation
from io_storages.serializers import ExportStorageSerializer, ImportStorageSerializer
from projects.models import Project
from rest_framework import generics, serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

logger = logging.getLogger(__name__)


def _run_export_storage_auto_sync(storage_class, storage_id):
    try:
        storage = storage_class.objects.get(pk=storage_id)
        storage.sync()
    except Exception:
        logger.exception(f'Post-commit export storage auto-sync failed for storage {storage_id}')
        try:
            storage = storage_class.objects.filter(pk=storage_id).first()
            if storage is not None:
                storage.info_set_failed()
        except Exception:
            logger.exception(f'Failed to mark export storage {storage_id} as failed after auto-sync error')



def _validated_project_id(request):
    if not hasattr(request.data, 'get'):
        raise ValidationError('Request body must be a JSON object.')
    project_id = request.data.get('project')
    if project_id is None:
        return None
    return serializers.IntegerField(min_value=1).run_validation(project_id)


class ImportStorageListAPI(generics.ListCreateAPIView):
    permission_required = ViewClassPermission(
        GET=all_permissions.storages_view,
        POST=all_permissions.storages_change,
    )
    parser_classes = (JSONParser, FormParser, MultiPartParser)

    serializer_class = ImportStorageSerializer

    def create(self, request, *args, **kwargs):
        project_id = _validated_project_id(request)
        if project_id is not None:
            project = generics.get_object_or_404(Project.objects.for_user(request.user), pk=project_id)
            require_project_manager(request, project)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        project = serializer.validated_data.get('project')
        with transaction.atomic():
            project, _ = lock_project_operation(project.pk, resolve_principal(self.request), SYNC_STORAGE)
            require_project_manager(self.request, project)
            serializer.save(project=project)

    def get_queryset(self):
        project_pk = self.request.query_params.get('project')
        if not project_pk:
            raise ValidationError('query parameter "project" is required')

        project = get_managed_project_or_404(self.request, project_pk)
        StorageClass = self.serializer_class.Meta.model
        storages = StorageClass.objects.filter(project_id=project.id)

        # check failed jobs and sync their statuses
        StorageClass.ensure_storage_statuses(storages)
        return storages


class _StorageMutationFence:
    """Keep storage configuration stable while an admitted effect is pending."""

    def _locked_storage(self, instance):
        if connection.vendor != 'postgresql':
            raise ValidationError('Storage admission requires PostgreSQL row locks.')
        principal = resolve_principal(self.request)
        project, _ = lock_project_operation(instance.project_id, principal, SYNC_STORAGE)
        require_project_manager(self.request, project)
        storage = generics.get_object_or_404(type(instance).objects.select_for_update(), pk=instance.pk, project=project)
        if storage.status not in LEGACY_STORAGE_SETTLED_STATUSES:
            raise ValidationError({'reason': 'LEGACY_STORAGE_NOT_DRAINED'})
        if StorageOperation.objects.filter(
            project=project, storage_model=storage._meta.label_lower, storage_id=storage.pk,
        ).exclude(state__in=(
            StorageOperation.State.COMPLETED, StorageOperation.State.CANCELLED, StorageOperation.State.RECONCILED,
        )).exists():
            raise ValidationError({'reason': 'STORAGE_OPERATION_PENDING'})
        return storage

    def perform_update(self, serializer):
        if not getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
            return super().perform_update(serializer)
        instance = serializer.instance
        requested_project = serializer.validated_data.get('project')
        if requested_project is not None and requested_project.pk != instance.project_id:
            raise ValidationError('Moving storage to another project is not supported.')
        with transaction.atomic():
            serializer.instance = self._locked_storage(instance)
            serializer.save()

    def perform_destroy(self, instance):
        if not getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
            return super().perform_destroy(instance)
        with transaction.atomic():
            self._locked_storage(instance).delete()


class ImportStorageDetailAPI(_StorageMutationFence, generics.RetrieveUpdateDestroyAPIView):
    """RUD storage by pk specified in URL"""

    permission_required = ViewClassPermission(
        GET=all_permissions.storages_view,
        PATCH=all_permissions.storages_change,
        PUT=all_permissions.storages_change,
        DELETE=all_permissions.storages_change,
    )
    parser_classes = (JSONParser, FormParser, MultiPartParser)
    serializer_class = ImportStorageSerializer

    def get_queryset(self):
        StorageClass = self.serializer_class.Meta.model
        return StorageClass.objects.filter(project__in=managed_projects_for_user(self.request))

    @extend_schema(exclude=True)
    def put(self, request, *args, **kwargs):
        return super(ImportStorageDetailAPI, self).put(request, *args, **kwargs)


class ExportStorageListAPI(generics.ListCreateAPIView):

    permission_required = ViewClassPermission(
        GET=all_permissions.storages_view,
        POST=all_permissions.storages_change,
    )
    parser_classes = (JSONParser, FormParser, MultiPartParser)
    serializer_class = ExportStorageSerializer

    def create(self, request, *args, **kwargs):
        project_id = _validated_project_id(request)
        if project_id is not None:
            project = generics.get_object_or_404(Project.objects.for_user(request.user), pk=project_id)
            require_project_manager(request, project)
        return super().create(request, *args, **kwargs)

    def get_queryset(self):
        project_pk = self.request.query_params.get('project')
        if not project_pk:
            raise ValidationError('query parameter "project" is required')

        project = get_managed_project_or_404(self.request, project_pk)
        StorageClass = self.serializer_class.Meta.model
        storages = StorageClass.objects.filter(project_id=project.id)

        # check failed jobs and sync their statuses
        StorageClass.ensure_storage_statuses(storages)
        return storages

    def perform_create(self, serializer):
        project = serializer.validated_data.get('project')
        require_project_manager(self.request, project)
        # double check: not export storages don't validate connection in serializer,
        # just make another explicit check here, note: in this create API we have credentials in request.data
        instance = serializer.Meta.model(**serializer.validated_data)
        instance.require_mutable_delivery_allowed()
        try:
            instance.validate_connection()
        except Exception as exc:
            raise ValidationError(exc)

        with transaction.atomic():
            project, _ = lock_project_operation(project.pk, resolve_principal(self.request), SYNC_STORAGE)
            require_project_manager(self.request, project)
            instance.require_mutable_delivery_allowed()
            storage = serializer.save(project=project)
            if settings.SYNC_ON_TARGET_STORAGE_CREATION:
                if getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
                    if connection.vendor != 'postgresql':
                        raise ValidationError('Storage admission requires PostgreSQL row locks.')
                    if not storage.info_set_queued():
                        raise ValidationError('Unable to queue initial storage synchronization.')
                    record = StorageOperation.objects.create(
                        project=project, storage_model=storage._meta.label_lower, storage_id=storage.pk,
                        operation='export_sync', lifecycle_revision=project.lifecycle_revision,
                        state=StorageOperation.State.QUEUED,
                    )
                    bind_queued_storage_status(storage, record)
                    from io_storages.dispatch_recovery import redispatch_queued_operation

                    transaction.on_commit(lambda pk=record.pk: redispatch_queued_operation(pk, expected_attempt=0), robust=True)
                else:
                    storage_id = storage.id
                    storage_class = storage.__class__
                    transaction.on_commit(
                        lambda storage_class=storage_class, storage_id=storage_id: _run_export_storage_auto_sync(
                            storage_class, storage_id
                        )
                    )


class ExportStorageDetailAPI(_StorageMutationFence, generics.RetrieveUpdateDestroyAPIView):
    """RUD storage by pk specified in URL"""

    permission_required = ViewClassPermission(
        GET=all_permissions.storages_view,
        PATCH=all_permissions.storages_change,
        PUT=all_permissions.storages_change,
        DELETE=all_permissions.storages_change,
    )
    parser_classes = (JSONParser, FormParser, MultiPartParser)
    serializer_class = ExportStorageSerializer

    def get_queryset(self):
        StorageClass = self.serializer_class.Meta.model
        return StorageClass.objects.filter(project__in=managed_projects_for_user(self.request))

    @extend_schema(exclude=True)
    def put(self, request, *args, **kwargs):
        return super(ExportStorageDetailAPI, self).put(request, *args, **kwargs)


class ImportStorageSyncAPI(generics.GenericAPIView):

    permission_required = ViewClassPermission(
        POST=all_permissions.storages_sync,
    )
    parser_classes = (JSONParser, FormParser, MultiPartParser)
    serializer_class = ImportStorageSerializer

    def get_queryset(self):
        ImportStorageClass = self.serializer_class.Meta.model
        return ImportStorageClass.objects.filter(project__in=managed_projects_for_user(self.request))

    def post(self, request, *args, **kwargs):
        storage = self.get_object()
        # check connectivity & access, raise an exception if not satisfied
        if not storage.synchronizable:
            response_data = {'message': f'Storage {str(storage.id)} is not synchronizable'}
            return Response(status=status.HTTP_400_BAD_REQUEST, data=response_data)
        storage.validate_connection()
        storage.sync()
        storage.refresh_from_db()
        return Response(self.serializer_class(storage).data)


class ExportStorageSyncAPI(generics.GenericAPIView):

    permission_required = ViewClassPermission(
        POST=all_permissions.storages_sync,
    )
    parser_classes = (JSONParser, FormParser, MultiPartParser)
    serializer_class = ExportStorageSerializer

    def get_queryset(self):
        ExportStorageClass = self.serializer_class.Meta.model
        return ExportStorageClass.objects.filter(project__in=managed_projects_for_user(self.request))

    def post(self, request, *args, **kwargs):
        storage = self.get_object()
        # check connectivity & access, raise an exception if not satisfied
        if not storage.synchronizable:
            response_data = {'message': f'Storage {str(storage.id)} is not synchronizable'}
            return Response(status=status.HTTP_400_BAD_REQUEST, data=response_data)
        storage.validate_connection()
        storage.sync()
        storage.refresh_from_db()
        return Response(self.serializer_class(storage).data)


class StorageValidateAPI(generics.CreateAPIView):

    permission_required = all_permissions.storages_change
    parser_classes = (JSONParser, FormParser, MultiPartParser)

    def create(self, request, *args, **kwargs):
        from .functions import validate_storage_instance

        validate_storage_instance(request, self.serializer_class)
        return Response()


@extend_schema(exclude=True)
class ImportStorageListFilesAPI(generics.CreateAPIView):

    permission_required = all_permissions.storages_change
    parser_classes = (JSONParser, FormParser, MultiPartParser)
    serializer_class = None  # Default serializer

    def __init__(self, serializer_class=None, *args, **kwargs):
        self.serializer_class = serializer_class
        super().__init__(*args, **kwargs)

    @extend_schema(exclude=True)
    def create(self, request, *args, **kwargs):
        from .functions import validate_storage_instance

        instance = validate_storage_instance(request, self.serializer_class)
        limit = int(request.data.get('limit', settings.DEFAULT_STORAGE_LIST_LIMIT))

        try:
            files = []
            start_time = time.time()
            timeout_seconds = 30

            for object in instance.iter_objects():
                files.append(instance.get_unified_metadata(object))

                # Check if we've reached the file limit
                if len(files) >= limit:
                    files.append({'key': None, 'last_modified': None, 'size': None})
                    break

                # Check if we've exceeded the timeout
                if time.time() - start_time > timeout_seconds:
                    files.append({'key': '... storage scan timeout reached ...', 'last_modified': None, 'size': None})
                    break

            return Response({'files': files})
        except Exception as exc:
            logger.exception('Error listing storage files: %s', exc)
            raise ValidationError('Failed to list storage files')


@extend_schema(exclude=True)
class StorageFormLayoutAPI(generics.RetrieveAPIView):

    permission_required = all_permissions.storages_change
    parser_classes = (JSONParser, FormParser, MultiPartParser)
    storage_type = None

    @extend_schema(exclude=True)
    def get(self, request, *args, **kwargs):
        form_layout_file = os.path.join(os.path.dirname(inspect.getfile(self.__class__)), 'form_layout.yml')
        if not os.path.exists(form_layout_file):
            raise NotFound(f'"form_layout.yml" is not found for {self.__class__.__name__}')

        form_layout = read_yaml(form_layout_file)
        form_layout = self.post_process_form(form_layout)
        return Response(form_layout[self.storage_type])

    def post_process_form(self, form_layout):
        return form_layout


class ImportStorageValidateAPI(StorageValidateAPI):
    serializer_class = ImportStorageSerializer


class ExportStorageValidateAPI(StorageValidateAPI):
    serializer_class = ExportStorageSerializer


class ImportStorageFormLayoutAPI(StorageFormLayoutAPI):
    storage_type = 'ImportStorage'


class ExportStorageFormLayoutAPI(StorageFormLayoutAPI):
    storage_type = 'ExportStorage'
