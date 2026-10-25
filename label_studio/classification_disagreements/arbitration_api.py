from uuid import UUID

from access_control.identity import resolve_actor
from access_control.project_operations import (
    ARBITRATE,
    ORGANIZE_ARBITRATION,
    READ_WORK,
    SECURITY_CANCEL,
    lock_project_operation,
    project_operation_policy,
)
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.generics import get_object_or_404

from . import arbitration as a
from .batch_api import BatchAPI
from .models import ArbitrationDecision, ArbitrationTask, ClassificationBatch


def body(request, keys):
    value = request.data
    if not isinstance(value, dict) or set(value) != set(keys):
        a.error('INVALID_INPUT', 400)
    for name in ('batch_id', 'task_id', 'assignee_id', 'expected_version', 'winner_submission_id'):
        if name in value and not a.integer(value[name]):
            a.error('INVALID_INPUT', 400)
    for name in ('expected_manifest_hash', 'expected_input_set_hash'):
        if name in value and not a.hash_value(value[name]):
            a.error('INVALID_INPUT', 400)
    if 'reason' in value:
        if not isinstance(value['reason'], str):
            a.error('INVALID_INPUT', 400)
        try:
            # Reuse DRF's length, null-character and surrogate validators.
            # Keep the raw body unchanged for original-key receipt matching.
            serializers.CharField(min_length=1, max_length=a.MAX_REASON).run_validation(value['reason'])
        except ValidationError:
            a.error('INVALID_INPUT', 400)
    try:
        key = UUID(request.headers.get('Idempotency-Key', ''))
    except (ValueError, AttributeError):
        a.error('INVALID_KEY', 400)
    return value, key


def same_receipt(principal, actor, request_hash, stored_principal, stored_actor, stored_hash):
    if (principal.principal_id, actor.pk, request_hash) != (stored_principal, stored_actor, stored_hash):
        a.error('KEY_CONFLICT')


class ArbitrationAPI(BatchAPI):
    def context(self, request, pk, task_id=None, *, create=False, decide=False, cancel=False, capability=False):
        principal, actor = resolve_actor(request)
        project, role = a.guard(
            pk, principal, actor, create=create, decide=decide, cancel=cancel, capability=capability
        )
        return principal, actor, project, role

    def recheck(self, request, pk, context, **options):
        current = self.context(request, pk, **options)
        if current[0] != context[0] or current[1].pk != context[1].pk or current[3] != context[3]:
            a.error('AUTHORITY_CHANGED')
        return current


class CapabilityAPI(ArbitrationAPI):
    def get(self, request, pk):
        self.query(request)
        context = self.context(request, pk, capability=True)

        def data(current):
            policy = project_operation_policy(current[2])
            read = policy.allows(READ_WORK)
            return {
                'enabled': True,
                'viewer_hash': a.viewer_hash(current),
                'schema': a.SCHEMA,
                'rule': a.RULE,
                'can_read': read,
                'can_create': read and current[3] == 'manager' and policy.allows(ORGANIZE_ARBITRATION),
                'can_decide': read and current[3] == 'reviewer' and policy.allows(ARBITRATE),
                'can_cancel': current[3] == 'manager',
                'limits': {
                    'participants': a.MAX_PARTICIPANTS,
                    'snapshot_bytes': a.MAX_SNAPSHOT_BYTES,
                    'text_bytes': a.MAX_TEXT_BYTES,
                    'reason_characters': a.MAX_REASON,
                },
            }

        value = data(context)

        def verify():
            if data(self.recheck(request, pk, context, capability=True)) != value:
                a.error('AUTHORITY_CHANGED')

        return self.respond(request, value, verify)


