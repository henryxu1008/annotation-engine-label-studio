"""Independent, immutable adoption of one frozen bbox submission.

These services do not approve a review or publish a dataset. Public API and
browser integration require separate authorization and negative-path testing.
"""

from uuid import UUID

from access_control.authorization import authorization
from access_control.project_operations import (
    ARBITRATE_BBOX,
    ORGANIZE_BBOX_ARBITRATION,
    READ_WORK,
    SECURITY_CANCEL,
    lock_project_operation,
    require_project_operation,
)
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound
from tasks.models import Submission, Task, TaskAssignment

from . import arbitration as common
from . import bbox, bbox_batches, bbox_quality, ner, ner_api, ner_arbitration, ner_batches
from .models import BboxArbitrationDecision, BboxArbitrationTask

SCHEMA = 'bbox-arbitration/v1'
RULE = 'adopt-exact-bbox-submission/v1'
MAX_ORIGIN = 1024 * 1024
JSON_LIMITS = {'origin': (MAX_ORIGIN, 4 * MAX_ORIGIN), 'participant_ids': (1024, 4096),
               'cancellation': (16 * 1024, 64 * 1024), 'winner': (4096, 16384)}


def rows(project):
    return ner_arbitration.guarded(BboxArbitrationTask.objects.filter(project=project),
                                   ('origin', 'participant_ids', 'cancellation'), limits=JSON_LIMITS)


def decisions(project):
    return ner_arbitration.guarded(BboxArbitrationDecision.objects.filter(project=project),
                                   ('winner', 'reason'), limits=JSON_LIMITS)


def guard(project, principal, *, role, operation=None):
    key = f'{project.organization_id}:{project.pk}'
    quality, media, arbitration = (settings.ANNOTATION_BBOX_QUALITY_MODES, settings.ANNOTATION_BBOX_MEDIA_PROJECTS,
                                   settings.ANNOTATION_BBOX_ARBITRATION_MODES)
    if (connection.vendor not in ('postgresql', 'sqlite') or not settings.ANNOTATION_BBOX_QUALITY_ENABLED
            or not settings.ANNOTATION_BBOX_MEDIA_ENABLED or not settings.ANNOTATION_BBOX_ARBITRATION_ENABLED
            or any(not isinstance(value, dict) for value in (quality, media, arbitration))
            or quality.get(key) != 'fork_review' or media.get(key) != 'managed'
            or arbitration.get(key) != 'fork_review' or authorization.project_role(principal, project) != role):
        raise NotFound()
    if operation != SECURITY_CANCEL:
        require_project_operation(project, READ_WORK)
        require_project_operation(project, operation)


def frozen_origin(project, batch, task_id, expected_hash):
    batch = bbox_batches.checked(batch, project)
    if batch.manifest_hash != expected_hash:
        raise bbox.Error('BBOX_ARBITRATION_ORIGIN_CHANGED', 409)
    plan = batch.manifest['plan']
    item = next((entry for entry in plan['items'] if entry['task_id'] == task_id), None)
    if item is None:
        raise NotFound()
    filtered = {**plan, 'task_ids': [task_id], 'items': [item]}
    bbox_quality.replay(project, filtered)
    result = item['result']
    pairs = result.get('pairs') if result.get('availability') == 'available' else None
    if not pairs or not any(pair['f1']['numerator'] != pair['f1']['denominator'] for pair in pairs):
        raise bbox.Error('BBOX_ARBITRATION_INELIGIBLE', 409)
    origin = {'schema': SCHEMA, 'rule': RULE, 'project_id': project.pk, 'organization_id': project.organization_id,
              'batch_id': batch.pk, 'batch_hash': batch.manifest_hash, 'item': item}
    bbox.bounded(origin, MAX_ORIGIN)
    return origin


