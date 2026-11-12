"""Handle-addressed adapters over the existing annotation and draft writers.

These routes share the upstream writers and their transaction, policy and
revision checks. They are not an enablement of blind dispatch: legacy reads,
the editor and assignment workflow still require a coordinated migration.
"""

from collections.abc import Mapping

from access_control.identity import resolve_principal
from django.db import transaction
from rest_framework.exceptions import APIException, NotFound, ValidationError
from tasks.api import AnnotationAPI, AnnotationDraftAPI, AnnotationDraftListAPI, AnnotationsListAPI

from .blind_participant_projection import sensitive_nonmanager
from .blind_resource_handles import issue_resource_handle, resolve_resource_handle
from .blind_task_handles import resolve_handle


class BlindWriteMixin:
    resource_kind = None
    task_parent = False
    suppress_work_webhooks = True
    input_fields = frozenset({
        'result', 'assignment_id', 'assignment_version', 'submit_for_review', 'draft_id', 'draft_revision',
        'was_cancelled', 'lead_time', 'was_postponed', 'started_at', 'unique_id',
        'parent_annotation', 'parent_prediction',
    })

    @transaction.atomic
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def handle_exception(self, exc):
        transaction.set_rollback(True)
        return super().handle_exception(exc)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        from access_control.project_operations import READ_WORK, lock_project_operation
        from tasks.models import TaskAssignment
        from .blind_task_handles import _lock_bounded_task
        from .models import BlindCaptureEpoch, BlindTaskHandle

        self.blind_principal = resolve_principal(request)
        self.blind_task_handle = self.kwargs['task_handle']
        record = BlindTaskHandle.objects.filter(
            task_handle=self.blind_task_handle, actor_id_frozen=self.blind_principal.local_user_id,
        ).first()
        if record is None:
            raise NotFound('Work is unavailable.')
        project, _ = lock_project_operation(record.project_id_frozen, self.blind_principal, READ_WORK)
        if not sensitive_nonmanager(project, self.blind_principal):
            raise NotFound('Work is unavailable.')
        # Writers can later validate quality evidence under an exclusive epoch
        # lock. Acquire that lock now, rather than upgrading a shared read lock
        # while another project's writer does the same.
        BlindCaptureEpoch.objects.select_for_update().filter(pk=1).first()
        task = _lock_bounded_task(project_id=project.pk, task_id=record.task_id_frozen)
        if not TaskAssignment.objects.select_for_update().filter(
                pk=record.assignment_id_frozen, project=project, task=task,
                assignee_id=self.blind_principal.local_user_id, status__in=TaskAssignment.ACTIVE_STATUSES,
        ).exists():
            raise NotFound('Work is unavailable.')
        self.blind_assignment = resolve_handle(principal=self.blind_principal, task_handle=self.blind_task_handle)
        if not sensitive_nonmanager(self.blind_assignment.project, self.blind_principal):
            raise NotFound('Work is unavailable.')
        if self.task_parent:
            self.kwargs['pk'] = self.blind_assignment.task_id
        else:
            obj = self._resolve_resource(self.resource_kind, self.kwargs['resource_handle'])
            self.kwargs['pk'] = obj.pk
        if 'annotation_handle' in self.kwargs:
            obj = self._resolve_resource('annotation', self.kwargs['annotation_handle'])
            self.kwargs['annotation_id'] = obj.pk
        if not isinstance(request.data, Mapping):
            raise ValidationError({'detail': 'Expected a work object.'})
        data = request.data.copy()
        if set(data) - self.input_fields:
            raise ValidationError({'detail': 'Unsupported work fields.'})
        if 'result' in data:
            from .gold_api import MAX_BYTES
            from .partial import bounded_size

            try:
                bounded_size(data['result'], MAX_BYTES)
            except (APIException, TypeError, ValueError, OverflowError):
                raise ValidationError({'detail': 'Work result is unavailable.'})
        if data.get('parent_prediction') is not None or data.get('parent_annotation') is not None:
            raise ValidationError({'detail': 'Parent resources are unavailable.'})
        # The upstream editor uses integer 0 for an annotation that has never
        # had a saved draft. It is not a resource handle; the underlying writer
        # still checks that no live draft exists before accepting a submission.
        if type(data.get('draft_id')) is int and data['draft_id'] == 0:
            data.pop('draft_id')
        elif data.get('draft_id') is not None:
            obj = self._resolve_resource('draft', data['draft_id'])
            data['draft_id'] = obj.pk
        data['task_handle'] = self.blind_task_handle
        # Reuse the existing DRF writers after validating and translating all
        # resource references; serializers and concurrency checks read data.
        request._full_data = data

    def _resolve_resource(self, kind, handle):
        if isinstance(handle, str) and handle.isascii() and handle.isdecimal() and len(handle) <= 16:
            handle = int(handle)
        obj = resolve_resource_handle(principal=self.blind_principal, kind=kind, handle=handle)
        if obj.task_id != self.blind_assignment.task_id:
            raise NotFound('Work is unavailable.')
        if kind == 'annotation' and obj.pk != self.blind_assignment.annotation_id:
            raise NotFound('Work is unavailable.')
        if kind == 'draft' and obj.assignment_id != self.blind_assignment.pk:
            raise NotFound('Work is unavailable.')
        return obj

    def _project_response(self, response, kind):
        if not 200 <= response.status_code < 300 or response.data is None:
            return response
        raw = response.data
        resource = issue_resource_handle(principal=self.blind_principal, assignment_id=self.blind_assignment.pk,
                                         kind=kind, object_id=raw['id'])
        current = resolve_handle(principal=self.blind_principal, task_handle=self.blind_task_handle)
        fields = ('result', 'was_cancelled') if kind == 'annotation' else ('result', 'revision', 'was_postponed')
        response.data = {key: raw[key] for key in fields if key in raw}
        response.data.update({'id': resource.handle, 'task': self.blind_task_handle,
                              'assignment_version': current.version})
        response['Cache-Control'] = 'private, no-store'
        return response


class BlindAnnotationCreateAPI(BlindWriteMixin, AnnotationsListAPI):
    task_parent = True
    http_method_names = ['post']

    def post(self, request, *args, **kwargs):
        return self._project_response(super().post(request, *args, **kwargs), 'annotation')


class BlindAnnotationUpdateAPI(BlindWriteMixin, AnnotationAPI):
    resource_kind = 'annotation'
    http_method_names = ['patch', 'put']

    def patch(self, request, *args, **kwargs):
        return self._project_response(super().patch(request, *args, **kwargs), 'annotation')

    def put(self, request, *args, **kwargs):
        return self._project_response(super().put(request, *args, **kwargs), 'annotation')


class BlindDraftCreateAPI(BlindWriteMixin, AnnotationDraftListAPI):
    task_parent = True
    http_method_names = ['post']

    def post(self, request, *args, **kwargs):
        return self._project_response(super().post(request, *args, **kwargs), 'draft')


class BlindDraftUpdateAPI(BlindWriteMixin, AnnotationDraftAPI):
    resource_kind = 'draft'
    http_method_names = ['patch', 'put', 'delete']

    def patch(self, request, *args, **kwargs):
        return self._project_response(super().patch(request, *args, **kwargs), 'draft')

    def put(self, request, *args, **kwargs):
        return self._project_response(super().put(request, *args, **kwargs), 'draft')
