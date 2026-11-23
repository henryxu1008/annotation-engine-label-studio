"""Manager commands for mixed work; release remains blocked on blind timing."""

from uuid import UUID

from core.permissions import all_permissions
from django.conf import settings
from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView
from tasks.models import Task
from users.models import User

from .blind_legacy_api import _require_blind_mode
from .blind_mix import create_mixed_assignment, reassign_mixed_assignment
from .gold_api import guard
from .models import BlindMixDecision


# This is deliberately not a setting: current ordinary and quality reads are
# distinguishable. Remove this release gate only after the boundary is accepted.
_PUBLIC_MIX_READY = False


class BlindMixCommandAPI(APIView):
    permission_required = all_permissions.annotations_view

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'private, no-store'
        response['Referrer-Policy'] = 'no-referrer'
        return response

    @transaction.atomic
    def post(self, request, pk, predecessor_id=None):
        principal, actor, project = guard(request, pk)
        _require_blind_mode(project, principal)
        if not _PUBLIC_MIX_READY or not getattr(settings, 'ANNOTATION_CLASSIFICATION_BLIND_DISPATCH_ENABLED', False):
            raise NotFound()
        data = request.data
        expected = {'task_id', 'assignee_id'} if predecessor_id is not None else {
            'task_id', 'assignee_id', 'references', 'ratio_basis_points',
        }
        if (request.query_params or not isinstance(data, dict) or set(data) != expected
                or any(type(data.get(name)) is not int or not 0 < data[name] < 2**63
                       for name in ('task_id', 'assignee_id'))):
            raise ValidationError({'detail': 'Invalid work selection.'})
        try:
            key = UUID(request.headers.get('Idempotency-Key', ''))
        except (ValueError, TypeError, AttributeError):
            raise ValidationError({'detail': 'Invalid work selection.'})
        task = get_object_or_404(Task.objects.filter(project=project), pk=data['task_id'])
        assignee = get_object_or_404(User.objects.filter(is_active=True,
            active_organization_id=project.organization_id), pk=data['assignee_id'])
        # The underlying services lock and recheck project management, the
        # assignee, capture epoch, source, evidence and idempotency key.
        if predecessor_id is None:
            chosen = create_mixed_assignment(task=task, assignee=assignee, principal=principal, key=key,
                references=data['references'], ratio_basis_points=data['ratio_basis_points'])
        else:
            chosen = reassign_mixed_assignment(previous_id=predecessor_id, task=task, assignee=assignee,
                                               principal=principal, key=key)
        decision = get_object_or_404(BlindMixDecision.objects.filter(project=project, key=key,
            actor_id=actor.pk, chosen_id_frozen=chosen.pk), chosen_assignment_id=chosen.pk)
        return Response({'decision_id': decision.pk, 'assignment_id': chosen.pk})
