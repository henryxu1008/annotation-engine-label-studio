"""Append-only trusted confidence assertions. No model/Prediction integration."""
import json
import re
from datetime import datetime
from datetime import timezone as dt_timezone
from uuid import uuid4

from access_control.project_operations import PROVIDE_REVIEW_CONFIDENCE, lock_project_operation
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import APIException
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from tasks.models import Submission, Task, TaskAssignment

from . import confidence_contract as c
from .models import ReviewConfidenceObservation as Observation

FIELDS = ('proof', 'receipt')
LIMITS = {f: (c.MAX_FACT, c.MAX_FACT_STORED) for f in FIELDS}
POST_FIELDS = {'submission_id', 'profile_id', 'profile_digest', 'evaluated_input_hash', 'score_units', 'evaluated_at'}


def utc(value):
    if not isinstance(value, str) or not re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)', value
    ):
        c.fail('INVALID_INPUT', 400)
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(dt_timezone.utc).isoformat()
    except ValueError:
        c.fail('INVALID_INPUT', 400)


def parse(body):
    if (
        not isinstance(body, dict)
        or set(body) != POST_FIELDS
        or not c.positive(body['submission_id'])
        or not c.uuid_string(body['profile_id'])
        or not c.hexadecimal(body['profile_digest'])
        or not c.hexadecimal(body['evaluated_input_hash'])
        or not c.integer(body['score_units'])
    ):
        c.fail('INVALID_INPUT', 400)
    return {**body, 'evaluated_at': utc(body['evaluated_at'])}


def rows(query, budget=None, *, lock=False):
    budget = budget or c.metadata_budget()
    result = budget.rows(query, FIELDS, LIMITS, lock=lock)
    for row in result:
        validate(row)
    return result


def validate(row):
    try:
        p, r = row.proof, row.receipt
        c.bounded(p, c.MAX_FACT)
        c.bounded(r, c.MAX_FACT)
        descriptor = c.descriptor(p['profile'])
        pin = p['subject']
        valid = (
            set(p)
            == {
                'schema',
                'observation_id',
                'instance_id',
                'organization_id',
                'project_id',
                'profile',
                'profile_digest',
                'subject',
                'input_hash',
                'score_units',
                'producer_namespace',
                'event_uuid',
                'evaluated_at',
                'accepted_at',
            }
            and p['schema'] == c.FACT
            and p['observation_id'] == str(row.pk)
            and p['instance_id'] == str(row.instance_id)
            and p['project_id'] == row.project_id
            and c.positive(p['organization_id'])
            and c.digest(descriptor) == p['profile_digest'] == row.profile_digest
            and descriptor['profile_id'] == str(row.profile_id)
            and descriptor['producer_namespace'] == p['producer_namespace'] == str(row.producer_namespace)
            and p['event_uuid'] == str(row.event_uuid)
            and c.integer(p['score_units'])
            and set(pin) == set(c.PINS)
            and all(c.positive(pin[k]) for k in c.PINS[:6])
            and all(c.hexadecimal(pin[k]) for k in c.PINS[6:])
            and pin['submission_id'] == row.submission_id_frozen
            and (row.submission_id is None or row.submission_id == row.submission_id_frozen)
            and pin['assignee_id'] == pin['submitter_id']
            and c.hexadecimal(p['input_hash'])
            and p['accepted_at'] == row.created_at.isoformat()
            and utc(p['evaluated_at']) == p['evaluated_at']
            and c.digest(p) == row.fact_hash
            and set(r) == {'schema', 'observation_id', 'owner', 'request_hash', 'fact_hash', 'accepted_at'}
            and r['schema'] == c.RECEIPT
            and r['observation_id'] == str(row.pk)
            and r['owner']
            == {'principal': row.creator_principal, 'source': row.creator_source, 'actor_id': row.actor_id_frozen}
            and c.text(row.creator_principal, 512)
            and c.text(row.creator_source, 128)
            and c.positive(row.actor_id_frozen)
            and c.hexadecimal(row.request_hash)
            and r['request_hash'] == row.request_hash
            and r['fact_hash'] == row.fact_hash
            and r['accepted_at'] == p['accepted_at']
            and c.digest(r) == row.receipt_hash
        )
    except APIException as exc:
        if exc.status_code == 422:
            raise
        valid = False
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        c.fail()


