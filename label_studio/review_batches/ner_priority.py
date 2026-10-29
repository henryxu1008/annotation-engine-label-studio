"""Versioned NER selection evidence for ReviewBatches, never review authority."""
import json
from contextlib import contextmanager

from access_control.identity import resolve_actor
from access_control.project_operations import ORGANIZE_REVIEW, lock_project_operation, require_project_operation
from classification_disagreements import ner, ner_api, ner_batches
from django.conf import settings
from django.db import connection, transaction
from django.db.models import BigIntegerField, BooleanField, Case, F, JSONField, Q, Value, When
from django.db.models.expressions import RawSQL
from rest_framework.exceptions import APIException
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.utils.encoders import JSONEncoder
from tasks.models import Submission, Task, TaskAssignment

from . import service
from .models import ReviewBatch

KIND = 'ner_conflict'
ALGORITHM = 'ner-conflict-ranked/v1'
SCHEMA = 'review-ner-priority/v1'
V1_PLAN_BYTES = 1024 * 1024
MAX_BYTES = V1_PLAN_BYTES
MAX_STORED_PLAN = 4 * V1_PLAN_BYTES
V1_CANDIDATES = 1000


def is_selection(value):
    return isinstance(value, dict) and value.get('kind') == KIND


def is_plan(plan):
    p = plan.get('priority', {}) if isinstance(plan, dict) else {}
    return isinstance(plan, dict) and (
        plan.get('algorithm') == ALGORITHM
        or isinstance(p, dict)
        and (p.get('schema') == SCHEMA or p.get('kind') == KIND)
    )


@contextmanager
def errors():
    try:
        yield
    except ner.Error as exc:
        service.fail(exc.code, exc.status)


def bounded(value):
    with errors():
        return ner.bounded(value, MAX_BYTES)


def load(query, *, for_serialization=False):
    with errors():
        return ner_api.load(
            query.select_related(None),
            validate_source=False,
            projection='submission' if for_serialization else 'review',
        )


def read_guard(pk, principal, actor):
    project, role, _ = service.guard(pk, principal, actor)
    modes = settings.ANNOTATION_NER_DISAGREEMENT_MODES
    if (
        not settings.ANNOTATION_NER_REVIEW_PRIORITY_ENABLED
        or not settings.ANNOTATION_NER_DISAGREEMENTS_ENABLED
        or not settings.ANNOTATION_NER_BATCHES_ENABLED
        or not isinstance(modes, dict)
        or modes.get(f'{project.organization_id}:{project.pk}') != 'fork_review'
    ):
        service.fail('REVIEW_NER_PRIORITY_UNAVAILABLE', 403)
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


def context(request, pk, principal, actor_id, *, create=False):
    current, actor = resolve_actor(request)
    if current != principal or actor.pk != actor_id:
        service.fail('REVIEW_PRIORITY_AUTHORITY_CHANGED', 403)
    project, role = read_guard(pk, current, actor)
    if create:
        if role != 'manager':
            service.fail('REVIEW_PRIORITY_FORBIDDEN', 403)
        require_project_operation(project, ORGANIZE_REVIEW)
    return current, actor, project, role


def marker():
    return Q(plan__algorithm=ALGORITHM) | Q(plan__priority__schema=SCHEMA) | Q(plan__priority__kind=KIND)


def query(project):
    """Only the new schema is SQL-size guarded; legacy plan contracts stay intact."""
    table = connection.ops.quote_name(ReviewBatch._meta.db_table)
    column = table + '.' + connection.ops.quote_name('plan')
    expression = (
        f'OCTET_LENGTH(CAST({column} AS TEXT))'
        if connection.vendor == 'postgresql'
        else f'LENGTH(CAST({column} AS BLOB))'
    )
    return (
        ReviewBatch.objects.filter(project=project)
        .annotate(
            _ner_kind=Case(When(marker(), then=Value(True)), default=Value(False), output_field=BooleanField()),
            _ner_plan_size=RawSQL(expression, [], output_field=BigIntegerField()),
        )
        .annotate(
            _ner_plan=Case(
                When(_ner_kind=False, then=F('plan')),
                When(_ner_plan_size__lte=MAX_STORED_PLAN, then=F('plan')),
                default=Value(None),
                output_field=JSONField(),
            )
        )
        .defer('plan')
    )


def hydrate(batch):
    if hasattr(batch, '_ner_plan'):
        if batch._ner_plan is None:
            service.fail('REVIEW_BATCH_PLAN_INVALID')
        batch.plan = batch._ner_plan
    return batch


