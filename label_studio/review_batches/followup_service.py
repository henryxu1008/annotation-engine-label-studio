"""One independent disposition; all fresh output stays inside its write fence."""

from uuid import UUID, uuid4

from access_control.project_operations import RECORD_SECOND_REVIEW_FOLLOWUP, lock_project_operation
from django.db import IntegrityError, transaction
from django.db.models import CharField, F, Value
from django.shortcuts import get_object_or_404
from projects.models import Project
from rest_framework.exceptions import PermissionDenied

from . import followup_contract as c
from . import followup_origin as origin
from . import followup_records as records
from . import second_review_service as second
from .models import SecondReviewConcernFollowup, SecondReviewDecision
from .second_review_bounded import Budget, typed_rows


def summary(task, opinion):
    return dict(
        task_id=str(task.pk),
        opinion_id=str(opinion.pk),
        submission_id=task.submission_id_frozen,
        revision=task.revision,
        batch_id=task.review_batch_id_frozen,
        opinion_created_at=opinion.receipt['created_at'],
        opinion=opinion.receipt,
        followup_id=None,
        recorded=None,
    )


def recorded_summary(row):
    source = row.origin_ref['source']
    return dict(
        task_id=str(row.task_id_frozen),
        opinion_id=str(row.opinion_id_frozen),
        submission_id=source['subject']['submission_id'],
        revision=source['subject']['revision'],
        batch_id=source['batch_id'],
        opinion_created_at=row.origin_ref['opinion']['created_at'],
        opinion=row.origin_ref['opinion'],
        followup_id=str(row.pk),
        recorded=row.receipt,
    )


@transaction.atomic
def history(request, ctx):
    budget = Budget()
    binding = c.digest(
        dict(schema=c.SCHEMA, owner=c.owner(ctx), selection='all-concerns/v1', order='opinion-time-uuid')
    )
    offset, size = c.page(request.query_params, binding)
    stored = records.scope(ctx)
    pending = SecondReviewDecision.objects.filter(
        project=ctx[2], outcome='concern', task__in=second.scope(ctx).filter(status='completed', version=2)
    ).exclude(pk__in=stored.values('opinion_id_frozen'))
    fields = ('kind', 'sort_time', 'sort_uuid', 'row_id')
    left = (
        pending.order_by()
        .annotate(
            kind=Value('pending', output_field=CharField()),
            sort_time=F('created_at'),
            sort_uuid=F('id'),
            row_id=F('id'),
        )
        .values(*fields)
    )
    right = (
        stored.order_by()
        .annotate(
            kind=Value('recorded', output_field=CharField()),
            sort_time=F('opinion_created_at'),
            sort_uuid=F('opinion_id_frozen'),
            row_id=F('id'),
        )
        .values(*fields)
    )
    union = left.union(right).order_by('sort_time', 'sort_uuid')
    count = union.count()
    page = list(typed_rows(union[offset : offset + size]))
    opinions = budget.rows(
        pending.filter(pk__in=[v['row_id'] for v in page if v['kind'] == 'pending']),
        {'receipt': second.PROOF_LIMIT, 'reason': (8192, 8192)},
    )
    tasks = budget.rows(
        second.scope(ctx)
        .filter(pk__in=[v.task_id_frozen for v in opinions])
        .only(
            'id',
            'project_id',
            'organization_id_frozen',
            'instance_id',
            'submission_id_frozen',
            'review_batch_id_frozen',
            'revision',
            'assignee_id_frozen',
            'source_hash',
            'first_review_hash',
            'status',
            'version',
        ),
        {},
    )
    by_task = {v.pk: v for v in tasks}
    values = {}
    for opinion in opinions:
        task = by_task.get(opinion.task_id_frozen)
        if (
            task is None
            or task.status != 'completed'
            or task.version != 2
            or opinion.task_id != task.pk
            or opinion.actor_id_frozen != task.assignee_id_frozen
            or c.digest(opinion.receipt) != opinion.receipt_hash
        ):
            c.fail()
        second.checked_receipt(
            opinion.receipt, 'decision', task, opinion.idempotency_key, opinion.request_hash, decision=opinion
        )
        values[('pending', opinion.pk)] = summary(task, opinion)
    for row in records.rows(stored.filter(pk__in=[v['row_id'] for v in page if v['kind'] == 'recorded']), budget):
        values[('recorded', row.pk)] = recorded_summary(row)
    try:
        results = [values[(v['kind'], v['row_id'])] for v in page]
    except KeyError:
        c.fail('SOURCE_CHANGED')
    try:
        c.recheck(request, ctx, 'record')
        can_record = True
    except PermissionDenied:
        can_record = False
    return c.respond(
        request,
        ctx,
        c.envelope(
            ctx,
            count=count,
            results=results,
            next=c.base.cursor(binding, offset + size) if offset + size < count else None,
            can_record=can_record,
            source_integrity='not_checked',
        ),
    )


@transaction.atomic
def detail(request, ctx, pk):
    return c.respond(request, ctx, records.metadata(records.get(ctx, pk, Budget()), ctx))


def ensure_unrecorded(ctx, row):
    if records.scope(ctx).filter(opinion_id_frozen=row._second_opinion.pk).exists():
        c.fail('FOLLOWUP_EXISTS')