def response(data, *, status=200):
    c.bounded(data, c.MAX_OUTPUT)
    emitted = json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False))
    result = Response(data, status=status)
    result.accepted_renderer, result.accepted_media_type, result.renderer_context = (
        JSONRenderer(),
        'application/json',
        {},
    )
    result.render()
    result._confidence_expected = emitted
    check_response(result)
    return result


def check_response(result):
    if len(result.content) > c.MAX_OUTPUT:
        c.fail('TOO_LARGE', 422)
    if json.loads(result.content) != result._confidence_expected or result.data != result._confidence_expected:
        c.fail('INPUT_CHANGED')


def source(current, identifier, *, lock=False):
    project, actor = current[2], current[1]
    meta = (
        c.pending_query(project)
        .filter(pk=identifier)
        .values('pk', 'assignment_id', 'assignment__task_id', 'assignment__assignee_id', 'submitted_by_id')
        .first()
    )
    if meta is None:
        c.fail('INPUT_CHANGED')
    if actor.pk in (meta['assignment__assignee_id'], meta['submitted_by_id']):
        c.fail('FORBIDDEN', 403)
    if lock:
        for model, key in (
            (Task, meta['assignment__task_id']),
            (TaskAssignment, meta['assignment_id']),
            (Submission, identifier),
        ):
            query = model.objects.filter(pk=key)
            query = (
                query.filter(assignment__project=project, assignment__task__project=project)
                if model is Submission
                else query.filter(project=project)
            )
            list(query.select_for_update().values_list('pk', flat=True))
    loaded = c.load(c.pending_query(project).filter(pk=identifier), single=True)
    if len(loaded) != 1:
        c.fail('INPUT_CHANGED')
    row = loaded[0]
    if actor.pk in (row.assignment.assignee_id, row.submitted_by_id):
        c.fail('FORBIDDEN', 403)
    if not c.supported(row):
        c.fail('INPUT_UNSUPPORTED', 400)
    value, fingerprint = c.envelope(row, current)
    return row, value, fingerprint


def verify_source(request, initial, identifier, selection, expected, original_grant, *, lock=False):
    current = c.context(request, initial, producer=True)
    if c.grant(current, selection['profile_id']) != original_grant:
        c.fail('AUTHORITY_CHANGED', 403)
    profile = c.selected_profile(current[2], selection)
    if profile['descriptor']['producer_namespace'] != original_grant['producer_namespace']:
        c.fail('FORBIDDEN', 403)
    _, value, fingerprint = source(current, identifier, lock=lock)
    if (value, fingerprint) != expected:
        c.fail('INPUT_CHANGED')
    c.context(request, initial, producer=True)
    if c.grant(current, selection['profile_id']) != original_grant:
        c.fail('AUTHORITY_CHANGED', 403)


def input_response(request, initial, identifier, selection):
    original_grant = c.grant(initial, selection['profile_id'])
    profile = c.selected_profile(initial[2], selection)
    if profile['descriptor']['producer_namespace'] != original_grant['producer_namespace']:
        c.fail('FORBIDDEN', 403)
    _, value, fingerprint = source(initial, identifier)
    result = response(
        {**value, 'input_hash': fingerprint, 'profile': profile['descriptor'], 'profile_digest': profile['digest']}
    )
    verify_source(request, initial, identifier, selection, (value, fingerprint), original_grant)
    check_response(result)
    c.context(request, initial, producer=True)
    return result


def acknowledgement(row, replayed):
    return {
        'schema': c.RECEIPT,
        'observation_id': str(row.pk),
        'project_id': row.project_id,
        'event_uuid': str(row.event_uuid),
        'profile_id': str(row.profile_id),
        'profile_digest': row.profile_digest,
        'submission_id': row.submission_id_frozen,
        'request_hash': row.request_hash,
        'fact_hash': row.fact_hash,
        'receipt_hash': row.receipt_hash,
        'replayed': replayed,
    }


