"""Optional exact frozen field evidence; selection never grants review authority."""
import json
from contextlib import contextmanager

from access_control.identity import resolve_actor
from access_control.project_operations import ORGANIZE_REVIEW, lock_project_operation, require_project_operation
from classification_disagreements import field_api, fields
from classification_disagreements import field_batches as batches
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import APIException
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.utils.encoders import JSONEncoder
from tasks.models import Submission, Task, TaskAssignment

from . import service
from .models import ReviewBatch

KIND = 'field_conflict'
ALGORITHM = 'field-conflict-ranked/v1'
SCHEMA = 'review-field-priority/v1'
V1_BYTES = 1024 * 1024
MAX_BYTES = V1_BYTES
MAX_PAGE_BYTES, MAX_PAGE_STORED = 8 * V1_BYTES, 32 * V1_BYTES
VERSIONS = {
    'source_schema': 'field-batches/v1',
    'metric_rule': 'field-lf-ascii-trim/v1',
    'selection': 'active-assignment-latest/v1',
    'explanation': 'field-value-differences/v1',
    'engine': 'python-string-equality/v1',
    'queue_rule': 'field-raw-difference-queue/v1',
}


def is_selection(value):
    return isinstance(value, dict) and value.get('kind') == KIND


def is_plan(plan):
    p = plan.get('priority') if isinstance(plan, dict) else None
    return isinstance(plan, dict) and (
        plan.get('algorithm') == ALGORITHM
        or isinstance(p, dict)
        and (p.get('kind') == KIND or p.get('schema') == SCHEMA)
    )


def marker():
    return Q(plan__algorithm=ALGORITHM) | Q(plan__priority__kind=KIND) | Q(plan__priority__schema=SCHEMA)


@contextmanager
def errors():
    try:
        yield
    except fields.Error as exc:
        service.fail(exc.code, exc.status)


def canonical_size(value, maximum=V1_BYTES):
    with errors():
        return fields.bounded(value, maximum)


def bounded(value):
    return canonical_size(value, MAX_BYTES)


def load(query, *, for_serialization=False):
    with errors():
        return field_api.load(
            query.select_related(None),
            validate_source=False,
            projection='submission' if for_serialization else 'review',
        )


def read_guard(pk, principal, actor, plan=None):
    project, role, _ = service.guard(pk, principal, actor)
    modes = settings.ANNOTATION_FIELD_DISAGREEMENT_MODES
    if (
        not settings.ANNOTATION_FIELD_REVIEW_PRIORITY_ENABLED
        or not settings.ANNOTATION_FIELD_BATCHES_ENABLED
        or not settings.ANNOTATION_FIELD_DISAGREEMENTS_ENABLED
        or not isinstance(modes, dict)
        or modes.get(f'{project.organization_id}:{project.pk}') != 'fork_review'
    ):
        service.fail('REVIEW_FIELD_PRIORITY_UNAVAILABLE', 403)
    if plan is not None and plan['priority']['organization_id'] != project.organization_id:
        service.fail('REVIEW_PRIORITY_AUTHORITY_CHANGED', 403)
    return project, role


def available(pk, principal, actor, *, create=False):
    try:
        project, role = read_guard(pk, principal, actor)
        if create:
            if role != 'manager':
                return False
            require_project_operation(project, ORGANIZE_REVIEW)
        return True
    except APIException:
        return False


def context(request, initial, *, create=True):
    principal, actor = resolve_actor(request)
    if principal != initial[0] or actor.pk != initial[1].pk:
        service.fail('REVIEW_PRIORITY_AUTHORITY_CHANGED', 403)
    project, role = read_guard(initial[2].pk, principal, actor)
    if project.organization_id != initial[2].organization_id or role != initial[3]:
        service.fail('REVIEW_PRIORITY_AUTHORITY_CHANGED', 403)
    if create:
        if role != 'manager':
            service.fail('REVIEW_PRIORITY_FORBIDDEN', 403)
        require_project_operation(project, ORGANIZE_REVIEW)
    return principal, actor, project, role