class ListAPI(ArbitrationAPI):
    def get(self, request, pk):
        self.query(request, ('page', 'page_size', 'state'))
        state = request.query_params.get('state', 'pending')
        if state not in ('pending', 'resolved', 'cancelled', 'all'):
            a.error('INVALID_INPUT', 400)

        def materialize(context):
            rows = a.scoped(context[2], context[1], context[3])
            if state != 'all':
                rows = rows.filter(status=state)
            page = self.paginate_queryset(rows)
            data = self.get_paginated_response([a.meta(a.checked(row, context[2]), context) for row in page]).data
            return {**data, 'viewer_hash': a.viewer_hash(context)}

        return self.read(request, pk, materialize)

    def post(self, request, pk):
        self.query(request)
        context = self.context(request, pk, create=True)
        value, key = body(request, ('batch_id', 'task_id', 'expected_manifest_hash', 'assignee_id'))
        request_hash = a.digest({'action': 'create', 'project_id': pk, 'body': value})
        principal = context[0]
        with transaction.atomic():
            project, actor = lock_project_operation(
                pk, principal, ORGANIZE_ARBITRATION, related_user_ids=[value['assignee_id']]
            )
            context = self.recheck(request, pk, context, create=True)
            if actor.pk != context[1].pk:
                a.error('AUTHORITY_CHANGED')
            previous = ArbitrationTask.objects.filter(project=project, idempotency_key=key).first()
            initial = None
            if previous:
                same_receipt(
                    principal,
                    actor,
                    request_hash,
                    previous.creator_principal,
                    previous.actor_id_frozen,
                    previous.request_hash,
                )
                row = a.checked(previous, project)
            else:
                batch = get_object_or_404(ClassificationBatch, project=project, pk=value['batch_id'])
                origin = a.origin(project, batch, value['task_id'], value['expected_manifest_hash'])
                target = a.assignee(project, value['assignee_id'], origin['item'])
                initial = a.evidence(project, actor, context[3], origin, lock=True)
                a.require_evidence(initial)
                input_hash = a.input_hash(project, origin['item'])
                if ArbitrationTask.objects.filter(
                    project=project,
                    task_id_frozen=value['task_id'],
                    input_set_hash=input_hash,
                    status__in=['pending', 'resolved'],
                ).exists():
                    a.error('DUPLICATE_ACTIVE')
                row = ArbitrationTask.objects.create(
                    project=project,
                    created_by=actor,
                    assignee=target,
                    creator_principal=principal.principal_id,
                    actor_id_frozen=actor.pk,
                    assignee_id_frozen=target.pk,
                    batch_id_frozen=batch.pk,
                    task_id_frozen=value['task_id'],
                    input_set_hash=input_hash,
                    origin=origin,
                    origin_hash=a.digest(origin),
                    participant_ids=a.participants(origin['item']),
                    idempotency_key=key,
                    request_hash=request_hash,
                )

            result = {
                **a.meta(row, context),
                'replayed': previous is not None,
                'receipt': {
                    'operation': 'create',
                    'key': str(key),
                    'status_at_commit': 'pending',
                    'version_at_commit': 1,
                },
            }

            def verify():
                current = self.recheck(request, pk, context, create=True)
                fresh = a.get_row(current, row.pk)
                if a.meta(fresh, current) != a.meta(row, context):
                    a.error('STATE_CHANGED')
                if initial is not None:
                    a.assignee(current[2], value['assignee_id'], row.origin['item'])
                    if a.evidence(current[2], current[1], current[3], row.origin) != initial:
                        a.error('SOURCE_CHANGED')
                self.recheck(request, pk, context, create=True)

            return self.respond(request, result, verify, status=200 if previous else 201)


class AssigneesAPI(ArbitrationAPI):
    def get(self, request, pk):
        self.query(request, ('batch_id', 'task_id', 'expected_manifest_hash', 'page', 'page_size'))
        values = {}
        for key in ('batch_id', 'task_id'):
            raw = request.query_params.get(key, '')
            if not raw.isascii() or not raw.isdigit() or len(raw) > 16 or not a.integer(int(raw)):
                a.error('INVALID_INPUT', 400)
            values[key] = int(raw)
        expected = request.query_params.get('expected_manifest_hash')
        if not a.hash_value(expected):
            a.error('INVALID_INPUT', 400)
        context = self.context(request, pk, create=True)

        def materialize(current):
            project = current[2]
            batch = get_object_or_404(ClassificationBatch, project=project, pk=values['batch_id'])
            o = a.origin(project, batch, values['task_id'], expected)
            page = self.paginate_queryset(a.eligible_assignees(project, o['item']))
            data = self.get_paginated_response([{'id': u.pk, 'name': u.username or u.email} for u in page]).data
            return {
                **data,
                'viewer_hash': a.viewer_hash(current),
                'batch_id': batch.pk,
                'task_id': values['task_id'],
                'batch_name': batch.name,
                'origin_manifest_hash': expected,
                'input_set_hash': a.input_hash(project, o['item']),
            }

        data = materialize(context)

        def verify():
            current = self.recheck(request, pk, context, create=True)
            if materialize(current) != data:
                a.error('READ_CHANGED')
            self.recheck(request, pk, context, create=True)

        return self.respond(request, data, verify)


class DetailAPI(ArbitrationAPI):
    def get(self, request, pk, arbitration_id):
        self.query(request)
        return self.read(request, pk, lambda c: a.detail(a.get_row(c, arbitration_id), c))


