"""Typed NER revision adoption. No annotation, review or release writes."""

import copy
from types import SimpleNamespace

from access_control.authorization import authorization
from access_control.project_operations import (
    ARBITRATE_NER,
    ORGANIZE_NER_ARBITRATION,
    READ_WORK,
    project_operation_policy,
    require_project_operation,
)
from django.conf import settings
from django.db import connection, models
from django.db.models import Case, F, Value, When
from django.db.models.expressions import RawSQL
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.crypto import salted_hmac
from projects.models import Project
from rest_framework.exceptions import NotFound
from tasks.models import Submission, Task, TaskAssignment
from tasks.my_work import work_projects

from . import ner
from . import ner_batches as b
from .arbitration import eligible_assignees as independent_reviewers
from .arbitration import hash_value, integer, participants
from .models import NerArbitrationDecision, NerArbitrationTask

SCHEMA = 'ner-arbitrations/v1'
RULE = 'adopt-exact-ner-submission/v1'
INPUT_SCHEMA = 'ner-arbitration-input/v1'
MAX_REASON = 2000
MAX_COMMAND = 16 * 1024
MAX_PARTICIPANTS, MAX_CALLS = 20, 380
# These are immutable v1 storage envelopes, not mutable processing ceilings.
JSON_LIMITS = {
    'origin': (64 * 1024, 256 * 1024),
    'participant_ids': (1024, 4096),
    'cancellation': (16 * 1024, 64 * 1024),
    'winner': (2048, 8192),
}


def error(code, status=409):
    raise ner.Error('NER_ARBITRATION_' + code, status)


def bounded(value, maximum):
    try:
        ner.bounded(value, maximum)
    except (ner.Error, UnicodeError, TypeError, ValueError):
        error('BUDGET_EXCEEDED', 422)


def viewer_hash(context):
    p, actor, project, role = context
    return salted_hmac(
        SCHEMA + '/viewer',
        ner.digest(
            {
                'principal': p.principal_id,
                'source': p.source,
                'actor': actor.pk,
                'organization': project.organization_id,
                'project': project.pk,
                'role': role,
            }
        ),
        algorithm='sha256',
    ).hexdigest()