def materialize(current, args, *, lock=False):
    principal, actor, project, _ = current
    candidate_query, candidate_ids = service.candidate_rows(project, args)
    selection = args['priority']
    with errors():
        query = batches.query_batches(project)
        if lock:
            query = query.select_for_update()
        batch = query.filter(pk=selection['batch_id']).first()
        if batch is None:
            service.fail('REVIEW_PRIORITY_SOURCE_UNAVAILABLE')
        batches.checked(batch, project)
        if batch.manifest_hash != selection['manifest_hash']:
            service.fail('REVIEW_PRIORITY_MANIFEST_CHANGED')
        tasks = batch.manifest['tasks']
        if (
            len(tasks) > batches.MAX_TASKS
            or any(len(t['cohort']) > batches.MAX_PARTICIPANTS for t in tasks)
            or sum(len(t['cohort']) for t in tasks) > batches.MAX_INPUTS
        ):
            service.fail('REVIEW_FIELD_PRIORITY_TOO_LARGE', 422)
        cohort = [e for t in tasks for e in t['cohort']]
        source_ids = {e['source']['submission_id'] for e in cohort if e['source']}
        union_ids = sorted(source_ids | set(candidate_ids))
        if lock:
            meta = list(
                candidate_query.filter(pk__in=candidate_ids).values_list('assignment__task_id', 'assignment_id')
            )
            for model, ids in (
                (Task, {t['task_id'] for t in tasks} | {t for t, _ in meta}),
                (TaskAssignment, {e['assignment_id'] for e in cohort} | {a for _, a in meta}),
                (Submission, union_ids),
            ):
                scoped = model.objects.filter(pk__in=ids)
                scoped = (
                    scoped.filter(assignment__project=project, assignment__task__project=project)
                    if model is Submission
                    else scoped.filter(project=project)
                )
                list(scoped.order_by('pk').select_for_update().values_list('pk', flat=True))
        loaded = load(batches.scoped_sources(project, union_ids))
        sources = {r.pk: r for r in loaded}
        candidates = []
        for identifier in candidate_ids:
            row = sources.get(identifier)
            if row is None or not service.valid_source(row, project.pk):
                service.fail('REVIEW_BATCH_SOURCE_INVALID')
            candidates.append(service.identity(row))
        items, inspected, missing = batches.inspect_items(batch, current, loaded_rows=loaded)
        if missing:
            service.fail('REVIEW_PRIORITY_SOURCE_UNAVAILABLE')
        cost = batches.budget(inspected)
        frozen = {t['task_id']: t for t in tasks}
        evidence = []
        for candidate in candidates:
            item = frozen.get(candidate['task_id'])
            state, reason, calculation, pin = 'unknown', 'not_covered', None, None
            if item is not None:
                reason = 'revision_mismatch'
                pin = next(
                    (
                        e['source']
                        for e in item['cohort']
                        if e['source'] and all(e['source'].get(k) == v for k, v in candidate.items())
                    ),
                    None,
                )
                if pin is not None:
                    if field_api.pin(sources[candidate['submission_id']]) != pin:
                        service.fail('REVIEW_PRIORITY_SOURCE_UNAVAILABLE')
                    calculation = item['result']['calculation_hash']
                    outcome = item['result']['outcome']
                    if outcome in ('agreement', 'disagreement'):
                        state = 'conflict' if outcome == 'disagreement' else 'agreement'
                        reason = 'frozen_' + state
                    else:
                        reason = 'metric_unavailable'
            evidence.append(
                {
                    'submission_id': candidate['submission_id'],
                    'state': state,
                    'reason': reason,
                    'calculation_hash': calculation,
                    'source_pin': pin,
                }
            )
        priority = {
            'schema': SCHEMA,
            'kind': KIND,
            'organization_id': project.organization_id,
            'batch_id': batch.pk,
            'manifest_hash': batch.manifest_hash,
            **VERSIONS,
            'creator': {'principal': principal.principal_id, 'source': principal.source, 'actor_id': actor.pk},
            'participants': [
                {
                    'task_id': t['task_id'],
                    'actor_ids': sorted(
                        {
                            v
                            for e in t['cohort']
                            for v in (e['assignee_id'], (e['source'] or {}).get('submitter_id'))
                            if v is not None
                        }
                    ),
                }
                for t in tasks
            ],
            'evidence': evidence,
        }
        plan = service.assemble_plan(project.pk, args, candidates, priority, ALGORITHM)
        bounded(plan)
        validate(plan)
        return plan, {'items': items, 'inspected': inspected, 'cost': cost}


def calculate(prepared):
    with errors():
        scored, actual = batches.score_all(prepared['inspected'])
        if actual != prepared['cost']:
            service.fail('REVIEW_PRIORITY_READ_CHANGED')
        batches.project_scored(prepared['items'], scored, set())
    return scored


