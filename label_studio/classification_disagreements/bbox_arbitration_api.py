"""Scoped local bbox arbitration commands and receipt reads."""

from access_control.authorization import authorization
from access_control.identity import resolve_actor
from access_control.project_operations import (
    ARBITRATE_BBOX,
    ORGANIZE_BBOX_ARBITRATION,
    READ_WORK,
    SECURITY_CANCEL,
    project_operation_policy,
)
from core.utils.private_file import PrivateFileError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.crypto import salted_hmac
from projects.models import Project
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from tasks.my_work import work_projects

from . import bbox, bbox_arbitration, bbox_batches, bbox_storage, ner_api, ner_arbitration
from .bbox_batch_api import BatchAPI
from .models import BboxMediaPin


def viewer(context):
    principal, actor, project, role = context
    return salted_hmac('bbox-arbitration-viewer/v1', bbox.digest({
        'principal': principal.principal_id, 'source': principal.source, 'actor': actor.pk,
        'project': project.pk, 'role': role,
    }), algorithm='sha256').hexdigest()


def metadata(context, row):
    row = bbox_arbitration.checked(context[2], row)
    if context[3] == 'reviewer' and (row.assignee_id_frozen != context[1].pk
                                     or context[1].pk in row.participant_ids):
        raise NotFound()
    batch = bbox_batches.checked(get_object_or_404(bbox_batches.query(context[2]), pk=row.batch_id), context[2])
    policy = project_operation_policy(context[2])
    value = {'id': row.pk, 'schema': bbox_arbitration.SCHEMA, 'decision_rule': bbox_arbitration.RULE,
             'project_id': context[2].pk, 'batch_id': row.batch_id, 'task_id': row.task_id_frozen,
             'batch_name': batch.name, 'assignee_id': row.assignee_id_frozen, 'input_set_hash': row.input_hash,
             'viewer_hash': viewer(context), 'status': row.status, 'version': row.version,
             'created_at': row.created_at.isoformat(), 'calculated_at': batch.manifest['calculated_at'],
             'can_decide': context[3] == 'reviewer' and row.status == 'pending' and policy.allows(ARBITRATE_BBOX),
             'can_cancel': context[3] == 'manager' and row.status == 'pending'}
    if context[3] == 'manager':
        value['origin_manifest_hash'] = row.origin['batch_hash']
    return value


class ArbitrationAPI(BatchAPI):
    def context(self, request, pk, *, role, operation):
        principal, actor = resolve_actor(request)
        project = get_object_or_404(Project.objects.filter(
            pk__in=work_projects(actor, ['manager', 'reviewer']), deleted_at__isnull=True), pk=pk)
        bbox_arbitration.guard(project, principal, role=role, operation=operation)
        return principal, actor, project, role

    def recheck(self, request, pk, initial, *, role, operation):
        current = self.context(request, pk, role=role, operation=operation)
        if current[0] != initial[0] or current[1].pk != initial[1].pk or current[3] != initial[3]:
            raise NotFound()
        return current


class CreateAPI(ArbitrationAPI):
    def get(self, request, pk):
        if (set(request.query_params) - {'page', 'page_size', 'state'}
                or any(len(request.query_params.getlist(key)) != 1 for key in request.query_params)):
            raise ValidationError('Invalid input.')
        state = request.query_params.get('state', 'all')
        if state not in ('all', 'pending', 'resolved', 'cancelled'):
            raise ValidationError('Invalid input.')
        principal, actor = resolve_actor(request)
        project = get_object_or_404(Project.objects.filter(
            pk__in=work_projects(actor, ['manager', 'reviewer']), deleted_at__isnull=True), pk=pk)
        role = authorization.project_role(principal, project)
        if role not in ('manager', 'reviewer'):
            raise NotFound()
        initial = self.context(request, pk, role=role, operation=READ_WORK)

        def materialize(context):
            query = bbox_arbitration.rows(context[2])
            if role == 'reviewer':
                query = ner_arbitration.independent_query(query, context[1])
            if state != 'all':
                query = query.filter(status=state)
            page = self.paginate_queryset(query.order_by('-pk'))
            return {**self.get_paginated_response([metadata(context, row) for row in page]).data,
                    'viewer_hash': viewer(context)}

        body = materialize(initial)
        current = self.recheck(request, pk, initial, role=role, operation=READ_WORK)
        if materialize(current) != body:
            raise bbox.Error('BBOX_ARBITRATION_SOURCE_CHANGED', 409)
        self.recheck(request, pk, initial, role=role, operation=READ_WORK)
        return Response(body)

    def post(self, request, pk):
        required = {'batch_id', 'task_id', 'expected_hash', 'assignee_id'}
        if request.query_params or not isinstance(request.data, dict) or set(request.data) != required:
            raise ValidationError('Invalid input.')
        initial = self.context(request, pk, role='manager', operation=ORGANIZE_BBOX_ARBITRATION)
        row, replayed = bbox_arbitration.create(initial[2], initial[0], **request.data,
                                                idempotency_key=request.headers.get('Idempotency-Key'))
        self.recheck(request, pk, initial, role='manager', operation=ORGANIZE_BBOX_ARBITRATION)
        return Response({**metadata(initial, row), 'replayed': replayed,
                         'receipt': {'operation': 'create', 'key': str(row.idempotency_key),
                                     'actor_id': row.actor_id_frozen, 'status_at_commit': 'pending',
                                     'version_at_commit': 1, 'request_hash': row.request_hash,
                                     'input_set_hash': row.input_hash}},
                        status=200 if replayed else 201)