@transaction.atomic
def context(request, ctx, task_id, target_id):
    budget = Budget()
    origin.lock(request, ctx, task_id, target_id, budget)
    value = origin.load(ctx, task_id, target_id, budget)
    ensure_unrecorded(ctx, value['task'])
    source = value['source']['row']
    # Discovery is metadata only and never silently becomes the selected target.
    from tasks.models import Submission

    newest_id = (
        Submission.objects.filter(assignment_id=source.assignment_id, revision__gt=source.revision)
        .order_by('-revision')
        .values_list('pk', flat=True)
        .first()
    )
    candidates = (
        budget.rows(Submission.objects.filter(pk=newest_id).only('id', 'revision', 'status'), {}) if newest_id else []
    )
    output = c.envelope(
        ctx,
        second_review_task_id=str(value['task'].pk),
        opinion_id=value['origin_ref']['opinion_id'],
        concern_hash=value['concern_hash'],
        context_hash=value['context_hash'],
        expected_version=0,
        original=origin.projection(value),
        opinion=value['origin_ref']['opinion'],
        target=(
            dict(
                **value['target_ref'],
                result_snapshot=value['related'].result_snapshot,
                presentation_hash=value['target_presentation_hash'],
            )
            if value['related'] is not None
            else None
        ),
        candidates=[dict(id=v.pk, revision=v.revision, status=v.status) for v in candidates],
        source_health='target_source_verified',
        metric_integrity='not_checked',
    )

    def verify():
        fresh = origin.load(ctx, task_id, target_id, budget)
        ensure_unrecorded(ctx, fresh['task'])
        if origin.representation(fresh) != origin.representation(value):
            c.fail('SOURCE_CHANGED')

    return c.respond(request, ctx, output, verify, action='record')


def recover(request, ctx, body, previous, budget):
    row = records.get(ctx, previous.pk, budget)
    if (
        row.receipt['owner'] != records.actor(ctx)
        or row.created_by_id != ctx[1].pk
        or row.request_hash != c.digest(body)
        or str(row.task_id_frozen) != body['second_review_task_id']
    ):
        c.fail('KEY_CONFLICT')
    return c.respond(
        request,
        ctx,
        c.envelope(ctx, receipt=row.receipt, replayed=True, source_integrity='not_checked'),
        action='record',
    )


def command(request, ctx, body, key):
    body = c.parse(body)
    try:
        return write(request, ctx, body, key)
    except IntegrityError as exc:
        # Project fencing serializes supported PG commands. The constraints
        # remain authoritative for any final insert race; never swallow guard
        # failures or other unexpected integrity defects.
        name = getattr(getattr(exc.__cause__, 'diag', None), 'constraint_name', None)
        if name in ('concern_followup_key_unique', 'concern_followup_subject_unique'):
            c.fail('KEY_CONFLICT' if name == 'concern_followup_key_unique' else 'FOLLOWUP_EXISTS')
        raise


@transaction.atomic
def write(request, ctx, body, key):
    budget = Budget()
    query = records.scope(ctx).filter(idempotency_key=key)
    previous = query.only('pk').first()
    # Acquire only the Project fence before reading any origin hints. A winner
    # may commit while we wait, and its receipt must be recovered even if its
    # sources have since disappeared. Human locks are acquired once afterwards
    # with the full related set (or caller-only on the source-free replay path).
    get_object_or_404(Project.objects.filter(pk=ctx[2].pk, deleted_at__isnull=True).select_for_update().values('pk'))
    c.recheck(request, ctx, 'record')
    previous = query.only('pk').first()
    if previous:
        lock_project_operation(ctx[2].pk, ctx[0], RECORD_SECOND_REVIEW_FOLLOWUP)
        c.recheck(request, ctx, 'record')
        return recover(request, ctx, body, previous, budget)
    task_id, target_id = UUID(body['second_review_task_id']), body['related_submission_id']
    origin.lock(request, ctx, task_id, target_id, budget)
    previous = query.only('pk').first()
    if previous:
        return recover(request, ctx, body, previous, budget)
    value = origin.load(ctx, task_id, target_id, budget)
    ensure_unrecorded(ctx, value['task'])
    if body['expected_viewer_hash'] != c.viewer(ctx):
        c.fail('VIEWER_CHANGED')
    if (
        body['expected_concern_hash'] != value['concern_hash']
        or body['expected_context_hash'] != value['context_hash']
    ):
        c.fail('SOURCE_CHANGED')
    from django.utils import timezone

    row = SecondReviewConcernFollowup(
        id=uuid4(),
        project=ctx[2],
        organization_id_frozen=ctx[2].organization_id,
        instance_id=c.base.instance(),
        second_review_task=value['task'],
        task_id_frozen=value['task'].pk,
        opinion=value['task']._second_opinion,
        opinion_id_frozen=value['task']._second_opinion.pk,
        opinion_created_at=value['task']._second_opinion.created_at,
        created_by=ctx[1],
        actor_id_frozen=ctx[1].pk,
        creator_principal=ctx[0].principal_id,
        creator_source=ctx[0].source,
        created_at=timezone.now(),
        concern_hash=value['concern_hash'],
        origin_ref=value['origin_ref'],
        disposition=body['disposition'],
        reason=body['reason'],
        related_submission=value['related'],
        related_submission_id_frozen=target_id,
        target_ref=value['target_ref'],
        version=1,
        idempotency_key=key,
        request_hash=c.digest(body),
    )
    row.receipt = records.receipt(row)
    row.receipt_hash = c.digest(row.receipt)
    c.bounded([row.origin_ref, row.target_ref, row.receipt, row.reason], records.LIMIT[0])
    records.checked(row)
    row.save(force_insert=True)

    def verify():
        fresh = origin.load(ctx, task_id, target_id, budget)
        if origin.representation(fresh) != origin.representation(value):
            c.fail('SOURCE_CHANGED')
        if records.get(ctx, row.pk, budget).receipt_hash != row.receipt_hash:
            c.fail()

    return c.respond(
        request,
        ctx,
        c.envelope(ctx, receipt=row.receipt, replayed=False, source_integrity='not_checked'),
        verify,
        action='record',
        status=201,
    )
