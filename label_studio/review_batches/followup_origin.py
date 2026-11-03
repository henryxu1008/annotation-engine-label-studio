"""Exact frozen concern and optional existing successor; no workflow writes."""

import hashlib
import json
from datetime import datetime

import rfc8785
from access_control.project_operations import RECORD_SECOND_REVIEW_FOLLOWUP, lock_project_operation
from classification_disagreements.ner import canonical
from django.shortcuts import get_object_or_404
from tasks.models import ReviewDecision, Submission, Task, TaskAssignment

from . import confidence_contract as types
from . import followup_contract as c
from . import followup_snapshots, second_review_origin, second_review_service, service
from .models import ReviewBatch, SecondReviewDecision


def hints(ctx, source_id, budget):
    row = followup_snapshots.one(
        budget.rows(types.source_query(ctx[2]).filter(pk=source_id).only('id', 'assignment_id', 'submitted_by_id'), {})
    )
    assignment = followup_snapshots.one(
        budget.rows(
            TaskAssignment.objects.filter(pk=row.assignment_id, project=ctx[2]).only(
                'id', 'task_id', 'assignee_id', 'project_id'
            ),
            {},
        )
    )
    reviews = budget.rows(ReviewDecision.objects.filter(submission_id=row.pk).only('id', 'reviewer_id'), {})
    return dict(
        submission=row.pk,
        assignment=assignment.pk,
        task=assignment.task_id,
        people=[row.submitted_by_id, assignment.assignee_id, *(r.reviewer_id for r in reviews)],
    )


def lock(request, ctx, task_id, target_id, budget):
    row = second_review_service.task(ctx, task_id, proof=True, budget=budget)
    before = [hints(ctx, row.submission_id_frozen, budget)]
    if target_id is not None:
        before.append(hints(ctx, target_id, budget))
    try:
        excluded = row.proof['excluded_actor_ids']
        if not isinstance(excluded, list) or len(excluded) > 100 or any(not c.positive(v) for v in excluded):
            c.fail()
    except (KeyError, TypeError):
        c.fail()
    people = {row.assignee_id, *excluded, *(v for item in before for v in item['people'])}
    if ctx[1].pk in people:
        c.fail('FORBIDDEN', 403)
    lock_project_operation(ctx[2].pk, ctx[0], RECORD_SECOND_REVIEW_FOLLOWUP, related_user_ids=people)
    c.recheck(request, ctx, 'record')
    for model, ids, filters in (
        (Task, {v['task'] for v in before}, dict(project=ctx[2])),
        (TaskAssignment, {v['assignment'] for v in before}, dict(project=ctx[2])),
        (Submission, {v['submission'] for v in before}, {}),
    ):
        found = list(
            model.objects.filter(pk__in=ids, **filters).order_by('pk').select_for_update().values_list('pk', flat=True)
        )
        if set(found) != ids:
            c.fail('SOURCE_UNAVAILABLE')
    list(
        ReviewDecision.objects.filter(submission_id__in=[v['submission'] for v in before])
        .order_by('pk')
        .select_for_update()
        .values_list('pk', flat=True)
    )
    get_object_or_404(
        ReviewBatch.objects.select_for_update().values('pk'), pk=row.review_batch_id_frozen, project=ctx[2]
    )
    get_object_or_404(second_review_service.scope(ctx).select_for_update().values('pk'), pk=row.pk)
    list(
        SecondReviewDecision.objects.filter(project=ctx[2], task_id_frozen=row.pk)
        .select_for_update()
        .values_list('pk', flat=True)
    )
    if before != [hints(ctx, v['submission'], budget) for v in before]:
        c.fail('SOURCE_CHANGED')


def first_review(row):
    review = getattr(row, 'review', None)
    if row.status == 'pending' and review is None:
        return None
    if (
        row.status not in ('approved', 'rejected')
        or review is None
        or review.decision != row.status
        or not c.positive(review.reviewer_id)
        or review.reviewer_id == row.submitted_by_id
        or not isinstance(review.created_at, datetime)
        or review.created_at.tzinfo is None
    ):
        c.fail('SOURCE_CHANGED')
    return dict(
        schema='first-review-proof/v1',
        id=review.pk,
        submission_id=row.pk,
        reviewer_id=review.reviewer_id,
        decision=review.decision,
        reason=review.reason,
        created_at=second_review_origin.utc(review.created_at),
    )