class AssigneesAPI(ArbitrationAPI):
    def get(self, request, pk):
        allowed = {'batch_id', 'task_id', 'expected_hash', 'page', 'page_size'}
        if (set(request.query_params) - allowed
                or any(len(request.query_params.getlist(key)) != 1 for key in request.query_params)):
            raise ValidationError('Invalid input.')
        batch_id = ner_api.parse_id(request.query_params.get('batch_id', ''))
        task_id = ner_api.parse_id(request.query_params.get('task_id', ''))
        expected = request.query_params.get('expected_hash', '')
        if not ner_api.HEX.fullmatch(expected):
            raise ValidationError('Invalid input.')
        initial = self.context(request, pk, role='manager', operation=ORGANIZE_BBOX_ARBITRATION)

        def materialize(context):
            batch = get_object_or_404(bbox_batches.query(context[2]), pk=batch_id)
            origin = bbox_arbitration.frozen_origin(context[2], batch, task_id, expected)
            page = self.paginate_queryset(bbox_arbitration.common.eligible_assignees(context[2], origin['item']))
            return {**self.get_paginated_response([{'id': user.pk, 'name': user.username or user.email}
                                                    for user in page]).data,
                    'batch_id': batch_id, 'task_id': task_id, 'expected_hash': expected,
                    'batch_name': batch.name, 'origin_manifest_hash': expected,
                    'input_set_hash': bbox_arbitration.input_hash(context[2], origin['item']),
                    'viewer_hash': viewer(context)}

        body = materialize(initial)
        current = self.recheck(request, pk, initial, role='manager', operation=ORGANIZE_BBOX_ARBITRATION)
        if materialize(current) != body:
            raise bbox.Error('BBOX_ARBITRATION_SOURCE_CHANGED', 409)
        self.recheck(request, pk, initial, role='manager', operation=ORGANIZE_BBOX_ARBITRATION)
        return Response(body)


class CapabilityAPI(ArbitrationAPI):
    def get(self, request, pk):
        if request.query_params:
            raise ValidationError('Invalid input.')
        principal, actor = resolve_actor(request)
        project = get_object_or_404(Project.objects.filter(
            pk__in=work_projects(actor, ['manager', 'reviewer']), deleted_at__isnull=True), pk=pk)
        role = authorization.project_role(principal, project)
        if role not in ('manager', 'reviewer'):
            raise NotFound()
        initial = self.context(request, pk, role=role, operation=READ_WORK)
        policy = project_operation_policy(initial[2])
        viewer_hash = viewer(initial)
        body = {'enabled': True, 'schema': bbox_arbitration.SCHEMA, 'decision_rule': bbox_arbitration.RULE,
                'project_id': pk, 'viewer': viewer_hash, 'viewer_hash': viewer_hash, 'actor_id': actor.pk, 'can_read': True,
                'can_create': role == 'manager' and policy.allows(ORGANIZE_BBOX_ARBITRATION),
                'can_decide': role == 'reviewer' and policy.allows(ARBITRATE_BBOX), 'can_cancel': role == 'manager'}
        self.recheck(request, pk, initial, role=role, operation=READ_WORK)
        return Response(body)