def key_value(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise bbox.Error('BBOX_ARBITRATION_INVALID_KEY', 400) from exc


def input_hash(project, item):
    return bbox.digest({'project_id': project.pk, 'task_id': item['task_id'], 'rule': bbox.RULE,
                        'selection': ner_batches.SELECTION, 'cohort': item['cohort']})


def lock_sources(project, item):
    task = Task.objects.select_for_update().get(pk=item['task_id'], project=project)
    assignments = [entry['assignment_id'] for entry in item['cohort']]
    submissions = [entry['source']['submission_id'] for entry in item['cohort'] if entry['source']]
    list(TaskAssignment.objects.filter(pk__in=assignments).select_for_update().order_by('pk').values_list('pk', flat=True))
    list(Submission.objects.filter(pk__in=submissions).select_for_update().order_by('pk').values_list('pk', flat=True))
    return task


def create(project, principal, *, batch_id, task_id, expected_hash, assignee_id, idempotency_key):
    guard(project, principal, role='manager', operation=ORGANIZE_BBOX_ARBITRATION)
    if (any(type(value) is not int or value <= 0 for value in (batch_id, task_id, assignee_id))
            or not isinstance(expected_hash, str) or not ner_api.HEX.fullmatch(expected_hash)):
        raise bbox.Error('BBOX_ARBITRATION_INVALID_INPUT', 400)
    key = key_value(idempotency_key)
    request_hash = bbox.digest({'batch_id': batch_id, 'task_id': task_id, 'expected_hash': expected_hash,
                                'assignee_id': assignee_id})
    with transaction.atomic():
        project, actor = lock_project_operation(project.pk, principal, ORGANIZE_BBOX_ARBITRATION,
                                                related_user_ids=[assignee_id])
        guard(project, principal, role='manager', operation=ORGANIZE_BBOX_ARBITRATION)
        previous = rows(project).filter(idempotency_key=key).first()
        if previous:
            checked(project, previous)
            if (previous.request_hash != request_hash or previous.actor_id_frozen != actor.pk
                    or previous.creator_principal != principal.principal_id or previous.creator_source != principal.source):
                raise bbox.Error('BBOX_ARBITRATION_KEY_CONFLICT', 409)
            return previous, True
        batch = bbox_batches.query(project).filter(pk=batch_id).first()
        if batch is None:
            raise NotFound()
        origin = frozen_origin(project, batch, task_id, expected_hash)
        item = origin['item']
        if not common.eligible_assignees(project, item).filter(pk=assignee_id).exists():
            raise bbox.Error('BBOX_ARBITRATION_ASSIGNEE_INVALID', 400)
        lock_sources(project, item)
        origin = frozen_origin(project, batch, task_id, expected_hash)
        if not common.eligible_assignees(project, item).filter(pk=assignee_id).exists():
            raise bbox.Error('BBOX_ARBITRATION_ASSIGNEE_CHANGED', 409)
        frozen_input_hash = input_hash(project, item)
        if BboxArbitrationTask.objects.filter(project=project, task_id_frozen=task_id, input_hash=frozen_input_hash,
                                              status__in=['pending', 'resolved']).exists():
            raise bbox.Error('BBOX_ARBITRATION_ALREADY_EXISTS', 409)
        row = BboxArbitrationTask.objects.create(
            project=project, batch_id=batch_id, task_id_frozen=task_id, created_by=actor, actor_id_frozen=actor.pk,
            creator_principal=principal.principal_id, creator_source=principal.source,
            assignee_id=assignee_id, assignee_id_frozen=assignee_id, input_hash=frozen_input_hash,
            origin=origin, origin_hash=bbox.digest(origin), participant_ids=common.participants(item),
            idempotency_key=key, request_hash=request_hash,
        )
        return row, False


def checked(project, row):
    try:
        ner_arbitration.hydrate(row, ('origin', 'participant_ids', 'cancellation'), limits=JSON_LIMITS)
        origin = row.origin
        bbox.bounded(origin, MAX_ORIGIN)
        item = origin['item']
        expected_input = input_hash(project, item)
        if (bbox.digest(origin) != row.origin_hash or origin['schema'] != SCHEMA or origin['rule'] != RULE
                or origin['project_id'] != project.pk or origin['organization_id'] != project.organization_id
                or origin['batch_id'] != row.batch_id or item['task_id'] != row.task_id_frozen
                or row.input_hash != expected_input or row.participant_ids != common.participants(item)
                or row.assignee_id_frozen in row.participant_ids or row.assignee_id_frozen == row.actor_id_frozen):
            raise ValueError('invalid origin')
        if row.status == 'cancelled':
            cancellation = row.cancellation
            payload = {key: value for key, value in cancellation.items() if key != 'payload_hash'}
            if (bbox.digest(payload) != cancellation['payload_hash'] or payload['schema'] != SCHEMA
                    or payload['arbitration_id'] != row.pk or payload['project_id'] != project.pk
                    or payload['input_hash'] != row.input_hash or payload['idempotency_key'] != str(row.cancel_key)
                    or payload['request_hash'] != row.cancel_request_hash):
                raise ValueError('invalid cancellation')
        return row
    except (ValueError, TypeError, KeyError, AttributeError, bbox.Error, ner.Error) as exc:
        raise bbox.Error('BBOX_ARBITRATION_ORIGIN_INVALID', 409) from exc


def decide(project, principal, *, arbitration_id, winner_submission_id, reason, idempotency_key,
           expected_version=None, expected_input_set_hash=None):
    guard(project, principal, role='reviewer', operation=ARBITRATE_BBOX)
    if (type(arbitration_id) is not int or arbitration_id <= 0
            or type(winner_submission_id) is not int or winner_submission_id <= 0):
        raise bbox.Error('BBOX_ARBITRATION_INVALID_INPUT', 400)
    reason = ner_batches.validated_text(reason, 2000, trim=True)
    if not reason:
        raise bbox.Error('BBOX_ARBITRATION_REASON_REQUIRED', 400)
    key = key_value(idempotency_key)
    request_hash = bbox.digest({'arbitration_id': arbitration_id, 'winner_submission_id': winner_submission_id,
                                'reason': reason, 'expected_version': expected_version,
                                'expected_input_set_hash': expected_input_set_hash})
    if ((expected_version is None) != (expected_input_set_hash is None)
            or expected_version is not None and (type(expected_version) is not int or expected_version < 1
                or not isinstance(expected_input_set_hash, str) or not ner_api.HEX.fullmatch(expected_input_set_hash))):
        raise bbox.Error('BBOX_ARBITRATION_INVALID_INPUT', 400)
    initial = rows(project).filter(pk=arbitration_id).first()
    if initial is None:
        raise NotFound()
    with transaction.atomic():
        project, actor = lock_project_operation(project.pk, principal, ARBITRATE_BBOX)
        guard(project, principal, role='reviewer', operation=ARBITRATE_BBOX)
        initial = checked(project, initial)
        lock_sources(project, initial.origin['item'])
        row = checked(project, rows(project).select_for_update().get(pk=arbitration_id))
        if actor.pk != row.assignee_id_frozen or actor.pk in row.participant_ids:
            raise NotFound()
        previous = decisions(project).filter(idempotency_key=key).first()
        if previous:
            if (previous.arbitration_id != row.pk or previous.actor_id_frozen != actor.pk
                    or previous.principal != principal.principal_id or previous.source != principal.source
                    or previous.request_hash != request_hash):
                raise bbox.Error('BBOX_ARBITRATION_KEY_CONFLICT', 409)
            checked_decision(previous, row)
            return previous, True
        if (row.status != 'pending' or row.version != 1
                or expected_version is not None and (row.version != expected_version
                                                      or row.input_hash != expected_input_set_hash)):
            raise bbox.Error('BBOX_ARBITRATION_STATE_CHANGED', 409)
        batch = bbox_batches.query(project).filter(pk=row.batch_id).first()
        if batch is None or frozen_origin(project, batch, row.task_id_frozen, row.origin['batch_hash']) != row.origin:
            raise bbox.Error('BBOX_ARBITRATION_SOURCE_CHANGED', 409)
        winner = next((entry['source'] for entry in row.origin['item']['cohort']
                       if entry['source'] and entry['source']['submission_id'] == winner_submission_id), None)
        if winner is None:
            raise bbox.Error('BBOX_ARBITRATION_WINNER_INVALID', 400)
        created_at = timezone.now()
        payload = {'schema': SCHEMA, 'arbitration_id': row.pk, 'project_id': project.pk,
                   'actor_id': actor.pk, 'principal': principal.principal_id, 'source': principal.source,
                   'input_hash': row.input_hash, 'winner': winner, 'reason': reason,
                   'idempotency_key': str(key), 'request_hash': request_hash, 'created_at': created_at.isoformat()}
        decision = BboxArbitrationDecision.objects.create(
            project=project, arbitration=row, actor=actor, actor_id_frozen=actor.pk, principal=principal.principal_id,
            source=principal.source, input_hash=row.input_hash, winner=winner, reason=reason, idempotency_key=key,
            request_hash=request_hash, payload_hash=bbox.digest(payload), created_at=created_at,
        )
        row.status, row.version = 'resolved', 2
        row.save(update_fields=['status', 'version'])
        return decision, False


def checked_decision(decision, row):
    try:
        ner_arbitration.hydrate(decision, ('winner', 'reason'), limits=JSON_LIMITS)
    except ner.Error as exc:
        raise bbox.Error('BBOX_ARBITRATION_DECISION_INVALID', 409) from exc
    payload = {'schema': SCHEMA, 'arbitration_id': row.pk, 'project_id': row.project_id,
               'actor_id': decision.actor_id_frozen, 'principal': decision.principal, 'source': decision.source,
               'input_hash': decision.input_hash, 'winner': decision.winner, 'reason': decision.reason,
               'idempotency_key': str(decision.idempotency_key), 'request_hash': decision.request_hash,
               'created_at': decision.created_at.isoformat()}
    if (decision.arbitration_id != row.pk or decision.project_id != row.project_id
            or decision.actor_id_frozen != row.assignee_id_frozen or decision.input_hash != row.input_hash
            or decision.winner not in [entry['source'] for entry in row.origin['item']['cohort']]
            or bbox.digest(payload) != decision.payload_hash or not decision.reason.strip()):
        raise bbox.Error('BBOX_ARBITRATION_DECISION_INVALID', 409)
    return decision


def cancel(project, principal, *, arbitration_id, reason, idempotency_key,
           expected_version=None, expected_input_set_hash=None):
    guard(project, principal, role='manager', operation=SECURITY_CANCEL)
    if type(arbitration_id) is not int or arbitration_id <= 0:
        raise bbox.Error('BBOX_ARBITRATION_INVALID_INPUT', 400)
    reason = ner_batches.validated_text(reason, 2000, trim=True)
    if not reason:
        raise bbox.Error('BBOX_ARBITRATION_REASON_REQUIRED', 400)
    key = key_value(idempotency_key)
    if ((expected_version is None) != (expected_input_set_hash is None)
            or expected_version is not None and (type(expected_version) is not int or expected_version < 1
                or not isinstance(expected_input_set_hash, str) or not ner_api.HEX.fullmatch(expected_input_set_hash))):
        raise bbox.Error('BBOX_ARBITRATION_INVALID_INPUT', 400)
    request_hash = bbox.digest({'arbitration_id': arbitration_id, 'reason': reason, 'expected_version': expected_version,
                                'expected_input_set_hash': expected_input_set_hash})
    initial = rows(project).filter(pk=arbitration_id).first()
    if initial is None:
        raise NotFound()
    with transaction.atomic():
        project, actor = lock_project_operation(project.pk, principal, SECURITY_CANCEL)
        guard(project, principal, role='manager', operation=SECURITY_CANCEL)
        list(Task.objects.filter(pk=initial.task_id_frozen, project=project).select_for_update().values_list('pk', flat=True))
        row = checked(project, rows(project).select_for_update().get(pk=arbitration_id))
        previous = rows(project).filter(cancel_key=key).first()
        if previous:
            previous = checked(project, previous)
            if (previous.pk != row.pk or previous.cancel_request_hash != request_hash
                    or previous.cancellation.get('actor_id') != actor.pk
                    or previous.cancellation.get('principal') != principal.principal_id
                    or previous.cancellation.get('source') != principal.source):
                raise bbox.Error('BBOX_ARBITRATION_KEY_CONFLICT', 409)
            return previous, True
        if (row.status != 'pending' or row.version != 1
                or expected_version is not None and (row.version != expected_version
                                                      or row.input_hash != expected_input_set_hash)):
            raise bbox.Error('BBOX_ARBITRATION_STATE_CHANGED', 409)
        payload = {'schema': SCHEMA, 'arbitration_id': row.pk, 'project_id': project.pk, 'actor_id': actor.pk,
                   'principal': principal.principal_id, 'source': principal.source, 'input_hash': row.input_hash,
                   'reason': reason, 'cancelled_at': timezone.now().isoformat(), 'idempotency_key': str(key),
                   'request_hash': request_hash}
        row.cancellation = {**payload, 'payload_hash': bbox.digest(payload)}
        row.status, row.version, row.cancel_key, row.cancel_request_hash = 'cancelled', 2, key, request_hash
        row.save(update_fields=['status', 'version', 'cancel_key', 'cancel_request_hash', 'cancellation'])
        return row, False