def validate(plan, batch=None):
    """Fixed v1 receipt validation; no engine or live analysis dependency."""
    positive, hexadecimal = field_api.positive, lambda v: isinstance(v, str) and field_api.HEX.fullmatch(v) is not None
    try:
        fields.bounded(plan, V1_BYTES)
        p, rule, candidates = plan['priority'], plan['rule'], plan['candidates']
        creator, evidence, participants = p['creator'], p['evidence'], p['participants']
        valid = (
            set(plan) == {'algorithm', 'project_id', 'rule', 'candidates', 'priority'}
            and plan['algorithm'] == ALGORITHM
            and positive(plan['project_id'])
            and set(p)
            == {
                'schema',
                'kind',
                'organization_id',
                'batch_id',
                'manifest_hash',
                'creator',
                'participants',
                'evidence',
                *VERSIONS,
            }
            and p['schema'] == SCHEMA
            and p['kind'] == KIND
            and positive(p['organization_id'])
            and positive(p['batch_id'])
            and hexadecimal(p['manifest_hash'])
            and all(p[k] == v for k, v in VERSIONS.items())
            and set(creator) == {'principal', 'source', 'actor_id'}
            and positive(creator['actor_id'])
            and isinstance(creator['principal'], str)
            and 1 <= len(creator['principal']) <= 512
            and isinstance(creator['source'], str)
            and 1 <= len(creator['source']) <= 128
            and isinstance(candidates, list)
            and 1 <= len(candidates) <= 1000
            and [c['submission_id'] for c in candidates] == sorted({c['submission_id'] for c in candidates})
            and all(
                set(c)
                == {'submission_id', 'assignment_id', 'task_id', 'revision', 'result_hash', 'submitter_id', 'selected'}
                and all(
                    positive(c[k]) for k in ('submission_id', 'assignment_id', 'task_id', 'revision', 'submitter_id')
                )
                and hexadecimal(c['result_hash'])
                and type(c['selected']) is bool
                for c in candidates
            )
            and set(rule) == {'rate', 'per_submitter', 'seed', 'package_ids', 'submitter_ids'}
            and type(rule['rate']) is int
            and 1 <= rule['rate'] <= 100
            and type(rule['per_submitter']) is bool
            and isinstance(rule['seed'], str)
            and 1 <= len(rule['seed']) <= 128
            and all(
                isinstance(rule[k], list)
                and len(rule[k]) <= 100
                and rule[k] == sorted(set(rule[k]))
                and all(positive(v) for v in rule[k])
                for k in ('package_ids', 'submitter_ids')
            )
            and isinstance(participants, list)
            and 1 <= len(participants) <= 100
            and [t['task_id'] for t in participants] == sorted({t['task_id'] for t in participants})
            and all(
                set(t) == {'task_id', 'actor_ids'}
                and positive(t['task_id'])
                and isinstance(t['actor_ids'], list)
                and len(t['actor_ids']) <= 40
                and t['actor_ids'] == sorted(set(t['actor_ids']))
                and all(positive(v) for v in t['actor_ids'])
                for t in participants
            )
            and isinstance(evidence, list)
            and [e['submission_id'] for e in evidence] == [c['submission_id'] for c in candidates]
        )
        actors = {t['task_id']: t['actor_ids'] for t in participants}
        for c, e in zip(candidates, evidence):
            valid = valid and set(e) == {'submission_id', 'state', 'reason', 'calculation_hash', 'source_pin'}
            valid = valid and (e['state'], e['reason']) in {
                ('conflict', 'frozen_conflict'),
                ('agreement', 'frozen_agreement'),
                ('unknown', 'not_covered'),
                ('unknown', 'revision_mismatch'),
                ('unknown', 'metric_unavailable'),
            }
            if e['reason'] in ('not_covered', 'revision_mismatch'):
                valid = valid and e['source_pin'] is None and e['calculation_hash'] is None
                valid = valid and ((c['task_id'] not in actors) == (e['reason'] == 'not_covered'))
            else:
                pin = e['source_pin']
                valid = valid and hexadecimal(e['calculation_hash']) and isinstance(pin, dict)
                valid = valid and set(pin) == set(field_api.PIN_IDS + field_api.PIN_HASHES)
                valid = valid and all(positive(pin[k]) for k in field_api.PIN_IDS)
                valid = valid and all(hexadecimal(pin[k]) for k in field_api.PIN_HASHES)
                valid = valid and all(pin[k] == v for k, v in c.items() if k != 'selected')
                valid = (
                    valid and pin['assignee_id'] == pin['submitter_id'] and pin['assignee_id'] in actors[c['task_id']]
                )
        if valid:
            valid = (
                service.assemble_plan(
                    plan['project_id'],
                    rule,
                    [{k: v for k, v in c.items() if k != 'selected'} for c in candidates],
                    p,
                    ALGORITHM,
                )
                == plan
            )
        if batch is not None:
            valid = (
                valid
                and creator['principal'] == batch.creator_principal
                and creator['actor_id'] == batch.actor_id_frozen
            )
            valid = valid and p['organization_id'] == batch.project.organization_id
    except (KeyError, ValueError, TypeError, AttributeError, fields.Error):
        valid = False
    if not valid:
        service.fail('REVIEW_BATCH_PLAN_INVALID')


