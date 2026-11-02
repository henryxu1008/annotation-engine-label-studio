"""Bounded plan policy + one exact initial review. Never hydrate the batch cohort."""
from datetime import datetime, timezone
from types import SimpleNamespace

from classification_disagreements import ner, snapshot_loading
from django.shortcuts import get_object_or_404
from tasks.models import ReviewDecision, Submission, Task, TaskAssignment

from . import confidence_contract as types
from . import confidence_priority, priority, service
from . import second_review_contract as c
from .models import ReviewBatch
from .second_review_bounded import Budget


def utc(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def hints(project_id, source_id):
    return get_object_or_404(
        Submission.objects.filter(assignment__project_id=project_id, assignment__task__project_id=project_id).values(
            'pk',
            'assignment_id',
            'assignment__task_id',
            'assignment__assignee_id',
            'submitted_by_id',
            'review__reviewer_id',
        ),
        pk=source_id,
    )


def lock_origin(project_id, batch_id, source_id, before):
    """All human locks were obtained with the Project fence, never acquired late."""
    if hints(project_id, source_id) != before:
        c.fail('SOURCE_CHANGED')
    get_object_or_404(
        Task.objects.select_for_update().values('pk'), pk=before['assignment__task_id'], project_id=project_id
    )
    get_object_or_404(
        TaskAssignment.objects.select_for_update().values('pk'), pk=before['assignment_id'], project_id=project_id
    )
    get_object_or_404(Submission.objects.select_for_update().values('pk'), pk=source_id)
    list(ReviewDecision.objects.filter(submission_id=source_id).select_for_update().values_list('pk', flat=True))
    get_object_or_404(ReviewBatch.objects.select_for_update().values('pk'), pk=batch_id, project_id=project_id)


def plan(ctx, batch_id, budget):
    p, actor, project, role = ctx
    service.enabled(project)
    query = ReviewBatch.objects.filter(pk=batch_id, project=project).only(
        'id', 'project_id', 'actor_id_frozen', 'creator_principal', 'plan', 'plan_hash'
    )
    rows = budget.rows(query, {'plan': (4 * c.MIB, 16 * c.MIB)})
    if not rows:
        c.fail('SOURCE_UNAVAILABLE')
    batch = rows[0]
    batch.project = project
    value = batch.plan
    try:
        if (
            not isinstance(value, dict)
            or set(value)
            not in (
                {'algorithm', 'project_id', 'rule', 'candidates'},
                {'algorithm', 'project_id', 'rule', 'candidates', 'priority'},
            )
            or type(value['project_id']) is not int
            or value['project_id'] != project.pk
            or c.digest(value) != batch.plan_hash
        ):
            c.fail()
        rule, candidates = value['rule'], value['candidates']
        if (
            not isinstance(rule, dict)
            or set(rule) != {'rate', 'per_submitter', 'seed', 'package_ids', 'submitter_ids'}
            or not types.integer(rule['rate'], 1, 100)
            or type(rule['per_submitter']) is not bool
            or not types.text(rule['seed'], 128)
            or any(
                not isinstance(rule[k], list)
                or len(rule[k]) > 100
                or len(set(rule[k])) != len(rule[k])
                or any(not c.positive(i) for i in rule[k])
                for k in ('package_ids', 'submitter_ids')
            )
            or not isinstance(candidates, list)
            or not 1 <= len(candidates) <= 1000
            or len({v['submission_id'] for v in candidates}) != len(candidates)
        ):
            c.fail()
        expected = {'submission_id', 'assignment_id', 'task_id', 'revision', 'result_hash', 'submitter_id', 'selected'}
        if any(
            not isinstance(v, dict)
            or set(v) != expected
            or type(v['selected']) is not bool
            or any(not c.positive(v[k]) for k in expected - {'result_hash', 'selected'})
            or not c.hexadecimal(v['result_hash'])
            for v in candidates
        ):
            c.fail()
        if 'priority' not in value:
            if value['algorithm'] != service.ALGORITHM:
                c.fail('UNSUPPORTED', 400)
            rebuilt = service.assemble_plan(
                project.pk, rule, [{k: v for k, v in item.items() if k != 'selected'} for item in candidates]
            )
            if c.digest(rebuilt) != batch.plan_hash:
                c.fail()
        else:
            source = value['priority']
            if not isinstance(source, dict) or value['algorithm'] not in (
                priority.ALGORITHM,
                confidence_priority.ALGORITHM,
            ):
                c.fail('UNSUPPORTED', 400)
            if value['algorithm'] == priority.ALGORITHM:
                if set(source) != {
                    'schema',
                    'batch_id',
                    'manifest_hash',
                    'metric_rule',
                    'selection',
                    'evidence',
                    'participants',
                }:
                    c.fail()
                if any(set(v) != {'task_id', 'actor_ids'} for v in source['participants']) or any(
                    set(v) != {'submission_id', 'state', 'reason', 'calculation_hash'} for v in source['evidence']
                ):
                    c.fail()
            priority.validate(value, batch)
            priority.read_guard(project.pk, p, actor, value)
    except (KeyError, ValueError, TypeError, AttributeError):
        c.fail()
    return batch


class SnapshotPolicy:
    MAX_REVIEW_REASON_BYTES = 32768
    MAX_REVIEW_TIMESTAMP_BYTES = 128
    MAX_STORED_BYTES = MAX_ROW_STORED_BYTES = 2 * c.MIB
    MAX_SNAPSHOT_BYTES = MAX_ROW_SNAPSHOT_BYTES = 256 * 1024
    Error = ner.Error
    TOO_LARGE = 'SECOND_REVIEW_TOO_LARGE'
    SOURCE_INTEGRITY_FAILED = 'SECOND_REVIEW_CORRUPT'
    bounded = staticmethod(c.bounded)


def load(ctx, batch_id, source_id, *, fresh=False, frozen=None):
    budget = Budget()
    batch = plan(ctx, batch_id, budget)
    candidate = next((v for v in batch.plan['candidates'] if v['submission_id'] == source_id), None)
    if candidate is None or not candidate['selected']:
        c.fail('SOURCE_INELIGIBLE')
    query = types.source_query(ctx[2]).filter(pk=source_id)
    if confidence_priority.is_plan(batch.plan):
        query = confidence_priority.linked_sources(batch, query)
    try:
        rows = snapshot_loading.load(
            query, policy=SnapshotPolicy, validate=service.valid_source, projection='supervision'
        )
    except ner.Error as exc:
        service.fail(exc.code, exc.status)
    if len(rows) != 1:
        c.fail('SOURCE_UNAVAILABLE')
    row = rows[0]
    review = getattr(row, 'review', None)
    if row.status not in ('approved', 'rejected') or review is None:
        c.fail('SOURCE_INELIGIBLE')
    if not isinstance(review.created_at, datetime) or review.created_at.tzinfo is None:
        c.fail()
    if not types.supported(row):
        c.fail('INPUT_UNSUPPORTED', 400)
    pins = types.source_pins(row, ctx)
    if any(not c.positive(pins[k]) for k in types.PINS[:6]) or not c.positive(review.reviewer_id):
        c.fail('SOURCE_UNAVAILABLE')
    if any(candidate[k] != v for k, v in service.identity(row).items()):
        c.fail('SOURCE_CHANGED')
    # A selected confidence row must still be the exact frozen subject. No fresh
    # profile registry, producer grant, sorting, comparison or scoring occurs.
    if confidence_priority.is_plan(batch.plan):
        confidence_priority.check_sources(batch, {row.pk: row})
        entry = next(v for v in batch.plan['priority']['evidence'] if v['submission_id'] == row.pk)
        if entry['subject'] is not None and entry['subject'] != pins or entry['assignee_id'] != pins['assignee_id']:
            c.fail('SOURCE_CHANGED')
        if entry['observation']:
            confidence_priority.verify_linked_source(
                SimpleNamespace(proof=entry['observation']['proof']), row, ctx, batch.plan['priority']['profile']
            )
    newest = (
        Submission.objects.filter(assignment_id=row.assignment_id)
        .order_by('-revision')
        .values_list('pk', flat=True)
        .first()
    )
    if fresh and newest != row.pk:
        c.fail('SOURCE_CHANGED')
    c.bounded(review.reason, 8192)
    budget.stored += row.ner_reason_total + row.ner_review_timestamp_size
    budget.canonical += c.bounded(review.reason, 8192) + c.bounded(utc(review.created_at), 128)
    if budget.stored > budget.stored_limit or budget.canonical > budget.canonical_limit:
        c.fail('TOO_LARGE', 422)
    first = dict(
        schema='first-review-proof/v1',
        id=review.pk,
        submission_id=row.pk,
        reviewer_id=review.reviewer_id,
        decision=review.decision,
        reason=review.reason,
        created_at=utc(review.created_at),
    )
    cohort = []
    marker = batch.plan.get('priority')
    if marker and batch.plan['algorithm'] == priority.ALGORITHM:
        cohort = sorted({a for v in marker['participants'] if v['task_id'] == pins['task_id'] for a in v['actor_ids']})
    excluded = sorted({pins['assignee_id'], pins['submitter_id'], review.reviewer_id, *cohort})
    if ctx[3] == 'reviewer' and ctx[1].pk in excluded:
        c.fail('FORBIDDEN', 403)
    proof = dict(
        schema=c.SCHEMA,
        input_contract=c.INPUT,
        rule=c.RULE,
        effect=c.EFFECT,
        instance_id=c.instance(),
        organization_id=ctx[2].organization_id,
        project_id=ctx[2].pk,
        batch_id=batch.pk,
        plan_hash=batch.plan_hash,
        algorithm=batch.plan['algorithm'],
        priority_schema=marker['schema'] if marker else None,
        priority_hash=c.digest(marker) if marker else None,
        candidate=candidate,
        subject=pins,
        first_review=first,
        first_review_hash=c.digest(first),
        excluded_actor_ids=excluded,
    )
    c.bounded(proof, 16 * 1024)
    if frozen is not None and c.digest(proof) != c.digest(frozen):
        c.fail('SOURCE_CHANGED')
    return dict(
        proof=proof,
        source_hash=c.digest(proof),
        first_review_hash=c.digest(first),
        row=row,
        plan_hash=batch.plan_hash,
        historical_revision=newest != row.pk,
        cost=dict(
            metadata_stored=budget.stored,
            metadata_canonical=budget.canonical,
            snapshot_stored=row.ner_row_size,
            snapshot_canonical=c.bounded(row.result_snapshot, 256 * 1024),
        ),
    )


def representation(value):
    return c.digest(
        dict(proof=value['proof'], snapshot=value['row'].result_snapshot, historical=value['historical_revision'])
    )
