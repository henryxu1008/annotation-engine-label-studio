"""Fence raw-ID task resources for participants in blind-sensitive projects.

These classes are installed only on legacy URL patterns. The handle-addressed
adapters continue to reuse the original view implementations directly.
"""
from access_control.identity import resolve_principal
from access_control.authorization import authorization
from access_control.project_operations import lock_project_actor
from classification_disagreements.blind_participant_projection import sensitive_nonmanager
from django.db import transaction
from projects.models import Project
from rest_framework.exceptions import NotFound, PermissionDenied

from . import api
from .models import Annotation, AnnotationDraft, Task


class LegacyParticipantFence:
    resource_model = Task
    project_paths = ('project',)

    @transaction.atomic
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def handle_exception(self, exc):
        transaction.set_rollback(True)
        return super().handle_exception(exc)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        resource = self.resource_model.objects.select_related(*self.project_paths).filter(pk=kwargs['pk']).first()
        if resource is None:
            raise NotFound('Work is unavailable.')
        projects = [resource.task.project if path == 'task__project' and resource.task_id else
                    resource.project if path == 'project' else None for path in self.project_paths]
        projects = [project for project in projects if project is not None]
        principal = resolve_principal(request)
        project_ids = {project.pk for project in projects}
        if not project_ids:
            raise NotFound('Work is unavailable.')
        # Quality admission takes this same project lock. Hold it until the
        # response is assembled even when the project is currently ordinary.
        locked = list(Project.all_objects.select_for_update().filter(pk__in=project_ids).order_by('pk'))
        if len(locked) != len(project_ids):
            raise NotFound('Work is unavailable.')
        locked = [lock_project_actor(project.pk, principal)[0] for project in locked]
        if any(sensitive_nonmanager(project, principal) for project in locked):
            raise NotFound('Work is unavailable.')
        if any(not authorization.can_view_project(principal, project) for project in locked):
            if self.resource_model in (Annotation, AnnotationDraft):
                raise PermissionDenied('Work is unavailable.')
            raise NotFound('Work is unavailable.')
        current = self.resource_model.objects.select_related(*self.project_paths).filter(pk=kwargs['pk']).first()
        if current is None:
            raise NotFound('Work is unavailable.')
        current_projects = {current.task.project_id if path == 'task__project' and current.task_id else
                            current.project_id if path == 'project' else None for path in self.project_paths}
        if None in current_projects or current_projects != project_ids:
            raise NotFound('Work is unavailable.')
        # TaskAPI caches its object in initial(); discard the pre-lock view.
        if isinstance(self, api.TaskAPI):
            self.task = self.get_object()


class LegacyTaskAPI(LegacyParticipantFence, api.TaskAPI):
    pass


class LegacyAnnotationsListAPI(LegacyParticipantFence, api.AnnotationsListAPI):
    pass


class LegacyDraftListAPI(LegacyParticipantFence, api.AnnotationDraftListAPI):
    pass


class LegacyTaskAgreementAPI(LegacyParticipantFence, api.TaskAgreementAPI):
    pass


class LegacyAnnotationAPI(LegacyParticipantFence, api.AnnotationAPI):
    resource_model = Annotation
    project_paths = ('project', 'task__project')


class LegacyAnnotationConvertAPI(LegacyParticipantFence, api.AnnotationConvertAPI):
    resource_model = Annotation
    project_paths = ('project', 'task__project')


class LegacyDraftAPI(LegacyParticipantFence, api.AnnotationDraftAPI):
    resource_model = AnnotationDraft
    project_paths = ('task__project',)