class DecideAPI(ArbitrationAPI):
    def post(self, request, pk, arbitration_id):
        if (request.query_params or not isinstance(request.data, dict)
                or set(request.data) != {'winner_submission_id', 'reason', 'expected_version', 'expected_input_set_hash'}):
            raise ValidationError('Invalid input.')
        initial = self.context(request, pk, role='reviewer', operation=ARBITRATE_BBOX)
        decision, replayed = bbox_arbitration.decide(initial[2], initial[0], arbitration_id=arbitration_id,
            **request.data, idempotency_key=request.headers.get('Idempotency-Key'))
        self.recheck(request, pk, initial, role='reviewer', operation=ARBITRATE_BBOX)
        row = bbox_arbitration.checked(initial[2], get_object_or_404(bbox_arbitration.rows(initial[2]), pk=arbitration_id))
        bbox_arbitration.checked_decision(decision, row)
        return Response({**metadata(initial, row), 'decision_id': decision.pk,
                         'winner_submission_id': decision.winner['submission_id'], 'winner_revision': decision.winner['revision'],
                         'payload_hash': decision.payload_hash, 'replayed': replayed,
                         'receipt': {'operation': 'decision', 'key': str(decision.idempotency_key),
                                     'actor_id': decision.actor_id_frozen, 'status_at_commit': 'resolved',
                                     'version_at_commit': 2, 'request_hash': decision.request_hash,
                                     'input_set_hash': decision.input_hash}},
                        status=200 if replayed else 201)


class CancelAPI(ArbitrationAPI):
    def post(self, request, pk, arbitration_id):
        if (request.query_params or not isinstance(request.data, dict)
                or set(request.data) != {'reason', 'expected_version', 'expected_input_set_hash'}):
            raise ValidationError('Invalid input.')
        initial = self.context(request, pk, role='manager', operation=SECURITY_CANCEL)
        row, replayed = bbox_arbitration.cancel(initial[2], initial[0], arbitration_id=arbitration_id,
                                                **request.data,
                                                idempotency_key=request.headers.get('Idempotency-Key'))
        self.recheck(request, pk, initial, role='manager', operation=SECURITY_CANCEL)
        row = get_object_or_404(bbox_arbitration.rows(initial[2]), pk=row.pk)
        return Response({**metadata(initial, row), 'replayed': replayed,
                         'receipt': {'operation': 'cancel', 'key': str(row.cancel_key),
                                     'actor_id': row.cancellation['actor_id'], 'status_at_commit': 'cancelled',
                                     'version_at_commit': 2, 'request_hash': row.cancel_request_hash,
                                     'input_set_hash': row.input_hash}})


