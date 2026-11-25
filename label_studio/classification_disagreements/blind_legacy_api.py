"""Manager-only repair of retained blind-read evidence; not blind dispatch."""

from access_control.authorization import authorization
from access_control.identity import LOCAL_IDENTITY_SOURCE, LocalIdentityProvider, get_identity_provider
from access_control.project_operations import ASSIGN, READ_WORK, local_principal, lock_project_operation
from core.permissions import all_permissions
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from tasks.models import TaskAssignment
from tasks.exceptions import AssignmentConflictError
from users.models import User

from .blind_provenance import lock_capture_epoch_for_write
from .blind_read_binding import seal_mixed_read_binding, seal_ordinary_read_binding, seal_unmixed_quality_read_binding
from .blind_task_handles import _lock_bounded_task
from .gold_api import guard
from .models import BlindMixDecision, BlindQualityAssignment, BlindReadAttestation, BlindReadBinding


def _require_blind_mode(project, principal):
    configured = settings.ANNOTATION_CLASSIFICATION_BLIND_PROJECTS
    if (connection.vendor not in ('postgresql', 'sqlite') or not settings.ANNOTATION_CLASSIFICATION_BLIND_ENABLED
            or not isinstance(configured, dict) or configured.get(f'{project.organization_id}:{project.pk}') is not True
            or principal.source != LOCAL_IDENTITY_SOURCE or type(get_identity_provider()) is not LocalIdentityProvider):
        raise NotFound()


class BlindLegacyBindingsAPI(APIView):
    """Seal only explicitly selected live assignments after full authorization."""

    permission_required = all_permissions.annotations_view

    @transaction.atomic
    def get(self, request, pk):
        principal, actor, candidate = guard(request, pk)
        _require_blind_mode(candidate, principal)
        if (set(request.query_params) - {'after'} or len(request.query_params.getlist('after')) > 1):
            raise ValidationError({'detail': 'Invalid work selection.'})
        raw = request.query_params.get('after', '0')
        if not raw.isascii() or not raw.isdecimal() or len(raw) > 19 or int(raw) >= 2**63:
            raise ValidationError({'detail': 'Invalid work selection.'})
        project, locked_actor = lock_project_operation(pk, principal, READ_WORK)
        if locked_actor.pk != actor.pk or guard(request, pk)[0] != principal:
            raise NotFound()
        bindings = BlindReadBinding.objects.filter(assignment_id_frozen=OuterRef('pk'))
        attestations = BlindReadAttestation.objects.filter(assignment_id_frozen=OuterRef('pk'))
        rows = list(TaskAssignment.objects.filter(project=project, task__project=project,
            status__in=TaskAssignment.ACTIVE_STATUSES, pk__gt=int(raw))
            .annotate(_has_binding=Exists(bindings), _has_attestation=Exists(attestations))
            .filter(Q(_has_binding=False) | Q(_has_attestation=False)).order_by('pk')
            .values('id', 'task_id', 'assignee_id', 'version', 'assigned_at')[:51])
        response = Response({'items': rows[:50], 'next_after': rows[49]['id'] if len(rows) > 50 else None})
        response['Cache-Control'] = 'private, no-store'
        return response

    @transaction.atomic
    def post(self, request, pk):
        principal, actor, candidate = guard(request, pk)
        if (request.query_params or not isinstance(request.data, dict) or set(request.data) != {'items'}
                or not isinstance(request.data['items'], list) or not 1 <= len(request.data['items']) <= 50):
            raise ValidationError({'detail': 'Invalid work selection.'})
        expected = {}
        for item in request.data['items']:
            if (not isinstance(item, dict) or set(item) != {'id', 'task_id', 'assignee_id', 'version', 'assigned_at'}
                    or any(type(item[name]) is not int or not 0 < item[name] < 2**63
                           for name in ('id', 'task_id', 'assignee_id', 'version'))
                    or not isinstance(item['assigned_at'], str) or len(item['assigned_at']) > 64
                    or item['id'] in expected):
                raise ValidationError({'detail': 'Invalid work selection.'})
            try:
                assigned_at = parse_datetime(item['assigned_at'])
            except (TypeError, ValueError, OverflowError):
                raise ValidationError({'detail': 'Invalid work selection.'})
            if assigned_at is None or timezone.is_naive(assigned_at):
                raise ValidationError({'detail': 'Invalid work selection.'})
            expected[item['id']] = (item['task_id'], item['assignee_id'], item['version'], assigned_at)
        _require_blind_mode(candidate, principal)
        ids = sorted(expected)
        scopes = list(TaskAssignment.objects.filter(pk__in=ids, project=candidate, task__project=candidate)
                      .values('id', 'task_id', 'assignee_id', 'version', 'assigned_at'))
        if len(scopes) != len(ids):
            raise NotFound()
        if any(expected[item['id']] != (item['task_id'], item['assignee_id'], item['version'], item['assigned_at'])
               for item in scopes):
            raise AssignmentConflictError()
        project, locked_actor = lock_project_operation(
            pk, principal, ASSIGN, related_user_ids=[item['assignee_id'] for item in scopes],
        )
        if locked_actor.pk != actor.pk or guard(request, pk)[0] != principal:
            raise NotFound()
        lock_capture_epoch_for_write()
        task_ids = sorted({item['task_id'] for item in scopes})
        tasks = {task_id: _lock_bounded_task(project_id=project.pk, task_id=task_id) for task_id in task_ids}
        assignments = list(TaskAssignment.objects.select_for_update().filter(pk__in=ids, project=project)
                           .order_by('pk'))
        if len(tasks) != len(task_ids) or len(assignments) != len(ids):
            raise NotFound()
        for assignment in assignments:
            if expected.get(assignment.pk) != (assignment.task_id, assignment.assignee_id,
                                                assignment.version, assignment.assigned_at):
                raise AssignmentConflictError()
            task = tasks.get(assignment.task_id)
            assignee = User.objects.filter(pk=assignment.assignee_id, is_active=True,
                                           active_organization_id=project.organization_id).first()
            if task is None or assignee is None or assignment.status not in TaskAssignment.ACTIVE_STATUSES:
                raise NotFound()
            task.project = project
            assignment.task, assignment.project = task, project
            live = authorization.active_task_assignment(local_principal(assignee), task, _locked_task=True)
            if live is None or live.pk != assignment.pk or live.project_id != project.pk:
                raise NotFound()
            decision = BlindMixDecision.objects.filter(chosen_id_frozen=assignment.pk).first()
            marker = BlindQualityAssignment.objects.filter(task_id=assignment.task_id,
                                                            assignment_id=assignment.pk).first()
            if marker is None and BlindQualityAssignment.objects.filter(task_id=assignment.task_id).exists():
                raise NotFound()
            if decision is not None:
                seal_mixed_read_binding(assignment=assignment, decision=decision)
            elif marker is not None:
                seal_unmixed_quality_read_binding(assignment=assignment, marker=marker)
            else:
                seal_ordinary_read_binding(assignment=assignment)
        response = Response({'processed': len(assignments)})
        response['Cache-Control'] = 'private, no-store'
        return response