def visible_query(project, principal, actor, role):
    rows = query(project)
    if not available(project.pk, principal, actor):
        return rows.filter(_ner_kind=False)
    if role != 'reviewer':
        return rows
    table = connection.ops.quote_name(ReviewBatch._meta.db_table)
    column = table + '.' + connection.ops.quote_name('plan')
    if connection.vendor == 'postgresql':
        predicate = f"""EXISTS (
            SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof({column}->'candidates')='array'
                THEN {column}->'candidates' ELSE '[]'::jsonb END) c
            WHERE c->>'submitter_id' <> %s AND NOT EXISTS (
                SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof({column}->'priority'->'participants')='array'
                    THEN {column}->'priority'->'participants' ELSE '[]'::jsonb END) p
                WHERE p->>'task_id'=c->>'task_id' AND p->'actor_ids' @> %s::jsonb))"""
        params = [str(actor.pk), json.dumps([actor.pk])]
    else:
        predicate = f"""EXISTS (
            SELECT 1 FROM json_each({column}, '$.candidates') c
            WHERE json_extract(CASE WHEN c.type='object' THEN c.value ELSE '{{}}' END, '$.submitter_id') <> %s AND NOT EXISTS (
                SELECT 1 FROM json_each({column}, '$.priority.participants') p
                WHERE json_extract(CASE WHEN p.type='object' THEN p.value ELSE '{{}}' END, '$.task_id')=
                      json_extract(CASE WHEN c.type='object' THEN c.value ELSE '{{}}' END, '$.task_id')
                  AND EXISTS (SELECT 1 FROM json_each(CASE WHEN p.type='object' THEN p.value ELSE '{{}}' END, '$.actor_ids') a WHERE a.value=%s)))"""
        params = [actor.pk, actor.pk]
    return rows.annotate(_ner_visible=RawSQL(predicate, params)).filter(Q(_ner_kind=False) | Q(_ner_visible=True))


def materialize(current, args, *, lock=False):
    principal, actor, project, _ = current
    rows, candidate_ids = service.candidate_rows(project, args)
    selection = args['priority']
    with errors():
        batch = ner_batches.query_batches(project).filter(pk=selection['batch_id']).first()
        if batch is None:
            service.fail('REVIEW_PRIORITY_SOURCE_UNAVAILABLE')
        batch = ner_batches.checked(batch, project)
        if batch.manifest_hash != selection['manifest_hash']:
            service.fail('REVIEW_PRIORITY_MANIFEST_CHANGED')
        tasks = batch.manifest['tasks']
        if (
            len(tasks) > ner_batches.MAX_TASKS
            or any(len(t['cohort']) > ner_batches.MAX_PARTICIPANTS for t in tasks)
            or sum(len(t['cohort']) for t in tasks) > ner_batches.MAX_INPUTS
            or batch.manifest['work_units'] > ner.MAX_WORK
            or 2 * batch.manifest['scorer_calls_per_pass'] > ner_batches.MAX_CALLS
        ):
            service.fail('REVIEW_NER_PRIORITY_TOO_LARGE', 422)
        cohort = [e for t in tasks for e in t['cohort']]
        source_ids = {e['source']['submission_id'] for e in cohort if e['source']}
        union_ids = sorted(source_ids | set(candidate_ids))
        if lock:
            candidate_meta = list(
                rows.filter(pk__in=candidate_ids).values_list('assignment__task_id', 'assignment_id')
            )
            task_ids = {t['task_id'] for t in tasks} | {t for t, _ in candidate_meta}
            assignments = {e['assignment_id'] for e in cohort} | {a for _, a in candidate_meta}
            list(
                Task.objects.filter(project=project, pk__in=task_ids)
                .order_by('pk')
                .select_for_update()
                .values_list('pk', flat=True)
            )
            list(
                TaskAssignment.objects.filter(project=project, pk__in=assignments)
                .order_by('pk')
                .select_for_update()
                .values_list('pk', flat=True)
            )
            list(
                Submission.objects.filter(pk__in=union_ids, assignment__project=project)
                .order_by('pk')
                .select_for_update()
                .values_list('pk', flat=True)
            )
        # The ID denominator and guarded Manifest are known before any snapshot
        # decoder. One SQL window guards the de-duplicated entire source union.
        loaded = load(
            Submission.objects.filter(pk__in=union_ids, assignment__project=project, assignment__task__project=project)
        )
        sources = {row.pk: row for row in loaded}
        candidates = []
        for identifier in candidate_ids:
            row = sources.get(identifier)
            if row is None or not service.valid_source(row, project.pk):
                service.fail('REVIEW_BATCH_SOURCE_INVALID')
            candidates.append(service.identity(row))
        reports, prepared = ner_batches.project_items(batch, current, loaded_rows=loaded)
        if any(r['source_status'] != 'verified' for r in reports):
            service.fail('REVIEW_PRIORITY_SOURCE_UNAVAILABLE')
        reports = {r['task_id']: r for r in reports}
        frozen = {t['task_id']: t for t in tasks}
        evidence = []
        for candidate in candidates:
            item = frozen.get(candidate['task_id'])
            state, reason, calculation = 'unknown', 'not_covered', None
            if item is not None:
                pin = next(
                    (
                        e['source']
                        for e in item['cohort']
                        if e['source'] and all(e['source'].get(k) == v for k, v in candidate.items())
                    ),
                    None,
                )
                reason = 'revision_mismatch'
                if pin is not None:
                    report = reports[candidate['task_id']]
                    calculation = item['result']['calculation_hash']
                    if report['outcome'] in ('agreement', 'disagreement'):
                        state = 'conflict' if report['outcome'] == 'disagreement' else 'agreement'
                        reason = 'frozen_' + state
                    else:
                        reason = 'metric_unavailable'
            evidence.append(
                {
                    'submission_id': candidate['submission_id'],
                    'state': state,
                    'reason': reason,
                    'calculation_hash': calculation,
                }
            )
        priority = {
            'schema': SCHEMA,
            'kind': KIND,
            'batch_id': batch.pk,
            'manifest_hash': batch.manifest_hash,
            'metric_rule': ner.RULE,
            'selection': ner_batches.SELECTION,
            'explanation': ner.EXPLANATION,
            'engine': ner.ENGINE,
            'creator': {'principal': principal.principal_id, 'source': principal.source, 'actor_id': actor.pk},
            'evidence': evidence,
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
        }
        plan = service.assemble_plan(project.pk, args, candidates, priority, ALGORITHM)
        bounded(plan)
        validate(plan)
        return plan, prepared