class DetailAPI(ArbitrationAPI):
    def get(self, request, pk, arbitration_id):
        if (set(request.query_params) not in (set(), {'left', 'right'})
                or any(len(request.query_params.getlist(key)) != 1 for key in request.query_params)):
            raise ValidationError('Invalid input.')
        selected = (sorted(ner_api.parse_id(request.query_params[key]) for key in ('left', 'right'))
                    if request.query_params else None)
        if selected and selected[0] == selected[1]:
            raise ValidationError('Invalid input.')
        principal, actor = resolve_actor(request)
        project = get_object_or_404(Project.objects.filter(
            pk__in=work_projects(actor, ['manager', 'reviewer']), deleted_at__isnull=True), pk=pk)
        role = authorization.project_role(principal, project)
        if role not in ('manager', 'reviewer'):
            raise NotFound()
        initial = self.context(request, pk, role=role, operation=READ_WORK)

        def materialize(context):
            row = bbox_arbitration.checked(context[2], get_object_or_404(
                bbox_arbitration.rows(context[2]), pk=arbitration_id))
            if context[3] == 'reviewer' and (row.assignee_id_frozen != context[1].pk
                                             or context[1].pk in row.participant_ids):
                raise NotFound()
            batch = get_object_or_404(bbox_batches.query(context[2]), pk=row.batch_id)
            if bbox_arbitration.frozen_origin(context[2], batch, row.task_id_frozen, row.origin['batch_hash']) != row.origin:
                raise bbox.Error('BBOX_ARBITRATION_SOURCE_CHANGED', 409)
            decision = bbox_arbitration.decisions(context[2]).filter(arbitration_id=row.pk).first()
            if decision:
                decision = bbox_arbitration.checked_decision(decision, row)
            origin = row.origin
            ids = sorted(entry['source']['submission_id'] for entry in origin['item']['cohort'] if entry['source'])
            chosen = selected or ids[:2]
            if len(chosen) != 2 or not set(chosen).issubset(ids):
                raise ValidationError('Invalid input.')
            if context[3] == 'reviewer':
                origin = {key: value for key, value in origin.items() if key != 'batch_hash'}
                origin['projection_hash'] = bbox.digest({'schema': bbox_arbitration.SCHEMA, 'item': origin['item']})
            return {**metadata(context, row), 'origin': origin, 'selected_pair': chosen,
                    'cancellation': row.cancellation, 'decision': (
                        {'id': decision.pk, 'actor_id': decision.actor_id_frozen, 'winner': decision.winner,
                         'winner_submission_id': decision.winner['submission_id'], 'winner_revision': decision.winner['revision'],
                         'reason': decision.reason, 'payload_hash': decision.payload_hash,
                         'created_at': decision.created_at.isoformat()}
                        if decision else None)}

        body = materialize(initial)
        bbox.bounded(body, bbox_arbitration.MAX_ORIGIN + 65536)
        response = self.finalize_response(request, Response({**body, 'observed_at': timezone.now().isoformat()}))
        response.render()
        if len(response.content) > bbox_arbitration.MAX_ORIGIN + 65536:
            raise bbox.Error('BBOX_ARBITRATION_TOO_LARGE', 422)
        current = self.recheck(request, pk, initial, role=role, operation=READ_WORK)
        if bbox.digest(materialize(current)) != bbox.digest(body):
            raise bbox.Error('BBOX_ARBITRATION_SOURCE_CHANGED', 409)
        self.recheck(request, pk, initial, role=role, operation=READ_WORK)
        return response


class ImageAPI(ArbitrationAPI):
    def get(self, request, pk, arbitration_id):
        if request.query_params:
            raise ValidationError('Invalid input.')
        principal, actor = resolve_actor(request)
        project = get_object_or_404(Project.objects.filter(
            pk__in=work_projects(actor, ['manager', 'reviewer']), deleted_at__isnull=True), pk=pk)
        role = authorization.project_role(principal, project)
        if role not in ('manager', 'reviewer'):
            raise NotFound()
        initial = self.context(request, pk, role=role, operation=READ_WORK)

        def verify(context):
            row = bbox_arbitration.checked(context[2], get_object_or_404(
                bbox_arbitration.rows(context[2]), pk=arbitration_id))
            if context[3] == 'reviewer' and (row.assignee_id_frozen != context[1].pk
                                             or context[1].pk in row.participant_ids):
                raise NotFound()
            batch = get_object_or_404(bbox_batches.query(context[2]), pk=row.batch_id)
            if bbox_arbitration.frozen_origin(context[2], batch, row.task_id_frozen, row.origin['batch_hash']) != row.origin:
                raise bbox.Error('BBOX_ARBITRATION_SOURCE_CHANGED', 409)
            pin = get_object_or_404(BboxMediaPin, project=context[2], task_id=row.task_id_frozen)
            sources = [entry['source'] for entry in row.origin['item']['cohort'] if entry['source']]
            if (not sources or any(not source.get('media') or source['media']['token'] != str(pin.token)
                                   or source['media']['sha256'] != pin.sha256 for source in sources)
                    or not 0 < pin.size_bytes <= bbox.MAX_IMAGE_BYTES):
                raise NotFound()
            return row, pin

        row, pin = verify(initial)
        try:
            with bbox_storage.open_verified(pin.token, pin.storage_binding, pin.sha256, pin.size_bytes) as reader:
                data = reader.read(pin.size_bytes + 1)
            if bbox.image_identity(data) != {'sha256': pin.sha256, 'width': pin.width, 'height': pin.height}:
                raise PrivateFileError()
        except (PrivateFileError, bbox.Incomparable, OSError, ValueError) as exc:
            raise NotFound() from exc
        current = self.recheck(request, pk, initial, role=role, operation=READ_WORK)
        fresh_row, fresh_pin = verify(current)
        if fresh_row.origin_hash != row.origin_hash or fresh_pin.pk != pin.pk:
            raise NotFound()
        self.recheck(request, pk, initial, role=role, operation=READ_WORK)
        response = HttpResponse(data, content_type='image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg')
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        return response
