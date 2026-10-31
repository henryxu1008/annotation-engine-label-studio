"""field-only dispatch/adopt/cancel API with fenced, source-free receipt recovery."""

from uuid import UUID

from access_control.identity import resolve_actor
from access_control.project_operations import (
    ARBITRATE_FIELD,
    ORGANIZE_FIELD_ARBITRATION,
    READ_WORK,
    SECURITY_CANCEL,
    lock_project_operation,
    project_operation_policy,
)
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from . import field_api, fields
from . import field_arbitration as a
from . import field_batches as b
from .field_batch_api import BatchAPI
from .models import FieldArbitrationDecision, FieldArbitrationTask


def body(request, keys):
    value = request.data
    if not isinstance(value, dict) or set(value) != set(keys):
        a.error('INVALID_INPUT', 400)
    for name in ('field_batch_id', 'task_id', 'assignee_id', 'expected_version', 'winner_submission_id'):
        if name in value and not a.integer(value[name]):
            a.error('INVALID_INPUT', 400)
    for name in ('expected_manifest_hash', 'expected_input_set_hash'):
        if name in value and not a.hash_value(value[name]):
            a.error('INVALID_INPUT', 400)
    if 'reason' in value:
        if not isinstance(value['reason'], str):
            a.error('INVALID_INPUT', 400)
        try:
            serializers.CharField(min_length=1, max_length=a.MAX_REASON).run_validation(value['reason'])
        except ValidationError:
            a.error('INVALID_INPUT', 400)
    a.bounded(value, a.MAX_COMMAND)
    try:
        key = UUID(request.headers.get('Idempotency-Key', ''))
    except (ValueError, AttributeError):
        a.error('INVALID_KEY', 400)
    return value, key


def same_receipt(context, request_hash, stored_principal, stored_source, stored_actor, stored_hash):
    principal, actor = context[:2]
    if (principal.principal_id, principal.source, actor.pk, request_hash) != (
        stored_principal,
        stored_source,
        stored_actor,
        stored_hash,
    ):
        a.error('KEY_CONFLICT')


def command_hash(action, project, value, identifier=None):
    return fields.digest(
        {'schema': a.SCHEMA, 'action': action, 'project_id': project, 'id': identifier, 'body': value}
    )


def receipt(row, action, key, principal, source, actor, when, request_hash):
    value = {
        'schema': a.SCHEMA,
        'operation': action,
        'project_id': row.project_id,
        'arbitration_id': row.pk,
        'principal': principal,
        'source': source,
        'actor_id': actor,
        'key': str(key),
        'created_at': when,
        'request_hash': request_hash,
        'input_set_hash': row.input_set_hash,
        'status_at_commit': {'create': 'pending', 'decision': 'resolved', 'cancel': 'cancelled'}[action],
        'version_at_commit': 1 if action == 'create' else 2,
    }
    return {**value, 'payload_hash': fields.digest(value)}


def create_task(context, value, key, request_hash, *, verified=None):
    """Fresh creation and one bounded evidence pass inside the caller's project fence."""
    principal, actor, project, _ = context
    batch = get_object_or_404(b.query_batches(project).select_for_update(), pk=value['field_batch_id'])
    frozen = a.origin(project, batch, value['task_id'], value['expected_manifest_hash'])
    target = a.assignee(context, value['assignee_id'], frozen['item'])
    if verified is None:
        initial, prepared = a.evidence(context, frozen, lock=True)
    else:
        verified_origin, initial, prepared = verified
        if verified_origin != frozen:
            a.error('SOURCE_CHANGED')
    a.require_evidence(initial)
    semantic = a.input_hash(project, frozen['item'])
    if FieldArbitrationTask.objects.filter(
        project=project,
        task_id_frozen=value['task_id'],
        input_set_hash=semantic,
        status__in=['pending', 'resolved'],
    ).exists():
        a.error('DUPLICATE_ACTIVE')
    row = FieldArbitrationTask.objects.create(
        project=project,
        created_by=actor,
        assignee=target,
        creator_principal=principal.principal_id,
        creator_source=principal.source,
        actor_id_frozen=actor.pk,
        assignee_id_frozen=target.pk,
        field_batch_id_frozen=batch.pk,
        task_id_frozen=value['task_id'],
        input_set_hash=semantic,
        origin=frozen,
        origin_hash=fields.digest(frozen),
        participant_ids=a.participants(frozen['item']),
        idempotency_key=key,
        request_hash=request_hash,
    )
    return row, initial, prepared


