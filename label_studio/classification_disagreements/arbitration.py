"""Local adoption of an existing frozen revision; never an approval operation."""

import copy

from access_control.authorization import authorization
from access_control.project_operations import (
    ARBITRATE,
    ORGANIZE_ARBITRATION,
    READ_WORK,
    project_operation_policy,
    require_project_operation,
)
from django.conf import settings
from django.db import connection, models
from django.db.models.expressions import RawSQL
from django.utils.crypto import salted_hmac
from organizations.models import OrganizationMember
from projects.models import Project, ProjectMember
from rest_framework.exceptions import APIException, NotFound
from rest_framework.generics import get_object_or_404
from review_batches.service import digest, fail
from tasks.my_work import work_projects
from users.models import User

from . import batches, service
from .models import ArbitrationDecision, ArbitrationTask, ClassificationBatch

SCHEMA = 'classification-arbitrations/v1'
RULE = 'adopt-exact-submission/v1'
MAX_TEXT_BYTES = 65536
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_PARTICIPANTS = 20
MAX_REASON = 2000


def error(code, status=409):
    fail('ARBITRATION_' + code, status)


def integer(value):
    return type(value) is int and 0 < value <= 2**53 - 1


def hash_value(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def viewer_hash(context):
    """Correlate read responses; never a bearer credential or persisted receipt."""
    principal, actor, project, role = context
    value = digest(
        {
            'principal': principal.principal_id,
            'source': principal.source,
            'actor': actor.pk,
            'project': project.pk,
            'organization': project.organization_id,
            'role': role,
        }
    )
    return salted_hmac(SCHEMA + '/viewer', value, algorithm='sha256').hexdigest()


def guard(pk, principal, actor, *, create=False, decide=False, cancel=False, capability=False):
    if not integer(pk):
        raise NotFound()
    project = get_object_or_404(
        Project.objects.filter(pk__in=work_projects(actor, ['manager', 'reviewer']), deleted_at__isnull=True), pk=pk
    )
    mode = f'{project.organization_id}:{project.pk}'
    maps = (
        settings.ANNOTATION_CLASSIFICATION_DISAGREEMENT_MODES,
        settings.ANNOTATION_CLASSIFICATION_ARBITRATION_MODES,
    )
    if (
        not settings.ANNOTATION_CLASSIFICATION_ARBITRATION_ENABLED
        or not settings.ANNOTATION_CLASSIFICATION_BATCHES_ENABLED
        or not settings.ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED
        or any(not isinstance(m, dict) or m.get(mode) != 'fork_review' for m in maps)
    ):
        raise NotFound()
    if connection.vendor not in ('postgresql', 'sqlite'):
        error('BACKEND_UNSUPPORTED', 503)
    role = authorization.project_role(principal, project)
    if (
        role not in ('manager', 'reviewer')
        or ((create or cancel) and role != 'manager')
        or (decide and role != 'reviewer')
    ):
        raise NotFound()
    if not cancel and not capability:
        require_project_operation(project, READ_WORK)
    if create:
        require_project_operation(project, ORGANIZE_ARBITRATION)
    if decide:
        require_project_operation(project, ARBITRATE)
    return project, role


def participants(item):
    return sorted(
        {
            v
            for e in item['cohort']
            for v in (e['assignee_id'], (e['source'] or {}).get('submitter_id'))
            if v is not None
        }
    )


def input_hash(project, item):
    return digest(
        {
            'project_id': project.pk,
            'task_id': item['task_id'],
            'rule': service.RULE,
            'selection': batches.SELECTION,
            'cohort': item['cohort'],
        }
    )


def origin(project, batch, task_id, expected):
    batches.checked(batch, project)
    if batch.manifest_hash != expected:
        error('ORIGIN_CHANGED')
    item = next((v for v in batch.manifest['tasks'] if v['task_id'] == task_id), None)
    if item is None:
        raise NotFound()
    if item['result']['outcome'] != 'disagreement' or item['selection_reason']:
        error('INELIGIBLE')
    return {
        'schema': SCHEMA,
        'project_id': project.pk,
        'organization_id': project.organization_id,
        'batch_id': batch.pk,
        'batch_name': batch.name,
        'batch_manifest_hash': batch.manifest_hash,
        'calculated_at': batch.manifest['calculated_at'],
        'metric_rule': service.RULE,
        'selection': batches.SELECTION,
        'decision_rule': RULE,
        'item': copy.deepcopy(item),
    }


def checked(row, project):
    try:
        o = row.origin
        valid = (
            row.origin_hash == digest(o)
            and o['schema'] == SCHEMA
            and row.project_id == o['project_id'] == project.pk
            and o['organization_id'] == project.organization_id
            and o['batch_id'] == row.batch_id_frozen
            and o['item']['task_id'] == row.task_id_frozen
            and o['metric_rule'] == service.RULE
            and o['selection'] == batches.SELECTION
            and o['decision_rule'] == RULE
            and row.input_set_hash == input_hash(project, o['item'])
            and row.participant_ids == participants(o['item'])
        )
        if row.status == 'cancelled':
            cancellation = {k: v for k, v in row.cancellation.items() if k != 'payload_hash'}
            valid = valid and (
                row.cancellation['payload_hash'] == digest(cancellation)
                and cancellation['input_set_hash'] == row.input_set_hash
                and cancellation['key'] == str(row.cancel_key)
            )
    except (TypeError, ValueError, KeyError, AttributeError):
        valid = False
    if not valid:
        error('ORIGIN_INVALID')
    return row


def scoped(project, actor, role):
    rows = ArbitrationTask.objects.filter(project=project)
    if role == 'reviewer':
        expression = (
            'NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(participant_ids) v WHERE v::bigint = %s)'
            if connection.vendor == 'postgresql'
            else 'NOT EXISTS (SELECT 1 FROM json_each(participant_ids) v WHERE v.value = %s)'
        )
        rows = (
            rows.filter(assignee_id_frozen=actor.pk)
            .annotate(_independent=RawSQL(expression, (actor.pk,), output_field=models.BooleanField()))
            .filter(_independent=True)
        )
    return rows.order_by('-pk')


def get_row(context, identifier, *, lock=False):
    if not integer(identifier):
        raise NotFound()
    _, actor, project, role = context
    rows = scoped(project, actor, role)
    if lock:
        rows = rows.select_for_update()
    return checked(get_object_or_404(rows, pk=identifier), project)


def assignee(project, identifier, item):
    user = eligible_assignees(project, item).filter(pk=identifier).first()
    if user is None:
        error('ASSIGNEE_UNAVAILABLE')
    return user


def eligible_assignees(project, item):
    members = ProjectMember.objects.filter(project=project, role='reviewer', enabled=True).values('user_id')
    organization = OrganizationMember.objects.filter(
        organization_id=project.organization_id, deleted_at__isnull=True
    ).values('user_id')
    return (
        User.objects.filter(pk__in=members, is_active=True, active_organization_id=project.organization_id)
        .filter(pk__in=organization)
        .exclude(pk=project.created_by_id)
        .exclude(pk__in=participants(item))
        .order_by('pk')
    )


def unavailable(reason):
    return {'available': False, 'reason': reason, 'texts': [], 'report': None}


def evidence(project, actor, role, o, *, lock=False):
    item = o['item']
    if not 2 <= len(item['cohort']) <= MAX_PARTICIPANTS:
        return unavailable('budget_exceeded')
    try:
        batch = get_object_or_404(ClassificationBatch, project=project, pk=o['batch_id'])
        if origin(project, batch, item['task_id'], o['batch_manifest_hash']) != o:
            return unavailable('source_unavailable')
        report, sources = batches.project_item_sources(batch, actor, role, item['task_id'], lock=lock)
        if report['outcome'] != 'disagreement' or report['status'] != 'comparable':
            return unavailable('source_unavailable')
        if sum(len(batches.canonical(row.result_snapshot)) for row in sources.values()) > MAX_SNAPSHOT_BYTES:
            return unavailable('budget_exceeded')
        first = sources[item['cohort'][0]['source']['submission_id']]
        schema = service.controls(first.result_snapshot['project']['label_config'])
        fields = sorted({(v['target'], v['field']) for v in schema.values()})
        texts = [{'name': name, 'text': first.result_snapshot['task']['data'][field]} for name, field in fields]
        if sum(len(v['text'].encode('utf-8')) for v in texts) > MAX_TEXT_BYTES:
            return unavailable('text_too_large')
        return {'available': True, 'reason': None, 'texts': texts, 'report': report}
    except (APIException, TypeError, ValueError, KeyError, AttributeError, service.Incomparable):
        return unavailable('source_unavailable')


def require_evidence(value):
    if not value['available']:
        error(
            'BUDGET_EXCEEDED' if value['reason'] in ('budget_exceeded', 'text_too_large') else 'SOURCE_UNAVAILABLE',
            422 if value['reason'] in ('budget_exceeded', 'text_too_large') else 409,
        )


def decision_payload(
    *,
    project_id,
    arbitration_id,
    actor_id_frozen,
    principal,
    created_at,
    input_set_hash,
    rule,
    winner,
    reason,
    idempotency_key,
    request_hash,
):
    return {
        'schema': SCHEMA,
        'project_id': project_id,
        'arbitration_id': arbitration_id,
        'actor_id': actor_id_frozen,
        'principal': principal,
        'created_at': created_at.isoformat(),
        'input_set_hash': input_set_hash,
        'rule': rule,
        'winner': winner,
        'reason': reason,
        'key': str(idempotency_key),
        'request_hash': request_hash,
    }


def checked_decision(decision, row):
    args = {
        k: getattr(decision, k)
        for k in (
            'project_id',
            'arbitration_id',
            'actor_id_frozen',
            'principal',
            'created_at',
            'input_set_hash',
            'rule',
            'winner',
            'reason',
            'idempotency_key',
            'request_hash',
        )
    }
    if (
        row.status != 'resolved'
        or decision.project_id != row.project_id
        or decision.input_set_hash != row.input_set_hash
        or decision.actor_id_frozen != row.assignee_id_frozen
        or decision.rule != RULE
        or decision.winner not in [e['source'] for e in row.origin['item']['cohort']]
        or digest(decision_payload(**args)) != decision.payload_hash
    ):
        error('DECISION_INVALID')
    return decision


def meta(row, context):
    _, actor, project, role = context
    policy = project_operation_policy(project)
    data = {
        'id': row.pk,
        'viewer_hash': viewer_hash(context),
        'schema': SCHEMA,
        'decision_rule': RULE,
        'batch_id': row.batch_id_frozen,
        'task_id': row.task_id_frozen,
        'batch_name': row.origin['batch_name'],
        'created_at': row.created_at.isoformat(),
        'calculated_at': row.origin['calculated_at'],
        'assignee_id': row.assignee_id_frozen,
        'input_set_hash': row.input_set_hash,
        'status': row.status,
        'version': row.version,
        'can_decide': row.status == 'pending'
        and role == 'reviewer'
        and row.assignee_id_frozen == actor.pk
        and actor.pk not in row.participant_ids
        and policy.allows(ARBITRATE),
        'can_cancel': row.status == 'pending' and role == 'manager',
    }
    if role == 'manager':
        data['origin_manifest_hash'] = row.origin['batch_manifest_hash']
    return data


def detail(row, context):
    value = evidence(context[2], context[1], context[3], row.origin)
    decision = ArbitrationDecision.objects.filter(arbitration=row).first()
    if (row.status == 'resolved') != (decision is not None):
        error('DECISION_INVALID')
    decided = None
    if decision:
        checked_decision(decision, row)
        decided = {
            'id': decision.pk,
            'actor_id': decision.actor_id_frozen,
            'created_at': decision.created_at.isoformat(),
            'winner_submission_id': decision.winner['submission_id'],
            'winner_revision': decision.winner['revision'],
            'reason': decision.reason,
            'rule': decision.rule,
        }
    cancelled = {k: row.cancellation[k] for k in ('actor_id', 'created_at', 'reason')} if row.cancellation else None
    return {**meta(row, context), **value, 'decision': decided, 'cancellation': cancelled}