def create(request, initial, body, event):
    request_hash = c.digest(body)
    try:
        with transaction.atomic():
            project, actor = lock_project_operation(initial[2].pk, initial[0], PROVIDE_REVIEW_CONFIDENCE)
            current = c.context(request, initial, producer=True)
            access = c.grant(current, body['profile_id'])
            query = Observation.objects.filter(
                project=project,
                instance_id=c.instance_id(),
                producer_namespace=access['producer_namespace'],
                event_uuid=event,
            )
            previous = rows(query, lock=True)
            if previous:
                row = previous[0]
                if (
                    row.request_hash != request_hash
                    or row.creator_principal != current[0].principal_id
                    or row.creator_source != current[0].source
                    or row.actor_id_frozen != actor.pk
                ):
                    c.fail('KEY_CONFLICT')
                if row.proof['organization_id'] != project.organization_id:
                    c.fail('AUTHORITY_CHANGED', 403)
                result = response(acknowledgement(row, True))
                again = rows(query, lock=True)
                if len(again) != 1 or again[0].receipt_hash != row.receipt_hash:
                    c.fail()
                c.context(request, initial, producer=True)
                if c.grant(current, body['profile_id']) != access:
                    c.fail('AUTHORITY_CHANGED', 403)
                check_response(result)
                return result
            profile = c.selected_profile(project, body)
            if profile['descriptor']['producer_namespace'] != access['producer_namespace']:
                c.fail('FORBIDDEN', 403)
            source_row, value, fingerprint = source(current, body['submission_id'], lock=True)
            if fingerprint != body['evaluated_input_hash']:
                c.fail('INPUT_CHANGED')
            existing = Observation.objects.filter(
                project=project,
                instance_id=c.instance_id(),
                profile_id=body['profile_id'],
                profile_digest=body['profile_digest'],
                submission_id_frozen=source_row.pk,
            )
            if existing.exists():
                c.fail('BINDING_EXISTS')
            identifier, accepted = uuid4(), timezone.now()
            proof = {
                'schema': c.FACT,
                'observation_id': str(identifier),
                'instance_id': c.instance_id(),
                'organization_id': project.organization_id,
                'project_id': project.pk,
                'profile': profile['descriptor'],
                'profile_digest': profile['digest'],
                'subject': value['subject'],
                'input_hash': fingerprint,
                'score_units': body['score_units'],
                'producer_namespace': access['producer_namespace'],
                'event_uuid': str(event),
                'evaluated_at': body['evaluated_at'],
                'accepted_at': accepted.isoformat(),
            }
            c.bounded(proof, c.MAX_FACT)
            receipt = {
                'schema': c.RECEIPT,
                'observation_id': str(identifier),
                'owner': {'principal': current[0].principal_id, 'source': current[0].source, 'actor_id': actor.pk},
                'request_hash': request_hash,
                'fact_hash': c.digest(proof),
                'accepted_at': accepted.isoformat(),
            }
            c.bounded(receipt, c.MAX_FACT)
            row = Observation.objects.create(
                id=identifier,
                project=project,
                instance_id=c.instance_id(),
                producer_namespace=access['producer_namespace'],
                profile_id=body['profile_id'],
                profile_digest=profile['digest'],
                event_uuid=event,
                submission=source_row,
                submission_id_frozen=source_row.pk,
                created_by=actor,
                actor_id_frozen=actor.pk,
                creator_principal=current[0].principal_id,
                creator_source=current[0].source,
                request_hash=request_hash,
                proof=proof,
                receipt=receipt,
                fact_hash=c.digest(proof),
                receipt_hash=c.digest(receipt),
                created_at=accepted,
            )
            result = response(acknowledgement(row, False), status=201)
            verify_source(request, initial, source_row.pk, body, (value, fingerprint), access, lock=True)
            checked = rows(Observation.objects.filter(pk=row.pk, project=project), lock=True)
            if len(checked) != 1 or checked[0].receipt_hash != row.receipt_hash:
                c.fail()
            check_response(result)
            c.context(request, initial, producer=True)
            return result
    except IntegrityError as exc:
        name = getattr(getattr(exc.__cause__, 'diag', None), 'constraint_name', None)
        message = str(exc)
        event_columns = ('project_id', 'instance_id', 'producer_namespace', 'event_uuid')
        subject_columns = ('project_id', 'instance_id', 'profile_id', 'profile_digest', 'submission_id_frozen')
        for constraint, columns, code in (
            ('confidence_event_unique', event_columns, 'KEY_CONFLICT'),
            ('confidence_subject_unique', subject_columns, 'BINDING_EXISTS'),
        ):
            sqlite = 'UNIQUE constraint failed: ' + ', '.join(
                Observation._meta.db_table + '.' + column for column in columns
            )
            if name == constraint or message == sqlite:
                c.fail(code)
        raise