def guard(pk, principal, actor, *, create=False, decide=False, cancel=False, capability=False):
    if not integer(pk):
        raise NotFound()
    project = get_object_or_404(
        Project.objects.filter(pk__in=work_projects(actor, ['manager', 'reviewer']), deleted_at__isnull=True), pk=pk
    )
    maps = (settings.ANNOTATION_NER_DISAGREEMENT_MODES, settings.ANNOTATION_NER_ARBITRATION_MODES)
    if (
        not settings.ANNOTATION_NER_DISAGREEMENTS_ENABLED
        or not settings.ANNOTATION_NER_BATCHES_ENABLED
        or not settings.ANNOTATION_NER_ARBITRATION_ENABLED
        or any(not isinstance(m, dict) or m.get(f'{project.organization_id}:{pk}') != 'fork_review' for m in maps)
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
        require_project_operation(project, ORGANIZE_NER_ARBITRATION)
    if decide:
        require_project_operation(project, ARBITRATE_NER)
    return project, role


def guarded(query, fields):
    """SQL byte gates run before Django JSON/Text decoding, including TOCTOU growth."""
    table = connection.ops.quote_name(query.model._meta.db_table)
    for field in fields:
        stored = JSON_LIMITS[field][1] if field in JSON_LIMITS else 16 * 1024
        column = table + '.' + connection.ops.quote_name(field)
        size = (
            f'OCTET_LENGTH(CAST({column} AS TEXT))'
            if connection.vendor == 'postgresql'
            else f'LENGTH(CAST({column} AS BLOB))'
        )
        query = query.annotate(**{f'_size_{field}': RawSQL(size, [], output_field=models.BigIntegerField())})
        output = models.JSONField() if field in JSON_LIMITS else models.TextField()
        query = query.annotate(
            **{
                f'_safe_{field}': Case(
                    When(**{f'_size_{field}__lte': stored}, then=F(field)), default=Value(None), output_field=output
                )
            }
        ).defer(field)
    return query


def hydrate(row, fields):
    for field in fields:
        if hasattr(row, f'_safe_{field}'):
            size = getattr(row, f'_size_{field}')
            if size is not None and (
                type(size) is not int
                or size < 0
                or size > (JSON_LIMITS[field][1] if field in JSON_LIMITS else 16 * 1024)
            ):
                error('LEDGER_INVALID')
            setattr(row, field, getattr(row, f'_safe_{field}'))
        bounded(getattr(row, field), JSON_LIMITS[field][0] if field in JSON_LIMITS else 16 * 1024)
    return row


def rows(project):
    return guarded(NerArbitrationTask.objects.filter(project=project), ('origin', 'participant_ids', 'cancellation'))


def scoped(project, actor, role):
    query = rows(project)
    if role == 'reviewer':
        # Bound/type-check before expanding. Invalid membership cannot admit a row.
        if connection.vendor == 'postgresql':
            safe = "CASE WHEN OCTET_LENGTH(participant_ids::text)<=4096 AND jsonb_typeof(participant_ids)='array' THEN participant_ids ELSE 'null'::jsonb END"
            expression = f"jsonb_typeof({safe})='array' AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof({safe})='array' THEN {safe} ELSE '[]'::jsonb END) v WHERE jsonb_typeof(v)<>'number' OR v::text=%s)"
            args = (str(actor.pk),)
        else:
            safe = "CASE WHEN LENGTH(CAST(participant_ids AS BLOB))<=4096 AND json_type(participant_ids)='array' THEN participant_ids ELSE 'null' END"
            expression = f"json_type({safe})='array' AND NOT EXISTS (SELECT 1 FROM json_each({safe}) v WHERE v.type<>'integer' OR v.value=%s)"
            args = (actor.pk,)
        query = (
            query.filter(assignee_id_frozen=actor.pk)
            .exclude(actor_id_frozen=actor.pk)
            .annotate(_independent=RawSQL(expression, args, output_field=models.BooleanField()))
            .filter(_independent=True)
        )
    return query.order_by('-pk')


def input_hash(project, item):
    return ner.digest(
        {
            'schema': INPUT_SCHEMA,
            'organization_id': project.organization_id,
            'project_id': project.pk,
            'source_schema': b.SCHEMA,
            'selection': b.SELECTION,
            'rule': ner.RULE,
            'explanation': ner.EXPLANATION,
            'engine': ner.ENGINE,
            'item': item,
        }
    )


def origin(project, batch, task_id, expected):
    b.checked(batch, project)
    if batch.manifest_hash != expected:
        error('ORIGIN_CHANGED')
    item = next((i for i in batch.manifest['tasks'] if i['task_id'] == task_id), None)
    if item is None:
        raise NotFound()
    if item['result']['outcome'] != 'disagreement' or item['selection_reason']:
        error('INELIGIBLE')
    value = {
        'schema': SCHEMA,
        'source_kind': 'ner_batch',
        'source_schema': b.SCHEMA,
        'project_id': project.pk,
        'organization_id': project.organization_id,
        'ner_batch_id': batch.pk,
        'batch_name': batch.name,
        'batch_manifest_hash': batch.manifest_hash,
        'calculated_at': batch.manifest['calculated_at'],
        'scope_enumerated_at': batch.manifest['scope_enumerated_at'],
        'selection': b.SELECTION,
        'metric_rule': ner.RULE,
        'explanation': ner.EXPLANATION,
        'engine': ner.ENGINE,
        'decision_rule': RULE,
        'item': copy.deepcopy(item),
    }
    bounded(value, JSON_LIMITS['origin'][0])
    return value


def checked(row, project):
    hydrate(row, ('origin', 'participant_ids', 'cancellation'))
    try:
        o = row.origin
        keys = {
            'schema',
            'source_kind',
            'source_schema',
            'project_id',
            'organization_id',
            'ner_batch_id',
            'batch_name',
            'batch_manifest_hash',
            'calculated_at',
            'scope_enumerated_at',
            'selection',
            'metric_rule',
            'explanation',
            'engine',
            'decision_rule',
            'item',
        }
        valid = (
            set(o) == keys
            and o['schema'] == SCHEMA
            and o['source_kind'] == 'ner_batch'
            and o['source_schema'] == b.SCHEMA
            and o['decision_rule'] == RULE
            and o['project_id'] == row.project_id == project.pk
            and o['organization_id'] == project.organization_id
            and o['ner_batch_id'] == row.ner_batch_id_frozen
            and o['item']['task_id'] == row.task_id_frozen
            and o['metric_rule'] == ner.RULE
            and o['selection'] == b.SELECTION
            and o['explanation'] == ner.EXPLANATION
            and o['engine'] == ner.ENGINE
            and hash_value(o['batch_manifest_hash'])
            and row.origin_hash == ner.digest(o)
            and row.input_set_hash == input_hash(project, o['item'])
            and isinstance(row.participant_ids, list)
            and all(integer(v) for v in row.participant_ids)
            and row.participant_ids == participants(o['item'])
            and row.assignee_id_frozen not in row.participant_ids
            and row.assignee_id_frozen != row.actor_id_frozen
            and integer(row.assignee_id_frozen)
            and integer(row.actor_id_frozen)
            and hash_value(row.request_hash)
            and bool(row.creator_principal)
            and bool(row.creator_source)
        )
        # Reuse the fixed NER v1 item validator without reading a source or applying current processing budgets.
        m = {
            'selection': b.SELECTION,
            'rule': ner.RULE,
            'explanation': ner.EXPLANATION,
            'engine': ner.ENGINE,
            'task_ids': [row.task_id_frozen],
            'tasks': [o['item']],
            'work_units': 0,
            'scorer_calls_per_pass': o['item']['result']['comparable_pairs'],
            'schema': b.SCHEMA,
            'project_id': project.pk,
            'organization_id': project.organization_id,
            'creator_principal': row.creator_principal,
            'creator_source': row.creator_source,
            'actor_id_frozen': row.actor_id_frozen,
            'name': o['batch_name'],
            'scope_enumerated_at': o['scope_enumerated_at'],
            'calculated_at': o['calculated_at'],
        }
        b.checked(
            SimpleNamespace(
                manifest=m,
                manifest_hash=ner.digest(m),
                creator_principal=row.creator_principal,
                actor_id_frozen=row.actor_id_frozen,
                name=o['batch_name'],
            ),
            project,
        )
        valid = valid and o['item']['result']['outcome'] == 'disagreement' and o['item']['selection_reason'] is None
        if row.status == 'cancelled':
            c = row.cancellation
            valid = valid and (
                row.version == 2
                and c['key'] == str(row.cancel_key)
                and c['input_set_hash'] == row.input_set_hash
                and c['request_hash'] == row.cancel_request_hash
                and c['schema'] == SCHEMA
                and c['project_id'] == project.pk
                and c['arbitration_id'] == row.pk
                and integer(c['actor_id'])
                and bool(c['principal'])
                and bool(c['source'])
                and isinstance(c['reason'], str)
                and 1 <= len(c['reason']) <= MAX_REASON
                and c['reason'] == c['reason'].strip()
                and not timezone.is_naive(timezone.datetime.fromisoformat(c['created_at']))
                and c['payload_hash'] == ner.digest({k: v for k, v in c.items() if k != 'payload_hash'})
            )
        else:
            valid = (
                valid
                and row.status in ('pending', 'resolved')
                and row.version == (1 if row.status == 'pending' else 2)
            )
            valid = valid and row.cancellation is None and row.cancel_key is None and not row.cancel_request_hash
    except (KeyError, ValueError, TypeError, AttributeError, ner.Error):
        valid = False
    if not valid:
        error('LEDGER_INVALID')
    d = decisions(project).filter(arbitration_id=row.pk).first()
    if (row.status == 'resolved') != (d is not None):
        error('DECISION_INVALID')
    if d:
        checked_decision(d, row)
    return row


def get_row(context, identifier, *, lock=False):
    if not integer(identifier):
        raise NotFound()
    query = scoped(context[2], context[1], context[3])
    if lock:
        query = query.select_for_update()
    return checked(get_object_or_404(query, pk=identifier), context[2])


def eligible_assignees(context, item):
    return independent_reviewers(context[2], item).exclude(pk=context[1].pk)


def assignee(context, identifier, item):
    target = eligible_assignees(context, item).filter(pk=identifier).first()
    if target is None:
        error('ASSIGNEE_UNAVAILABLE')
    return target


def evidence(context, frozen, *, pair=None, lock=False):
    project, item = context[2], frozen['item']
    count = len(item['cohort'])
    if count > min(MAX_PARTICIPANTS, ner.MAX_INPUTS) or 2 * item['result']['comparable_pairs'] > min(
        MAX_CALLS, b.MAX_CALLS
    ):
        error('BUDGET_EXCEEDED', 422)
    unavailable = {'source_status': 'unavailable', 'reason': 'source_unavailable', 'comparison': None, 'inputs': []}
    query = b.query_batches(project)
    if lock:
        query = query.select_for_update()
    batch = query.filter(pk=frozen['ner_batch_id']).first()
    if batch is None:
        return unavailable, {}
    try:
        if origin(project, batch, item['task_id'], frozen['batch_manifest_hash']) != frozen:
            return unavailable, {}
    except (ner.Error, NotFound):
        return unavailable, {}
    if lock:
        for model, ids in (
            (Task, [item['task_id']]),
            (TaskAssignment, [e['assignment_id'] for e in item['cohort']]),
            (Submission, [e['source']['submission_id'] for e in item['cohort']]),
        ):
            list(model.objects.filter(pk__in=ids).select_for_update().order_by('pk').values_list('pk', flat=True))
    # The original full manifest has been validated. Hydrate only the authorized target cohort.
    target = copy.copy(batch)
    target.manifest = {**batch.manifest, 'tasks': [item]}
    projected, prepared = b.project_items(target, context)
    if not projected or projected[0]['source_status'] != 'verified':
        return unavailable, {}
    row = projected[0]
    if row['outcome'] != 'disagreement':
        return unavailable, {}
    inputs = row['inputs']
    selected = tuple(sorted(pair)) if pair else tuple(p['submission_id'] for p in inputs[:2])
    if len(set(selected)) != 2 or not set(selected).issubset(p['submission_id'] for p in inputs):
        error('INVALID_INPUT', 400)
    value = {
        'source_status': 'verified',
        'reason': None,
        'comparison': ner.public(row['_prepared'], selected),
        'inputs': inputs,
    }
    bounded(value, ner.MAX_OUTPUT)
    return value, prepared


def require_evidence(value):
    if value['source_status'] != 'verified':
        error('SOURCE_UNAVAILABLE')


def check_engine(prepared):
    calls = sum(
        sum(bool(a or c) for n, a in enumerate(v['engine_inputs']) for c in v['engine_inputs'][n + 1 :])
        for v in prepared.values()
    )
    if 2 * calls > MAX_CALLS:
        error('BUDGET_EXCEEDED', 422)
    b.check_engine(prepared)


def decision_payload(**values):
    return {
        'schema': SCHEMA,
        **{
            k: (v.isoformat() if k == 'created_at' else str(v) if k == 'idempotency_key' else v)
            for k, v in values.items()
        },
    }


def decisions(project):
    return guarded(NerArbitrationDecision.objects.filter(project=project), ('winner', 'reason'))


def checked_decision(d, row):
    hydrate(d, ('winner', 'reason'))
    keys = (
        'project_id',
        'arbitration_id',
        'actor_id_frozen',
        'principal',
        'source',
        'created_at',
        'input_set_hash',
        'rule',
        'winner',
        'reason',
        'idempotency_key',
        'request_hash',
    )
    if (
        row.status != 'resolved'
        or d.project_id != row.project_id
        or d.arbitration_id != row.pk
        or d.input_set_hash != row.input_set_hash
        or d.actor_id_frozen != row.assignee_id_frozen
        or d.rule != RULE
        or d.winner not in [e['source'] for e in row.origin['item']['cohort']]
        or not isinstance(d.reason, str)
        or not 1 <= len(d.reason) <= MAX_REASON
        or d.reason.strip() != d.reason
        or not d.principal
        or not d.source
        or ner.digest(decision_payload(**{k: getattr(d, k) for k in keys})) != d.payload_hash
    ):
        error('DECISION_INVALID')
    return d


def meta(row, context):
    policy = project_operation_policy(context[2])
    result = {
        'id': row.pk,
        'schema': SCHEMA,
        'decision_rule': RULE,
        'viewer_hash': viewer_hash(context),
        'ner_batch_id': row.ner_batch_id_frozen,
        'task_id': row.task_id_frozen,
        'batch_name': row.origin['batch_name'],
        'assignee_id': row.assignee_id_frozen,
        'assignee_id_frozen': row.assignee_id_frozen,
        'input_set_hash': row.input_set_hash,
        'task_evidence_hash': ner.digest({'schema': SCHEMA, 'item': row.origin['item']}),
        'status': row.status,
        'version': row.version,
        'created_at': row.created_at.isoformat(),
        'calculated_at': row.origin['calculated_at'],
        'can_decide': row.status == 'pending'
        and context[3] == 'reviewer'
        and row.assignee_id_frozen == context[1].pk
        and context[1].pk not in row.participant_ids
        and policy.allows(ARBITRATE_NER),
        'can_cancel': row.status == 'pending' and context[3] == 'manager',
    }
    if context[3] == 'manager':
        result['origin_manifest_hash'] = row.origin['batch_manifest_hash']
    return result


def detail(row, context, pair=None):
    value, prepared = evidence(context, row.origin, pair=pair)
    d = decisions(context[2]).filter(arbitration=row).first()
    if (row.status == 'resolved') != (d is not None):
        error('DECISION_INVALID')
    decided = None
    if d:
        checked_decision(d, row)
        decided = {
            'id': d.pk,
            'actor_id': d.actor_id_frozen,
            'created_at': d.created_at.isoformat(),
            'winner_submission_id': d.winner['submission_id'],
            'winner_revision': d.winner['revision'],
            'reason': d.reason,
            'rule': RULE,
        }
    cancelled = {k: row.cancellation[k] for k in ('actor_id', 'created_at', 'reason')} if row.cancellation else None
    data = {
        **meta(row, context),
        **value,
        'frozen_counts': row.origin['item']['result'],
        'decision': decided,
        'cancellation': cancelled,
    }
    if value['source_status'] != 'verified':
        data['can_decide'] = False
    return data, prepared