class ArbitrationAPI(BatchAPI):
    def handle_exception(self, exc):
        # Keep the typed API domain even when a reused field validator rejects work.
        if isinstance(exc, fields.Error) and not exc.code.startswith('FIELD_ARBITRATION_'):
            suffix = exc.code.removeprefix('FIELD_BATCH_').removeprefix('FIELD_')
            suffix = {
                'TOO_LARGE': 'BUDGET_EXCEEDED',
                'MANIFEST_INVALID': 'ORIGIN_INVALID',
                'RESULT_CHANGED': 'SOURCE_CHANGED',
            }.get(suffix, suffix)
            exc = fields.Error('FIELD_ARBITRATION_' + suffix, exc.status)
        return super().handle_exception(exc)

    def context(self, request, pk, **options):
        principal, actor = resolve_actor(request)
        project, role = a.guard(pk, principal, actor, **options)
        return principal, actor, project, role

    def recheck(self, request, pk, initial, **options):
        current = self.context(request, pk, **options)
        if (
            current[0] != initial[0]
            or current[1].pk != initial[1].pk
            or current[3] != initial[3]
            or current[2].organization_id != initial[2].organization_id
        ):
            a.error('AUTHORITY_CHANGED')
        return current

    def query(self, request, allowed=()):
        q = request.query_params
        if set(q) - set(allowed) or any(len(q.getlist(k)) != 1 for k in q):
            a.error('INVALID_INPUT', 400)
        for key in ('page', 'page_size'):
            if key in q and (
                not q[key].isascii()
                or not q[key].isdigit()
                or len(q[key]) > 9
                or int(q[key]) < 1
                or (key == 'page_size' and int(q[key]) > 100)
            ):
                a.error('INVALID_INPUT', 400)

    def read(self, request, pk, materialize, **options):
        initial = self.context(request, pk, **options)
        data, prepared = materialize(initial)

        def verify():
            current = self.recheck(request, pk, initial, **options)
            # The second calculation already ran; this replay only inspects current sources.
            fresh, _ = materialize(current, prepared)
            if fresh != data:
                a.error('READ_CHANGED')
            self.recheck(request, pk, initial, **options)

        return self.respond(
            request,
            {**data, 'observed_at': timezone.now().isoformat(), 'processing_cost': a.processing(prepared)},
            verify,
            lambda: a.check_engine(prepared),
        )


class CapabilityAPI(ArbitrationAPI):
    def get(self, request, pk):
        self.query(request)

        def data(context, replay=None):
            policy = project_operation_policy(context[2])
            read = policy.allows(READ_WORK)
            return {
                'enabled': True,
                'schema': a.SCHEMA,
                'decision_rule': a.RULE,
                'actor_id': context[1].pk,
                'viewer_hash': a.viewer_hash(context),
                'can_read': read,
                'can_create': read and context[3] == 'manager' and policy.allows(ORGANIZE_FIELD_ARBITRATION),
                'can_decide': read and context[3] == 'reviewer' and policy.allows(ARBITRATE_FIELD),
                'can_cancel': context[3] == 'manager',
                'limits': {
                    'participants': a.MAX_PARTICIPANTS,
                    'scorer_calls': 0,
                    'normalizations_per_pass': a.MAX_NORMALIZATIONS,
                    'equalities_per_pass': a.MAX_EQUALITIES,
                    'reason_characters': a.MAX_REASON,
                },
            }, {}

        return self.read(request, pk, data, capability=True)


class AssigneesAPI(ArbitrationAPI):
    def get(self, request, pk):
        self.query(request, ('field_batch_id', 'task_id', 'expected_manifest_hash', 'page', 'page_size'))
        ids = {}
        for key in ('field_batch_id', 'task_id'):
            try:
                ids[key] = field_api.parse_id(request.query_params.get(key, ''))
            except fields.Error:
                a.error('INVALID_INPUT', 400)
        expected = request.query_params.get('expected_manifest_hash')
        if not a.hash_value(expected):
            a.error('INVALID_INPUT', 400)

        def data(context, replay=None):
            batch = get_object_or_404(b.query_batches(context[2]), pk=ids['field_batch_id'])
            frozen = a.origin(context[2], batch, ids['task_id'], expected)
            page = self.paginate_queryset(a.eligible_assignees(context, frozen['item']))
            result = self.get_paginated_response([{'id': u.pk, 'name': u.username or u.email} for u in page]).data
            return {
                **result,
                **ids,
                'batch_name': batch.name,
                'expected_manifest_hash': expected,
                'origin_manifest_hash': expected,
                'input_set_hash': a.input_hash(context[2], frozen['item']),
                'viewer_hash': a.viewer_hash(context),
            }, {}

        return self.read(request, pk, data, create=True)


