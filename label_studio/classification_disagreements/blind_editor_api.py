"""Narrow participant read contract for an already-issued blind work handle.

Dispatch and the general editor remain disconnected until all old routes and
side channels have been addressed. This endpoint never creates a mix slot.
"""

import hashlib
import hmac

from access_control.identity import resolve_actor, resolve_principal
from access_control.project_operations import EDIT, READ_WORK, lock_project_operation, project_operation_policy
from core.permissions import all_permissions
from django.conf import settings
from django.db import connection, transaction
from django.db.models import BinaryField, Func, IntegerField, TextField, Value
from django.db.models.functions import Cast, Concat, Length, MD5
from projects.models import Project
from rest_framework.exceptions import NotFound
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.views import APIView
from tasks.models import Annotation, AnnotationDraft, Task, TaskAssignment
from tasks.my_work import visible_annotation_assignments

from .blind_participant_projection import sensitive_nonmanager
from .blind_provenance import lock_capture_epoch_for_read
from .blind_resource_handles import _issue_for_locked_assignment
from .blind_task_handles import _lock_bounded_task, issue_handle, validate_locked_handle
from .gold_api import MAX_BYTES, bounded_query
from .models import BlindTaskHandle


def participant_text_data(project, task):
    """Return only configured text inputs, never unrelated source metadata."""
    from . import service

    try:
        if (not isinstance(project.label_config, str) or len(project.label_config) > MAX_BYTES
                or len(project.label_config.encode('utf-8')) > MAX_BYTES):
            raise service.Incomparable('missing_or_malformed')
        service.controls(project.label_config)
        root = service.etree.fromstring(project.label_config)
        fields = set()
        for node in root.iter('Text'):
            attributes = {key.lower(): value for key, value in node.attrib.items()}
            value = attributes.get('value', '')
            if value.startswith('$'):
                fields.add(value[1:])
        if (not isinstance(task.data, dict) or not fields or '' in fields
                or any(not isinstance(task.data.get(field), str) for field in fields)):
            raise service.Incomparable('missing_or_malformed')
        return {field: task.data[field] for field in sorted(fields)}
    except (service.Incomparable, TypeError, ValueError, service.etree.ParseError):
        raise NotFound('Work is unavailable.')


class BlindWorkListAPI(APIView):
    permission_required = all_permissions.tasks_view

    @transaction.atomic
    def get(self, request, pk):
        principal, actor = resolve_actor(request)
        candidate = generics.get_object_or_404(Project.objects.for_user(actor), pk=pk)
        project, actor = lock_project_operation(candidate.pk, principal, READ_WORK)
        if not sensitive_nonmanager(project, principal):
            raise NotFound('Work is unavailable.')
        lock_capture_epoch_for_read()
        raw_page = request.query_params.get('page', '1')
        if (set(request.query_params) - {'page'} or len(request.query_params.getlist('page')) > 1
                or not raw_page.isascii() or not raw_page.isdecimal() or len(raw_page) > 4
                or not 1 <= int(raw_page) <= 1000):
            raise NotFound('Work is unavailable.')
        page, limit = int(raw_page), 50
        seed = hmac.new(str(settings.SECRET_KEY).encode(),
                        f'blind-work-v1:{project.pk}:{actor.pk}'.encode(), hashlib.sha256).hexdigest()
        # Use ORM expressions so joins and subquery aliases remain correct on
        # both supported databases. Measure encoded bytes before JSON is read.
        size = (Func(Cast('task__data', TextField()), function='OCTET_LENGTH', output_field=IntegerField())
                if connection.vendor == 'postgresql' else Length(Cast('task__data', BinaryField())))
        work = visible_annotation_assignments(project, actor).annotate(
            _blind_data_bytes=size,
        ).filter(_blind_data_bytes__gte=0, _blind_data_bytes__lte=MAX_BYTES).order_by(
            MD5(Concat(Value(seed), Cast('task_id', TextField()), output_field=TextField())),
        )
        rows = list(work[(page - 1) * limit:page * limit + 1])
        result = []
        can_label = project_operation_policy(project).allows(EDIT)
        for assignment in rows[:limit]:
            handle = issue_handle(principal=principal, assignment_id=assignment.pk)
            # issue_handle has just checked the authenticated actor and locked
            # the exact project, task and assignment in this transaction.
            current = TaskAssignment.objects.select_related('task').filter(
                pk=handle.assignment_id_frozen, task_id=handle.task_id_frozen, project=project,
                assignee_id=actor.pk, status__in=TaskAssignment.ACTIVE_STATUSES,
            ).first()
            if current is None:
                raise NotFound('Work is unavailable.')
            result.append({'id': handle.task_handle, 'task_id': handle.task_handle, 'project': project.pk,
                           'data': participant_text_data(project, current.task),
                           'can_label': can_label,
                           'assignment_id': handle.assignment_handle,
                           'assignment_version': current.version,
                           'read_url': f'/api/tasks/blind/{handle.task_handle}/'})
        response = Response({'results': result, 'tasks': result, 'total': work.count(),
                             'page': page, 'has_next': len(rows) > limit})
        response['Cache-Control'] = 'private, no-store'
        return response