def validate(plan, batch=None):
    """Validate the immutable local receipt without any live source hydration."""
    try:
        ner.bounded(plan, V1_PLAN_BYTES)
        p = plan['priority']
        keys = {
            'schema',
            'kind',
            'batch_id',
            'manifest_hash',
            'metric_rule',
            'selection',
            'explanation',
            'engine',
            'creator',
            'evidence',
            'participants',
        }
        positive = ner_api.positive

        def hexadecimal(value):
            return isinstance(value, str) and ner_api.HEX.fullmatch(value) is not None

        creator = p['creator']
        candidates, evidence, participants = plan['candidates'], p['evidence'], p['participants']
        valid = (
            set(plan) == {'algorithm', 'project_id', 'rule', 'candidates', 'priority'}
            and plan['algorithm'] == ALGORITHM
            and positive(plan['project_id'])
            and set(p) == keys
            and p['kind'] == KIND
            and p['schema'] == SCHEMA
            and positive(p['batch_id'])
            and hexadecimal(p['manifest_hash'])
            and p['metric_rule'] == ner.RULE
            and p['selection'] == ner_batches.SELECTION
            and p['explanation'] == ner.EXPLANATION
            and p['engine'] == ner.ENGINE
            and set(creator) == {'principal', 'source', 'actor_id'}
            and isinstance(creator['principal'], str)
            and 1 <= len(creator['principal']) <= 512
            and isinstance(creator['source'], str)
            and 1 <= len(creator['source']) <= 128
            and positive(creator['actor_id'])
            and isinstance(candidates, list)
            and 1 <= len(candidates) <= V1_CANDIDATES
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
            and isinstance(participants, list)
            and len(participants) <= ner_batches.V1_TASKS
            and len({t['task_id'] for t in participants}) == len(participants)
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
            and all(
                set(e) == {'submission_id', 'state', 'reason', 'calculation_hash'}
                and (e['state'], e['reason'])
                in {
                    ('conflict', 'frozen_conflict'),
                    ('agreement', 'frozen_agreement'),
                    ('unknown', 'not_covered'),
                    ('unknown', 'revision_mismatch'),
                    ('unknown', 'metric_unavailable'),
                }
                and (
                    e['calculation_hash'] is None
                    if e['reason'] in ('not_covered', 'revision_mismatch')
                    else hexadecimal(e['calculation_hash'])
                )
                for e in evidence
            )
        )
        r = plan['rule']
        valid = valid and (
            set(r) == {'rate', 'per_submitter', 'seed', 'package_ids', 'submitter_ids'}
            and type(r['rate']) is int
            and 1 <= r['rate'] <= 100
            and type(r['per_submitter']) is bool
            and isinstance(r['seed'], str)
            and 1 <= len(r['seed']) <= 128
            and all(
                isinstance(r[k], list)
                and len(r[k]) <= 100
                and r[k] == sorted(set(r[k]))
                and all(positive(v) for v in r[k])
                for k in ('package_ids', 'submitter_ids')
            )
        )
        if valid:
            expected = service.assemble_plan(
                plan['project_id'],
                r,
                [{k: v for k, v in c.items() if k != 'selected'} for c in candidates],
                p,
                ALGORITHM,
            )
            valid = expected == plan
        if batch is not None:
            valid = (
                valid
                and creator['principal'] == batch.creator_principal
                and creator['actor_id'] == batch.actor_id_frozen
            )
    except (KeyError, TypeError, ValueError, AttributeError, ner.Error):
        valid = False
    if not valid:
        service.fail('REVIEW_BATCH_PLAN_INVALID')


