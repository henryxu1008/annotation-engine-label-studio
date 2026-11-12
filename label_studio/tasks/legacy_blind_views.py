"""Fence raw-ID task resources for participants in blind-sensitive projects.

These classes are installed only on legacy URL patterns. The handle-addressed
adapters continue to reuse the original view implementations directly.
"""
from access_control.identity import resolve_principal
from classification_disagreements.blind_participant_projection import sensitive_nonmanager
from rest_framework.exceptions import NotFound

from . import api
from .models import Annotation, AnnotationDraft, Task


class LegacyParticipantFence:
    resource_model = Task
    project_paths = ('project',)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        resource = self.resource_model.objects.select_related(*self.project_paths).filter(pk=kwargs['pk']).first()
        if resource is None:
            raise NotFound('Work is unavailable.')
        projects = [resource.task.project if path == 'task__project' and resource.task_id else
                    resource.project if path == 'project' else None for path in self.project_paths]
        projects = [project for project in projects if project is not None]
        principal = resolve_principal(request)
        if not projects or any(sensitive_nonmanager(project, principal) for project in projects):
            raise NotFound('Work is unavailable.')


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
