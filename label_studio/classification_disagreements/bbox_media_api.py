"""Opt-in managed image preparation and authenticated fixed-image reads."""

from access_control.authorization import authorization
from access_control.identity import resolve_actor
from access_control.project_operations import READ_WORK, require_project_operation
from core.permissions import all_permissions
from core.utils.private_file import PrivateFileError
from django.conf import settings
from django.db import connection
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from projects.models import Project
from rest_framework import generics
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.response import Response
from task_packages.scope import PackageScope
from tasks.models import Task

from . import bbox, bbox_prepare, bbox_storage
from .models import BboxMediaPin


def context(request, pk):
    principal, actor = resolve_actor(request)
    project = get_object_or_404(Project.objects.for_user(actor), pk=pk)
    modes = settings.ANNOTATION_BBOX_MEDIA_PROJECTS
    if (connection.vendor not in ('postgresql', 'sqlite') or not settings.ANNOTATION_BBOX_MEDIA_ENABLED
            or not isinstance(modes, dict)
            or modes.get(f'{project.organization_id}:{project.pk}') != 'managed'):
        raise NotFound()
    require_project_operation(project, READ_WORK)
    return principal, actor, project


class PrepareAPI(generics.GenericAPIView):
    permission_required = all_permissions.tasks_change

    def post(self, request, pk, task_id):
        if not isinstance(request.data, dict) or request.data or request.query_params:
            raise ValidationError('Unexpected input.')
        principal, _, project = context(request, pk)
        authorization.require(authorization.can_manage_project(principal, project))
        task = get_object_or_404(Task.objects.filter(project=project), pk=task_id)
        try:
            pin = bbox_prepare.prepare_managed(task, principal)
        except bbox.Incomparable as exc:
            raise ValidationError({'detail': 'Image source is unavailable or unsupported.'}) from exc
        except bbox.Error as exc:
            error = APIException('Image capture failed.')
            error.status_code = exc.status
            raise error from exc
        response = Response({'task_id': task.pk, 'media_token': str(pin.token)}, status=201)
        response['Cache-Control'] = 'private, no-store'
        return response


class MediaAPI(generics.GenericAPIView):
    permission_required = all_permissions.tasks_view

    def get(self, request, pk, token):
        if request.query_params:
            raise NotFound()
        principal, actor, project = context(request, pk)
        pin = get_object_or_404(BboxMediaPin.objects.select_related('task'), project=project, token=token)
        task = pin.task
        if task.project_id != project.pk:
            raise NotFound()
        allowed = authorization.can_manage_project(principal, project) or authorization.can_label_task(principal, task)
        authorization.require(allowed)
        scope = PackageScope.from_request(request, project)
        if scope:
            scope.require_task(task)
        try:
            if (pin.artifact_key != str(pin.token) or not 0 < pin.size_bytes <= bbox.MAX_IMAGE_BYTES
                    or not 0 < pin.width * pin.height <= bbox.MAX_IMAGE_PIXELS
                    or bbox.digest(task.data) != pin.task_data_hash
                    or bbox.digest(project.label_config) != pin.config_hash):
                raise PrivateFileError()
            with bbox_storage.open_verified(pin.token, pin.storage_binding, pin.sha256, pin.size_bytes) as reader:
                data = reader.read(pin.size_bytes + 1)
            image = bbox.image_identity(data)
            if image != {'sha256': pin.sha256, 'width': pin.width, 'height': pin.height}:
                raise PrivateFileError()
        except (PrivateFileError, bbox.Incomparable, bbox.Error, APIException, OSError, ValueError) as exc:
            raise NotFound() from exc
        # Re-read authority and source after bounded I/O, before releasing bytes.
        current_principal, current_actor, current_project = context(request, pk)
        current_task = get_object_or_404(Task.objects.filter(project=current_project), pk=task.pk)
        if (current_actor.pk != actor.pk or current_principal != principal
                or not (authorization.can_manage_project(principal, current_project)
                        or authorization.can_label_task(principal, current_task))
                or bbox.digest(current_task.data) != pin.task_data_hash
                or bbox.digest(current_project.label_config) != pin.config_hash):
            raise NotFound()
        current_scope = PackageScope.from_request(request, current_project)
        if current_scope:
            current_scope.require_task(current_task)
        content_type = 'image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg'
        response = HttpResponse(data, content_type=content_type)
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        return response