def response(request, current, data, status=200):
    from . import priority

    data = {**data, 'viewer_hash': priority.viewer_hash(current[0], current[1], current[2], current[3])}
    result = Response(data, status=status)
    emitted = json.loads(json.dumps(data, cls=JSONEncoder, ensure_ascii=False, allow_nan=False))
    bounded(emitted)
    result.accepted_renderer = JSONRenderer()
    result.accepted_media_type = 'application/json'
    result.renderer_context = {}
    result.render()
    if len(result.content) > MAX_BYTES:
        service.fail('REVIEW_NER_PRIORITY_TOO_LARGE', 422)
    if json.loads(result.content) != emitted or json.loads(json.dumps(result.data, cls=JSONEncoder)) != emitted:
        service.fail('REVIEW_PRIORITY_REPRESENTATION_CHANGED')
    context(request, current[2].pk, current[0], current[1].pk, create=True)
    return result


def verify_plan(request, current, args, plan, prepared, *, lock=False):
    # Official counter validation twice on bounded pinned inputs, followed by
    # a fresh complete source replay. Never wrap an already two-pass HTTP API.
    with errors():
        ner_batches.check_engine(prepared)
        ner_batches.check_engine(prepared)
    fresh = context(request, current[2].pk, current[0], current[1].pk, create=True)
    repeated, _ = materialize(fresh, args, lock=lock)
    if repeated != plan:
        service.fail('REVIEW_BATCH_PREVIEW_CHANGED')
    context(request, current[2].pk, current[0], current[1].pk, create=True)


def preview(request, pk, principal, actor, args):
    current = context(request, pk, principal, actor.pk, create=True)
    plan, prepared = materialize(current, args)
    result = response(request, current, {'plan': plan, 'plan_hash': service.digest(plan)})
    verify_plan(request, current, args, plan, prepared)
    return result


def create(request, pk, principal, args, expected, key, request_hash):
    with transaction.atomic():
        project, actor = lock_project_operation(pk, principal, ORGANIZE_REVIEW)
        current = context(request, pk, principal, actor.pk, create=True)
        previous = query(project).filter(idempotency_key=key).first()
        if previous is not None:
            previous = service.check_loaded_batch(hydrate(previous), project)
            if (
                not is_plan(previous.plan)
                or previous.request_hash != request_hash
                or previous.creator_principal != principal.principal_id
                or previous.actor_id_frozen != actor.pk
                or previous.plan['priority']['creator']['source'] != principal.source
            ):
                service.fail('REVIEW_BATCH_KEY_CONFLICT')
            return response(
                request, current, {**service.metadata(previous), 'replayed': True, 'idempotency_key': str(key)}
            )
        plan, prepared = materialize(current, args, lock=True)
        if service.digest(plan) != expected:
            service.fail('REVIEW_BATCH_PREVIEW_CHANGED')
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
            request, current, {**service.metadata(batch), 'replayed': False, 'idempotency_key': str(key)}, 201
        )
        verify_plan(request, current, args, plan, prepared, lock=True)
        return result