def processing(prepared=None):
    cost = prepared['cost'] if prepared else dict(batches.ZERO)
    return {
        'hydration_passes': 2 if prepared else 0,
        'calculation_passes': 2 if prepared else 0,
        'per_pass': cost,
        'total': {k: 2 * v for k, v in cost.items()},
        'scorer_calls': 0,
    }


def response(current, data, prepared=None, status=200):
    from . import priority

    data = {
        **data,
        'project_id': current[2].pk,
        'processing_cost': processing(prepared),
        'viewer_hash': priority.viewer_hash(*current),
    }
    emitted = json.loads(json.dumps(data, cls=JSONEncoder, ensure_ascii=False, allow_nan=False))
    bounded(emitted)
    result = Response(data, status=status)
    result.accepted_renderer, result.accepted_media_type, result.renderer_context = (
        JSONRenderer(),
        'application/json',
        {},
    )
    result.render()
    if len(result.content) > MAX_BYTES:
        service.fail('REVIEW_FIELD_PRIORITY_TOO_LARGE', 422)
    if json.loads(result.content) != emitted or json.loads(json.dumps(result.data, cls=JSONEncoder)) != emitted:
        service.fail('REVIEW_PRIORITY_REPRESENTATION_CHANGED')
    result._field_expected = emitted
    return result


def check_response(result):
    """The final source/counter callbacks cannot replace the rendered receipt."""
    if len(result.content) > MAX_BYTES:
        service.fail('REVIEW_FIELD_PRIORITY_TOO_LARGE', 422)
    if (
        json.loads(result.content) != result._field_expected
        or json.loads(json.dumps(result.data, cls=JSONEncoder)) != result._field_expected
    ):
        service.fail('REVIEW_PRIORITY_REPRESENTATION_CHANGED')


def verify(request, initial, args, plan, prepared, first, *, lock=False):
    if calculate(prepared) != first:
        service.fail('REVIEW_PRIORITY_READ_CHANGED')
    fresh = context(request, initial)
    repeated, replay = materialize(fresh, args, lock=lock)
    if repeated != plan or replay != prepared:
        service.fail('REVIEW_BATCH_PREVIEW_CHANGED')
    context(request, initial)


def preview(request, pk, principal, actor, args):
    project, role = read_guard(pk, principal, actor)
    initial = (principal, actor, project, role)
    current = context(request, initial)
    plan, prepared = materialize(current, args)
    first = calculate(prepared)
    result = response(current, {'plan': plan, 'plan_hash': service.digest(plan)}, prepared)
    verify(request, initial, args, plan, prepared, first)
    check_response(result)
    context(request, initial)
    return result


def create(request, pk, principal, args, expected, key, request_hash):
    from . import guarded

    with transaction.atomic():
        project, actor = lock_project_operation(pk, principal, ORGANIZE_REVIEW)
        current = context(request, (principal, actor, project, 'manager'))
        previous = guarded.query(project).filter(idempotency_key=key).first()
        if previous is not None:
            previous = service.check_loaded_batch(guarded.hydrate(previous), project)
            if (
                not is_plan(previous.plan)
                or previous.request_hash != request_hash
                or previous.creator_principal != principal.principal_id
                or previous.actor_id_frozen != actor.pk
                or previous.plan['priority']['creator']['source'] != principal.source
            ):
                service.fail('REVIEW_BATCH_KEY_CONFLICT')
            result = response(current, {**service.metadata(previous), 'idempotency_key': str(key), 'replayed': True})
            context(request, current)
            service.checked_batch(project, previous.pk)
            check_response(result)
            context(request, current)
            return result
        try:
            plan, prepared = materialize(current, args, lock=True)
        except APIException as exc:
            if getattr(exc, 'display_context', {}).get('reason') == 'REVIEW_BATCH_EMPTY':
                service.fail('REVIEW_BATCH_PREVIEW_CHANGED')
            raise
        if service.digest(plan) != expected:
            service.fail('REVIEW_BATCH_PREVIEW_CHANGED')
        first = calculate(prepared)
        batch = ReviewBatch.objects.create(
            project=project,
            created_by=actor,
            actor_id_frozen=actor.pk,
            creator_principal=principal.principal_id,
            name=args['name'],
            idempotency_key=key,
            request_hash=request_hash,
            plan=plan,
            plan_hash=expected,
        )
        result = response(
            current, {**service.metadata(batch), 'idempotency_key': str(key), 'replayed': False}, prepared, 201
        )
        verify(request, current, args, plan, prepared, first, lock=True)
        check_response(result)
        context(request, current)
        return result