class ListAPI(ArbitrationAPI):
    def get(self, request, pk):
        self.query(request, ('state', 'page', 'page_size'))
        state = request.query_params.get('state', 'pending')
        if state not in ('pending', 'resolved', 'cancelled', 'all'):
            a.error('INVALID_INPUT', 400)

        def data(context, replay=None):
            rows = a.scoped(context[2], context[1], context[3])
            if state != 'all':
                rows = rows.filter(status=state)
            page = self.paginate_queryset(rows)
            value = self.get_paginated_response([a.meta(a.checked(row, context[2]), context) for row in page]).data
            return {**value, 'viewer_hash': a.viewer_hash(context)}, {}

        return self.read(request, pk, data)

    def post(self, request, pk):
        self.query(request)
        context = self.context(request, pk, create=True)
        value, key = body(request, ('field_batch_id', 'task_id', 'expected_manifest_hash', 'assignee_id'))
        request_hash = command_hash('create', pk, value)
        principal = context[0]
        with transaction.atomic():
            project, actor = lock_project_operation(
                pk, principal, ORGANIZE_FIELD_ARBITRATION, related_user_ids=[value['assignee_id']]
            )
            context = self.recheck(request, pk, context, create=True)
            if actor.pk != context[1].pk:
                a.error('AUTHORITY_CHANGED')
            row = a.rows(project).filter(idempotency_key=key).first()
            replayed = row is not None
            initial, prepared = None, {}
            if replayed:
                row = a.checked(row, project)
                same_receipt(
                    context,
                    request_hash,
                    row.creator_principal,
                    row.creator_source,
                    row.actor_id_frozen,
                    row.request_hash,
                )
            else:
                row, initial, prepared = create_task(context, value, key, request_hash)
            result = {
                **a.meta(row, context),
                'action': 'create',
                'processing_cost': a.processing(prepared),
                'replayed': replayed,
                'current_status': row.status,
                'current_version': row.version,
                'receipt': receipt(
                    row,
                    'create',
                    key,
                    row.creator_principal,
                    row.creator_source,
                    row.actor_id_frozen,
                    row.created_at.isoformat(),
                    request_hash,
                ),
            }

            def verify():
                current = self.recheck(request, pk, context, create=True)
                fresh = a.get_row(current, row.pk)
                if a.meta(fresh, current) != a.meta(row, context):
                    a.error('STATE_CHANGED')
                if initial is not None:
                    a.assignee(current, value['assignee_id'], fresh.origin['item'])
                    if a.evidence(current, fresh.origin, replay=prepared)[0] != initial:
                        a.error('SOURCE_CHANGED')
                self.recheck(request, pk, context, create=True)

            return self.respond(
                request, result, verify, lambda: a.check_engine(prepared), status=200 if replayed else 201
            )


class DetailAPI(ArbitrationAPI):
    def get(self, request, pk, arbitration_id):
        self.query(request, ('left', 'right'))
        pair = None
        if bool('left' in request.query_params) != bool('right' in request.query_params):
            a.error('INVALID_INPUT', 400)
        if 'left' in request.query_params:
            pair = tuple(field_api.parse_id(request.query_params[k]) for k in ('left', 'right'))
            if pair[0] == pair[1]:
                a.error('INVALID_INPUT', 400)
        return self.read(
            request, pk, lambda c, replay=None: a.detail(a.get_row(c, arbitration_id), c, pair, replay=replay)
        )