def load(ctx, task_id, target_id, budget):
    row = second_review_service.task(ctx, task_id, proof=True, budget=budget)
    opinion = row._second_opinion
    if row.status != 'completed' or opinion is None or opinion.outcome != 'concern':
        c.fail('SOURCE_INELIGIBLE')
    if ctx[1].pk in {*row.proof['excluded_actor_ids'], row.assignee_id_frozen}:
        c.fail('FORBIDDEN', 403)
    second_review_service.require_links(row)
    if opinion.created_by_id != opinion.actor_id_frozen:
        c.fail('SOURCE_UNAVAILABLE')
    source = second_review_origin.load(
        ctx,
        row.review_batch_id_frozen,
        row.submission_id_frozen,
        frozen=row.proof,
        budget=budget,
        snapshot_loader=followup_snapshots.load,
    )
    if source['row'].assignment.assignee_id != source['row'].submitted_by_id:
        c.fail('SOURCE_CHANGED')
    first_review(source['row'])
    excluded = {*source['proof']['excluded_actor_ids'], row.assignee_id_frozen}
    original = dict(
        task_id=str(row.pk),
        opinion_id=str(opinion.pk),
        dispatch_receipt_hash=row.receipt_hash,
        opinion_receipt_hash=opinion.receipt_hash,
        source=source['proof'],
        opinion=opinion.receipt,
    )
    related, target = None, None
    if target_id is not None:
        related = followup_snapshots.one(
            followup_snapshots.load(types.source_query(ctx[2]).filter(pk=target_id), budget)
        )
        if not service.valid_source(related, ctx[2].pk) or not types.supported(related):
            c.fail('SOURCE_CHANGED')
        pins = types.source_pins(related, ctx)
        old = source['proof']['subject']
        if (
            any(not c.positive(pins[k]) for k in types.PINS[:6])
            or pins['assignee_id'] != pins['submitter_id']
            or any(
                pins[k] != old[k]
                for k in (
                    'assignment_id',
                    'task_id',
                    'assignee_id',
                    'submitter_id',
                    'task_data_hash',
                    'label_config_sha256',
                )
            )
            or pins['revision'] <= old['revision']
            or related.pk == source['row'].pk
            or related.assignment.status not in ('assigned', 'in_progress')
            or Submission.objects.filter(assignment_id=related.assignment_id, revision__gt=related.revision).exists()
        ):
            c.fail('SOURCE_CHANGED')
        first = first_review(related)
        if first:
            excluded.add(first['reviewer_id'])
        target = dict(
            subject=pins,
            status_at_record=related.status,
            first_review=first,
            first_review_hash=c.digest(first) if first else None,
        )
    if ctx[1].pk in excluded:
        c.fail('FORBIDDEN', 403)
    concern_hash = c.digest(original)
    binding = dict(
        schema=c.SCHEMA,
        rule=c.RULE,
        effect=c.EFFECT,
        project_id=ctx[2].pk,
        organization_id=ctx[2].organization_id,
        instance_id=c.base.instance(),
        concern_hash=concern_hash,
        target=target,
    )
    return dict(
        task=row,
        origin_ref=original,
        concern_hash=concern_hash,
        target_ref=target,
        context_hash=c.digest(binding),
        source=source,
        related=related,
        original_presentation_hash=presentation_hash(source['row'].result_snapshot),
        target_presentation_hash=presentation_hash(related.result_snapshot) if related is not None else None,
    )


def representation(value):
    return c.digest(
        dict(
            context_hash=value['context_hash'],
            original=value['source']['row'].result_snapshot,
            historical_revision=value['source']['historical_revision'],
            target=value['related'].result_snapshot if value['related'] is not None else None,
        )
    )


def projection(value):
    source = value['source']
    return dict(
        subject=source['proof']['subject'],
        first_review=source['proof']['first_review'],
        result_snapshot=source['row'].result_snapshot,
        historical_revision=source['historical_revision'],
        presentation_hash=value['original_presentation_hash'],
    )


def presentation_hash(snapshot):
    """Browser-checkable representation, separate from legacy Python hash pins."""
    try:
        # JSON.parse represents every JSON number as binary64, including old
        # annotation metadata with integers outside the safe integer domain.
        # Hash that same representation without changing the frozen snapshot
        # or its authoritative legacy result_hash.
        c.bounded(snapshot, 256 * 1024)
        displayed = json.loads(canonical(snapshot), parse_int=float)
        data = rfc8785.dumps(displayed)
    except (ValueError, TypeError, OverflowError):
        c.fail('INPUT_UNSUPPORTED', 400)
    if len(data) > 256 * 1024:
        c.fail('TOO_LARGE', 422)
    return hashlib.sha256(data).hexdigest()