class CommandAPI(ArbitrationAPI):
    action = 'decision'

    def post(self, request, pk, arbitration_id):
        self.query(request)
        cancel = self.action == 'cancel'
        options = {'cancel': True} if cancel else {'decide': True}
        context = self.context(request, pk, **options)
        keys = ['expected_version', 'expected_input_set_hash', 'reason']
        if not cancel:
            keys.append('winner_submission_id')
        value, key = body(request, keys)
        request_hash = a.digest({'action': self.action, 'project_id': pk, 'id': arbitration_id, 'body': value})
        principal = context[0]
        with transaction.atomic():
            project, actor = lock_project_operation(pk, principal, SECURITY_CANCEL if cancel else ARBITRATE)
            context = self.recheck(request, pk, context, **options)
            if actor.pk != context[1].pk:
                a.error('AUTHORITY_CHANGED')
            row = a.get_row(context, arbitration_id, lock=True)
            initial = None
            if cancel:
                previous = ArbitrationTask.objects.filter(project=project, cancel_key=key).first()
                receipt = previous.cancellation if previous else None
                if previous:
                    if previous.pk != row.pk:
                        a.error('KEY_CONFLICT')
                    same_receipt(
                        principal,
                        actor,
                        request_hash,
                        receipt['principal'],
                        receipt['actor_id'],
                        previous.cancel_request_hash,
                    )
            else:
                previous = ArbitrationDecision.objects.filter(project=project, idempotency_key=key).first()
                if previous:
                    if previous.arbitration_id != row.pk:
                        a.error('KEY_CONFLICT')
                    same_receipt(
                        principal,
                        actor,
                        request_hash,
                        previous.principal,
                        previous.actor_id_frozen,
                        previous.request_hash,
                    )
                    a.checked_decision(previous, row)

            if not previous:
                if row.status != 'pending':
                    a.error('TERMINAL')
                if value['expected_version'] != row.version:
                    a.error('CAS_CONFLICT')
                if value['expected_input_set_hash'] != row.input_set_hash:
                    a.error('INPUT_SET_CHANGED')
                if cancel:
                    receipt = {
                        'principal': principal.principal_id,
                        'actor_id': actor.pk,
                        'created_at': timezone.now().isoformat(),
                        'reason': value['reason'].strip(),
                        'input_set_hash': row.input_set_hash,
                        'key': str(key),
                    }
                    receipt['payload_hash'] = a.digest(receipt)
                    row.cancel_key, row.cancel_request_hash, row.cancellation = key, request_hash, receipt
                    row.status = 'cancelled'
                else:
                    winner = next(
                        (
                            e['source']
                            for e in row.origin['item']['cohort']
                            if e['source'] and e['source']['submission_id'] == value['winner_submission_id']
                        ),
                        None,
                    )
                    if winner is None:
                        raise NotFound()
                    initial = a.evidence(project, actor, context[3], row.origin, lock=True)
                    a.require_evidence(initial)
                    args = {
                        'project_id': project.pk,
                        'arbitration_id': row.pk,
                        'actor_id_frozen': actor.pk,
                        'principal': principal.principal_id,
                        'created_at': timezone.now(),
                        'input_set_hash': row.input_set_hash,
                        'rule': a.RULE,
                        'winner': winner,
                        'reason': value['reason'].strip(),
                        'idempotency_key': key,
                        'request_hash': request_hash,
                    }
                    previous = ArbitrationDecision.objects.create(
                        actor=actor, payload_hash=a.digest(a.decision_payload(**args)), **args
                    )
                    row.status = 'resolved'
                row.version += 1
                row.save(update_fields=['status', 'version', 'cancel_key', 'cancel_request_hash', 'cancellation'])
                replayed = False
            else:
                replayed = True

            result = {
                'id': row.pk,
                'status': row.status,
                'version': row.version,
                'replayed': replayed,
                'receipt': {
                    'operation': self.action,
                    'key': str(key),
                    'status_at_commit': row.status,
                    'version_at_commit': row.version,
                },
            }
            if not cancel:
                result['decision_id'] = previous.pk
                result['winner_submission_id'] = previous.winner['submission_id']

            def verify():
                current = self.recheck(request, pk, context, **options)
                fresh = a.get_row(current, row.pk)
                if fresh.status != row.status or fresh.version != row.version:
                    a.error('STATE_CHANGED')
                if cancel:
                    if fresh.cancellation != row.cancellation or fresh.cancel_request_hash != request_hash:
                        a.error('RECEIPT_INVALID')
                else:
                    a.checked_decision(ArbitrationDecision.objects.get(pk=previous.pk), fresh)
                if initial is not None and a.evidence(current[2], current[1], current[3], fresh.origin) != initial:
                    a.error('SOURCE_CHANGED')
                self.recheck(request, pk, context, **options)

            return self.respond(request, result, verify, status=200 if replayed else 201)


class CancelAPI(CommandAPI):
    action = 'cancel'