class CommandAPI(ArbitrationAPI):
    action = 'decision'

    def post(self, request, pk, arbitration_id):
        self.query(request)
        cancel = self.action == 'cancel'
        options = {'cancel': True} if cancel else {'decide': True}
        context = self.context(request, pk, **options)
        keys = ['expected_version', 'expected_input_set_hash', 'reason'] + ([] if cancel else ['winner_submission_id'])
        value, key = body(request, keys)
        request_hash = command_hash(self.action, pk, value, arbitration_id)
        principal = context[0]
        with transaction.atomic():
            project, actor = lock_project_operation(pk, principal, SECURITY_CANCEL if cancel else ARBITRATE_FIELD)
            context = self.recheck(request, pk, context, **options)
            if actor.pk != context[1].pk:
                a.error('AUTHORITY_CHANGED')
            row = a.get_row(context, arbitration_id, lock=True)
            initial, prepared = None, {}
            previous = (
                a.rows(project).filter(cancel_key=key).first()
                if cancel
                else a.decisions(project).filter(idempotency_key=key).first()
            )
            replayed = previous is not None
            if previous:
                if cancel:
                    previous = a.checked(previous, project)
                    c = previous.cancellation
                    if previous.pk != row.pk:
                        a.error('KEY_CONFLICT')
                    same_receipt(
                        context, request_hash, c['principal'], c['source'], c['actor_id'], previous.cancel_request_hash
                    )
                else:
                    if previous.arbitration_id != row.pk:
                        a.error('KEY_CONFLICT')
                    a.checked_decision(previous, row)
                    same_receipt(
                        context,
                        request_hash,
                        previous.principal,
                        previous.source,
                        previous.actor_id_frozen,
                        previous.request_hash,
                    )
            else:
                if row.status != 'pending':
                    a.error('TERMINAL')
                if value['expected_version'] != row.version:
                    a.error('CAS_CONFLICT')
                if value['expected_input_set_hash'] != row.input_set_hash:
                    a.error('INPUT_SET_CHANGED')
                if cancel:
                    c = {
                        'schema': a.SCHEMA,
                        'project_id': pk,
                        'arbitration_id': row.pk,
                        'principal': principal.principal_id,
                        'source': principal.source,
                        'actor_id': actor.pk,
                        'created_at': timezone.now().isoformat(),
                        'reason': value['reason'].strip(),
                        'input_set_hash': row.input_set_hash,
                        'key': str(key),
                        'request_hash': request_hash,
                    }
                    c['payload_hash'] = fields.digest(c)
                    row.cancel_key, row.cancel_request_hash, row.cancellation = key, request_hash, c
                    row.status = 'cancelled'
                else:
                    winner = next(
                        (
                            e['source']
                            for e in row.origin['item']['cohort']
                            if e['source']['submission_id'] == value['winner_submission_id']
                        ),
                        None,
                    )
                    if winner is None:
                        a.error('INVALID_INPUT', 400)
                    initial, prepared = a.evidence(context, row.origin, lock=True)
                    a.require_evidence(initial)
                    args = {
                        'project_id': pk,
                        'arbitration_id': row.pk,
                        'actor_id_frozen': actor.pk,
                        'principal': principal.principal_id,
                        'source': principal.source,
                        'created_at': timezone.now(),
                        'input_set_hash': row.input_set_hash,
                        'rule': a.RULE,
                        'winner': winner,
                        'reason': value['reason'].strip(),
                        'idempotency_key': key,
                        'request_hash': request_hash,
                    }
                    previous = FieldArbitrationDecision.objects.create(
                        actor=actor, payload_hash=fields.digest(a.decision_payload(**args)), **args
                    )
                    row.status = 'resolved'
                row.version = 2
                row.save(update_fields=['status', 'version', 'cancel_key', 'cancel_request_hash', 'cancellation'])
            when = row.cancellation['created_at'] if cancel else previous.created_at.isoformat()
            result = {
                'id': row.pk,
                'schema': a.SCHEMA,
                'action': self.action,
                'processing_cost': a.processing(prepared),
                'input_set_hash': row.input_set_hash,
                'viewer_hash': a.viewer_hash(context),
                'status': row.status,
                'version': row.version,
                'current_status': row.status,
                'current_version': row.version,
                'replayed': replayed,
                'receipt': receipt(
                    row, self.action, key, principal.principal_id, principal.source, actor.pk, when, request_hash
                ),
            }
            if not cancel:
                result.update(
                    decision_id=previous.pk,
                    winner_submission_id=previous.winner['submission_id'],
                    winner_revision=previous.winner['revision'],
                )

            def verify():
                current = self.recheck(request, pk, context, **options)
                fresh = a.get_row(current, row.pk)
                if a.meta(fresh, current) != a.meta(row, context):
                    a.error('STATE_CHANGED')
                if cancel:
                    if fresh.cancellation != row.cancellation:
                        a.error('RECEIPT_INVALID')
                else:
                    a.checked_decision(a.decisions(project).get(pk=previous.pk), fresh)
                if initial is not None and a.evidence(current, fresh.origin, replay=prepared)[0] != initial:
                    a.error('SOURCE_CHANGED')
                self.recheck(request, pk, context, **options)

            return self.respond(
                request, result, verify, lambda: a.check_engine(prepared), status=200 if replayed else 201
            )


class CancelAPI(CommandAPI):
    action = 'cancel'