class BlindEditorReadAPI(APIView):
    permission_required = all_permissions.tasks_view

    @transaction.atomic
    def get(self, request, pk):
        principal = resolve_principal(request)
        # This immutable ledger entry is only a lookup hint, not authority.
        # Resolve live access after both the project and capture locks are held.
        record = BlindTaskHandle.objects.filter(task_handle=pk, actor_id_frozen=principal.local_user_id).first()
        if record is None:
            raise NotFound('Work is unavailable.')
        project, _ = lock_project_operation(record.project_id_frozen, principal, READ_WORK)
        if not sensitive_nonmanager(project, principal):
            raise NotFound('Work is unavailable.')
        lock_capture_epoch_for_read()
        # Keep the same task and assignment locks previously obtained by
        # issue_handle; the ledger is not authority and may point to stale work.
        task_locked = _lock_bounded_task(task_id=record.task_id_frozen, project_id=project.pk)
        assignment_locked = TaskAssignment.objects.select_for_update().filter(
            pk=record.assignment_id_frozen, task=task_locked, project=project,
            assignee_id=principal.local_user_id, status__in=TaskAssignment.ACTIVE_STATUSES,
        ).first()
        if assignment_locked is None:
            raise NotFound('Work is unavailable.')
        # Reuse the exact locked and byte-bounded task instead of re-fetching
        # the immutable handle and its task through a separate queryset.
        task_locked.project = project
        assignment_locked.task, assignment_locked.project = task_locked, project
        assignment = validate_locked_handle(principal=principal, record=record, assignment=assignment_locked)
        task = task_locked
        annotation = None
        if assignment.annotation_id:
            annotation = bounded_query(Annotation.objects.select_for_update(), 'result').filter(
                pk=assignment.annotation_id, task=task, project=project, completed_by_id=principal.local_user_id,
            ).first()
            if (annotation is None or type(annotation._gold_bytes) is not int
                    or not 0 <= annotation._gold_bytes <= MAX_BYTES):
                raise NotFound('Work is unavailable.')
        drafts = list(bounded_query(AnnotationDraft.objects.select_for_update(), 'result').filter(
            task=task, assignment=assignment, user_id=principal.local_user_id,
        ).order_by('pk')[:2])
        if len(drafts) > 1 or any(d.annotation_id not in (None, assignment.annotation_id)
                                  or type(d._gold_bytes) is not int or not 0 <= d._gold_bytes <= MAX_BYTES
                                  for d in drafts):
            raise NotFound('Work is unavailable.')
        annotations = []
        if annotation:
            handle = _issue_for_locked_assignment(principal=principal, assignment=assignment_locked,
                                                  kind='annotation', object_id=annotation.pk)
            annotations.append({'id': handle.handle, 'task': record.task_handle, 'result': annotation.result,
                                'completed_by': principal.local_user_id, 'lead_time': annotation.lead_time,
                                'was_cancelled': annotation.was_cancelled})
        result_drafts = []
        for draft in drafts:
            handle = _issue_for_locked_assignment(principal=principal, assignment=assignment_locked,
                                                  kind='draft', object_id=draft.pk)
            result_drafts.append({'id': handle.handle, 'task': record.task_handle,
                                  'annotation': annotations[0]['id'] if draft.annotation_id and annotations else None,
                                  'result': draft.result, 'revision': draft.revision,
                                  'lead_time': draft.lead_time, 'was_postponed': draft.was_postponed})
        response = Response({'id': record.task_handle, 'project': project.pk, 'data': participant_text_data(project, task),
                             'assignment_id': record.assignment_handle, 'assignment_version': assignment.version,
                             'annotations': annotations, 'drafts': result_drafts, 'predictions': [],
                             # resolve_handle already verifies the current actor,
                             # project role and active assignment under the locks.
                             'can_label': project_operation_policy(project).allows(EDIT),
                             'default_selected_annotation': annotations[0]['id'] if annotations else None})
        response['Cache-Control'] = 'private, no-store'
        return response
