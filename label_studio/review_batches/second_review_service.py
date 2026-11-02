"""Transactional supervisory commands. Initial review and release are untouched."""
from datetime import datetime
from uuid import uuid4

from access_control.project_operations import (
    ORGANIZE_SECOND_REVIEW,
    SECOND_REVIEW,
    SECURITY_CANCEL,
    lock_project_operation,
)
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import ProjectMember
from users.models import User

from . import second_review_contract as c
from . import second_review_origin as origin
from .models import SecondReviewDecision, SecondReviewTask
from .second_review_bounded import Budget

RECEIPT = 'second-review-command-receipt/v1'
PROOF_LIMIT = (16 * 1024, 64 * 1024)


def scope(ctx):
    query = SecondReviewTask.objects.filter(
        project=ctx[2], organization_id_frozen=ctx[2].organization_id, instance_id=c.instance()
    )
    if ctx[3] == 'reviewer':
        query = query.filter(assignee_id=ctx[1].pk, assignee_id_frozen=ctx[1].pk)
    return query


def actor(ctx):
    return dict(principal=ctx[0].principal_id, source=ctx[0].source, actor_id=ctx[1].pk)


def receipt(command, ctx, task_id, key, request_hash, created_at, **data):
    return dict(
        schema=RECEIPT,
        command=command,
        task_id=str(task_id),
        project_id=ctx[2].pk,
        organization_id=ctx[2].organization_id,
        instance_id=c.instance(),
        owner=actor(ctx),
        key=str(key),
        request_hash=request_hash,
        created_at=origin.utc(created_at),
        effect=c.EFFECT,
        **data,
    )


def checked_receipt(value, command, row, key, request_hash, *, decision=None):
    try:
        source = decision or row
        expected_owner = dict(
            principal=source.creator_principal, source=source.creator_source, actor_id=source.actor_id_frozen
        )
        common = {
            'schema',
            'command',
            'task_id',
            'project_id',
            'organization_id',
            'instance_id',
            'owner',
            'key',
            'request_hash',
            'created_at',
            'effect',
            'source_hash',
            'version',
        }
        extra = {
            'dispatch': {'first_review_hash', 'assignee_id', 'batch_id', 'submission_id', 'revision'},
            'decision': {'first_review_hash', 'decision_id', 'outcome', 'reason'},
            'cancel': {'reason'},
        }[command]
        who = value['owner']
        timestamp = datetime.fromisoformat(value['created_at'].replace('Z', '+00:00'))
        valid = (
            isinstance(value, dict)
            and set(value) == common | extra
            and value['schema'] == RECEIPT
            and value['command'] == command
            and isinstance(who, dict)
            and set(who) == {'principal', 'source', 'actor_id'}
            and c.types.text(who['principal'], 512)
            and c.types.text(who['source'], 128)
            and c.positive(who['actor_id'])
            and timestamp.tzinfo is not None
            and origin.utc(timestamp) == value['created_at']
            and type(value['version']) is int
            and value['version'] == (1 if command == 'dispatch' else 2)
            and c.positive(value['project_id'])
            and c.positive(value['organization_id'])
            and c.uuid_string(value['task_id'])
            and c.uuid_string(value['instance_id'])
            and c.uuid_string(value['key'])
            and c.hexadecimal(value['request_hash'])
            and c.hexadecimal(value['source_hash'])
            and value['task_id'] == str(row.pk)
            and value['project_id'] == row.project_id
            and value['organization_id'] == row.organization_id_frozen
            and value['instance_id'] == str(row.instance_id)
            and value['key'] == str(key)
            and value['request_hash'] == request_hash
            and value['effect'] == c.EFFECT
        )
        if command != 'cancel':
            valid = valid and c.hexadecimal(value['first_review_hash'])
            valid = valid and value['owner'] == expected_owner and value['created_at'] == origin.utc(source.created_at)
            valid = (
                valid
                and value['source_hash'] == row.source_hash
                and value['first_review_hash'] == row.first_review_hash
            )
            if command == 'dispatch':
                valid = (
                    valid
                    and value['assignee_id'] == row.assignee_id_frozen
                    and value['batch_id'] == row.review_batch_id_frozen
                )
                valid = (
                    valid and value['submission_id'] == row.submission_id_frozen and value['revision'] == row.revision
                )
                valid = valid and all(
                    c.positive(value[k]) for k in ('assignee_id', 'batch_id', 'submission_id', 'revision')
                )
            else:
                valid = (
                    valid
                    and value['decision_id'] == str(source.pk)
                    and value['outcome'] == source.outcome
                    and value['reason'] == source.reason
                )
                valid = valid and c.uuid_string(value['decision_id']) and value['outcome'] in ('confirmed', 'concern')
                c.parse(
                    dict(
                        expected_version=1,
                        expected_source_hash=row.source_hash,
                        expected_viewer_hash='0' * 64,
                        reason=value['reason'],
                        outcome=value['outcome'],
                    ),
                    'decide',
                )
        else:
            valid = (
                valid and value['source_hash'] == row.source_hash and row.cancelled_by_id in (None, who['actor_id'])
            )
            c.parse(dict(expected_version=1, expected_viewer_hash='0' * 64, reason=value['reason']), 'cancel')
        if not valid:
            c.fail()
    except (KeyError, TypeError, AttributeError, ValueError):
        c.fail()
    return value


def task(ctx, task_id, *, proof=False, budget=None, cancel_only=False):
    budget = budget or Budget()
    limits = {'receipt': PROOF_LIMIT}
    if proof:
        limits['proof'] = PROOF_LIMIT
    rows = budget.rows(scope(ctx).filter(pk=task_id), limits, union=PROOF_LIMIT)
    if not rows:
        from rest_framework.exceptions import NotFound

        raise NotFound()
    row = rows[0]
    if (
        row.created_by_id not in (None, row.actor_id_frozen)
        or row.assignee_id not in (None, row.assignee_id_frozen)
        or row.review_batch_id not in (None, row.review_batch_id_frozen)
        or row.submission_id not in (None, row.submission_id_frozen)
    ):
        c.fail()
    if c.digest(row.receipt) != row.receipt_hash:
        c.fail()
    checked_receipt(row.receipt, 'dispatch', row, row.idempotency_key, row.request_hash)
    # Cancellation and opinions are bounded metadata; no batch, initial reason,
    # snapshot or proof is loaded on history/recovery paths.
    cancellation = budget.rows(scope(ctx).filter(pk=row.pk), {'cancellation': (32 * 1024, 128 * 1024)})[0].cancellation
    row.cancellation = cancellation
    opinion_query = SecondReviewDecision.objects.filter(project=ctx[2], task_id_frozen=row.pk)
    if cancel_only:
        # SECURITY_CANCEL has no READ_WORK authority. Do not hydrate even an
        # already recorded supervisory opinion just to reject its cancellation.
        if opinion_query.exists():
            c.fail('VERSION_CONFLICT' if row.status == 'completed' else 'CORRUPT')
        opinions = []
    else:
        opinions = budget.rows(opinion_query, {'receipt': (16 * 1024, 64 * 1024), 'reason': (8192, 8192)})
    if len(opinions) > 1:
        c.fail()
    opinion = opinions[0] if opinions else None
    if opinion:
        if (
            opinion.task_id != row.pk
            or opinion.source_hash != row.source_hash
            or opinion.first_review_hash != row.first_review_hash
            or opinion.actor_id_frozen != row.assignee_id_frozen
            or c.digest(opinion.receipt) != opinion.receipt_hash
        ):
            c.fail()
        checked_receipt(
            opinion.receipt, 'decision', row, opinion.idempotency_key, opinion.request_hash, decision=opinion
        )
    if row.status == 'pending':
        valid = (
            row.version == 1
            and opinion is None
            and cancellation is None
            and row.cancel_key is None
            and not row.cancellation_hash
        )
    elif row.status == 'completed':
        valid = (
            row.version == 2
            and opinion is not None
            and cancellation is None
            and row.cancel_key is None
            and not row.cancellation_hash
        )
    elif row.status == 'cancelled':
        valid = row.version == 2 and opinion is None and isinstance(cancellation, dict) and row.cancel_key is not None
        if valid:
            try:
                body = cancellation['body']
                ack = cancellation['receipt']
                valid = (
                    set(cancellation) == {'body', 'receipt'}
                    and c.digest(cancellation) == row.cancellation_hash
                    and c.parse(body, 'cancel') == body
                    and body['expected_version'] == 1
                    and c.digest(body) == ack['request_hash']
                    and ack['reason'] == body['reason']
                    and ack['version'] == 2
                    and ack['source_hash'] == row.source_hash
                )
                checked_receipt(ack, 'cancel', row, row.cancel_key, c.digest(body))
            except (KeyError, TypeError):
                valid = False
    else:
        valid = False
    if not valid:
        c.fail()
    if proof:
        try:
            valid_proof = (
                c.digest(row.proof) == row.source_hash
                and row.proof['effect'] == c.EFFECT
                and row.proof['rule'] == c.RULE
                and row.proof['schema'] == c.SCHEMA
                and row.proof['first_review_hash'] == row.first_review_hash == c.digest(row.proof['first_review'])
                and row.proof['batch_id'] == row.review_batch_id_frozen
                and row.proof['subject']['submission_id'] == row.submission_id_frozen
                and row.proof['subject']['revision'] == row.revision
                and row.proof['project_id'] == row.project_id
                and row.proof['organization_id'] == row.organization_id_frozen
                and row.proof['instance_id'] == str(row.instance_id)
            )
        except (KeyError, TypeError):
            valid_proof = False
        if not valid_proof:
            c.fail()
    row._second_opinion = opinion
    return row


def metadata(row, ctx):
    opinion = row._second_opinion
    return dict(
        id=str(row.pk),
        project_id=row.project_id,
        batch_id=row.review_batch_id_frozen,
        submission_id=row.submission_id_frozen,
        revision=row.revision,
        assignee_id=row.assignee_id_frozen,
        status=row.status,
        version=row.version,
        effect=c.EFFECT,
        source_hash=row.source_hash,
        first_review_hash=row.first_review_hash,
        created_at=origin.utc(row.created_at),
        source_integrity='not_checked',
        decision=opinion.receipt if opinion else None,
        cancellation=row.cancellation['receipt'] if row.cancellation else None,
        viewer_hash=c.viewer(ctx),
        owner_hash=c.owner(ctx),
    )


def candidates(ctx, source):
    project = ctx[2]
    org_users = OrganizationMember.objects.filter(
        organization_id=project.organization_id, deleted_at__isnull=True
    ).values('user_id')
    reviewers = ProjectMember.objects.filter(project=project, enabled=True, role='reviewer').values('user_id')
    return (
        User.objects.filter(pk__in=reviewers, is_active=True, active_organization_id=project.organization_id)
        .filter(pk__in=org_users)
        .exclude(pk=project.created_by_id)
        .exclude(pk__in=source['proof']['excluded_actor_ids'])
    )


def eligible(ctx, source, user_id):
    if not candidates(ctx, source).filter(pk=user_id).exists():
        c.fail('ASSIGNEE_INELIGIBLE', 403)


def verify_source(initial, ctx, batch_id, submission_id, *, fresh=False, frozen=None):
    again = origin.load(ctx, batch_id, submission_id, fresh=fresh, frozen=frozen)
    if origin.representation(again) != origin.representation(initial):
        c.fail('SOURCE_CHANGED')
    return again


def read_context(request, ctx, batch_id, submission_id, *, assignees=False):
    with transaction.atomic():
        before = origin.hints(ctx[2].pk, submission_id)
        lock_project_operation(ctx[2].pk, ctx[0], ORGANIZE_SECOND_REVIEW, related_user_ids=human_ids(before))
        c.recheck(request, ctx, 'organize')
        origin.lock_origin(ctx[2].pk, batch_id, submission_id, before)
        source = origin.load(ctx, batch_id, submission_id, fresh=True)
        existing_id = (
            scope(ctx)
            .filter(submission_id_frozen=submission_id, revision=source['proof']['subject']['revision'])
            .values_list('pk', flat=True)
            .first()
        )
        existing = metadata(task(ctx, existing_id), ctx) if existing_id else None
        data = dict(
            batch_id=batch_id,
            submission_id=submission_id,
            plan_hash=source['plan_hash'],
            source_hash=source['source_hash'],
            effect=c.EFFECT,
            viewer_hash=c.viewer(ctx),
            owner_hash=c.owner(ctx),
            eligible=existing is None,
            existing=existing,
            source_health='target_source_verified',
            metric_integrity='not_checked',
        )
        maximum = 128 * 1024
        if assignees:
            binding = dict(project=ctx[2].pk, source=source['source_hash'], viewer=c.viewer(ctx), order='id')
            offset, size = c.page(request.query_params, binding)
            query = candidates(ctx, source).order_by('pk')
            count = query.count()
            selected_ids = query.values_list('pk', flat=True)[offset : offset + size]
            rows = Budget(256 * 1024, 256 * 1024).rows(
                User.objects.filter(pk__in=selected_ids).order_by('pk').only('pk', 'first_name', 'last_name'),
                {'first_name': (1024, 1024), 'last_name': (1024, 1024)},
            )
            people = []
            for person in rows:
                name = ' '.join(v for v in (person.first_name, person.last_name) if v).strip() or str(person.pk)
                if len(name.encode('utf-8')) > 1024:
                    c.fail('TOO_LARGE', 422)
                people.append(dict(id=person.pk, display_name=name))
            data = dict(
                **data,
                count=count,
                results=people,
                next=c.cursor(binding, offset + size) if offset + size < count else None,
            )
            maximum = 256 * 1024
        elif request.query_params:
            c.fail('INVALID_INPUT', 400)
        return c.respond(
            request,
            ctx,
            data,
            lambda: verify_source(source, ctx, batch_id, submission_id, fresh=True),
            action='organize',
            maximum=maximum,
        )


def human_ids(hints):
    return [
        hints[k] for k in ('assignment__assignee_id', 'submitted_by_id', 'review__reviewer_id') if hints[k] is not None
    ]


def history(request, ctx, task_id=None):
    if task_id:
        row = task(ctx, task_id)
        data = metadata(row, ctx)
        return c.respond(request, ctx, data, lambda: same(data, metadata(task(ctx, task_id), ctx)))
    binding = dict(project=ctx[2].pk, viewer=c.viewer(ctx), order='created_at,id')
    offset, size = c.page(request.query_params, binding)
    ids = list(scope(ctx).order_by('created_at', 'pk').values_list('pk', flat=True)[offset : offset + size])
    budget = Budget(4 * c.MIB, 32 * c.MIB)
    data = dict(
        count=scope(ctx).count(),
        results=[metadata(task(ctx, pk, budget=budget), ctx) for pk in ids],
        next=c.cursor(binding, offset + size) if offset + size < scope(ctx).count() else None,
        viewer_hash=c.viewer(ctx),
        owner_hash=c.owner(ctx),
        effect=c.EFFECT,
    )
    # Bounded metadata is reread after render, with no source hydration.
    def verify():
        repeated = Budget(4 * c.MIB, 32 * c.MIB)
        same(data['results'], [metadata(task(ctx, pk, budget=repeated), ctx) for pk in ids])

    return c.respond(request, ctx, data, verify)


def same(left, right):
    if c.digest(left) != c.digest(right):
        c.fail('REPRESENTATION_CHANGED')


def source_response(request, ctx, task_id):
    with transaction.atomic():
        # Only scalar lookup precedes the Project/member fence.
        hint = get_object_or_404(
            scope(ctx).values('review_batch_id_frozen', 'submission_id_frozen', 'assignee_id'), pk=task_id
        )
        before = origin.hints(ctx[2].pk, hint['submission_id_frozen'])
        lock_project_operation(
            ctx[2].pk, ctx[0], 'read_work', related_user_ids=[*human_ids(before), hint['assignee_id']]
        )
        c.recheck(request, ctx)
        origin.lock_origin(ctx[2].pk, hint['review_batch_id_frozen'], hint['submission_id_frozen'], before)
        get_object_or_404(scope(ctx).select_for_update().values('pk'), pk=task_id)
        row = task(ctx, task_id, proof=True)
        require_links(row)
        source = origin.load(ctx, row.review_batch_id_frozen, row.submission_id_frozen, frozen=row.proof)
        data = dict(
            task=metadata(row, ctx),
            result_snapshot=source['row'].result_snapshot,
            first_review=source['proof']['first_review'],
            subject=source['proof']['subject'],
            historical_revision=source['historical_revision'],
            source_health='target_source_verified',
            metric_integrity='not_checked',
            viewer_hash=c.viewer(ctx),
            owner_hash=c.owner(ctx),
            effect=c.EFFECT,
        )
        return c.respond(
            request,
            ctx,
            data,
            lambda: verify_source(source, ctx, row.review_batch_id_frozen, row.submission_id_frozen, frozen=row.proof),
        )


def require_links(row):
    if (
        row.review_batch_id != row.review_batch_id_frozen
        or row.submission_id != row.submission_id_frozen
        or row.assignee_id != row.assignee_id_frozen
        or row.first_review_id != row.proof['first_review']['id']
        or row.first_reviewer_id != row.proof['first_review']['reviewer_id']
        or row.original_submitter_id != row.proof['subject']['submitter_id']
    ):
        c.fail('SOURCE_UNAVAILABLE')


def recover(request, ctx, action, body, key, previous):
    request_hash = c.digest(body)
    if action == 'organize':
        row = task(ctx, previous.pk)
        ack, owner_id = row.receipt, row.created_by_id
    elif action == 'decide':
        row = task(ctx, previous.task_id_frozen)
        opinion = row._second_opinion
        ack, owner_id = opinion.receipt, opinion.created_by_id
        if row.assignee_id != ctx[1].pk:
            c.fail('FORBIDDEN', 403)
    else:
        row = task(ctx, previous.pk, cancel_only=True)
        ack, owner_id = row.cancellation['receipt'], row.cancelled_by_id
    if ack['owner'] != actor(ctx) or owner_id != ctx[1].pk or ack['request_hash'] != request_hash:
        c.fail('KEY_CONFLICT')
    result = dict(
        receipt=ack, replayed=True, source_integrity='not_checked', viewer_hash=c.viewer(ctx), owner_hash=c.owner(ctx)
    )
    return c.respond(request, ctx, result, action=action)


def command(request, ctx, action, body, key, task_id=None):
    body = c.parse(body, action)
    with transaction.atomic():
        # Receipt recovery must be selected BEFORE any live-origin lookup.
        query = (
            SecondReviewDecision.objects.filter(project=ctx[2], idempotency_key=key)
            if action == 'decide'
            else scope(ctx).filter(**({'idempotency_key': key} if action == 'organize' else {'cancel_key': key}))
        )
        previous = query.only('pk', *(['task_id_frozen'] if action == 'decide' else [])).first()
        if previous:
            lock_project_operation(
                ctx[2].pk,
                ctx[0],
                {'organize': ORGANIZE_SECOND_REVIEW, 'decide': SECOND_REVIEW, 'cancel': SECURITY_CANCEL}[action],
            )
            c.recheck(request, ctx, action)
            expected_task = previous.task_id_frozen if action == 'decide' else previous.pk
            if task_id and expected_task != task_id:
                c.fail('KEY_CONFLICT')
            return recover(request, ctx, action, body, key, previous)
        before = None
        if action == 'organize':
            batch_id, source_id = body['batch_id'], body['submission_id']
            before = origin.hints(ctx[2].pk, source_id)
            users = [*human_ids(before), body['assignee_id']]
        elif action == 'decide':
            hint = get_object_or_404(
                scope(ctx).values('review_batch_id_frozen', 'submission_id_frozen', 'assignee_id'), pk=task_id
            )
            batch_id, source_id = hint['review_batch_id_frozen'], hint['submission_id_frozen']
            before = origin.hints(ctx[2].pk, source_id)
            users = [*human_ids(before), hint['assignee_id']]
        else:
            users = []
        lock_project_operation(
            ctx[2].pk,
            ctx[0],
            {'organize': ORGANIZE_SECOND_REVIEW, 'decide': SECOND_REVIEW, 'cancel': SECURITY_CANCEL}[action],
            related_user_ids=users,
        )
        c.recheck(request, ctx, action)
        # A contender may have committed while this command waited on Project.
        previous = query.only('pk', *(['task_id_frozen'] if action == 'decide' else [])).first()
        if previous:
            if task_id and (previous.task_id_frozen if action == 'decide' else previous.pk) != task_id:
                c.fail('KEY_CONFLICT')
            return recover(request, ctx, action, body, key, previous)
        if body['expected_viewer_hash'] != c.viewer(ctx):
            c.fail('VIEWER_CHANGED')
        source = None
        if before is not None:
            origin.lock_origin(ctx[2].pk, batch_id, source_id, before)
        if action != 'organize':
            get_object_or_404(scope(ctx).select_for_update().values('pk'), pk=task_id)
            row = task(ctx, task_id, proof=action == 'decide', cancel_only=action == 'cancel')
            if row.status != 'pending' or row.version != body['expected_version']:
                c.fail('VERSION_CONFLICT')
        if action in ('organize', 'decide'):
            if action == 'decide':
                require_links(row)
                if row.assignee_id != ctx[1].pk:
                    c.fail('FORBIDDEN', 403)
            source = origin.load(
                ctx, batch_id, source_id, fresh=action == 'organize', frozen=row.proof if action == 'decide' else None
            )
            if source['source_hash'] != body['expected_source_hash']:
                c.fail('SOURCE_CHANGED')
        now, request_hash = timezone.now(), c.digest(body)
        if action == 'organize':
            if source['plan_hash'] != body['expected_plan_hash']:
                c.fail('SOURCE_CHANGED')
            eligible(ctx, source, body['assignee_id'])
            if (
                scope(ctx)
                .filter(submission_id_frozen=source_id, revision=source['proof']['subject']['revision'])
                .exists()
            ):
                c.fail('SUBJECT_EXISTS')
            task_id = uuid4()
            ack = receipt(
                'dispatch',
                ctx,
                task_id,
                key,
                request_hash,
                now,
                source_hash=source['source_hash'],
                first_review_hash=source['first_review_hash'],
                batch_id=batch_id,
                submission_id=source_id,
                revision=source['proof']['subject']['revision'],
                assignee_id=body['assignee_id'],
                version=1,
            )
            if c.bounded(source['proof']) + c.bounded(ack) > PROOF_LIMIT[0]:
                c.fail('TOO_LARGE', 422)
            row = SecondReviewTask.objects.create(
                id=task_id,
                project=ctx[2],
                organization_id_frozen=ctx[2].organization_id,
                instance_id=c.instance(),
                review_batch_id=batch_id,
                review_batch_id_frozen=batch_id,
                submission_id=source_id,
                submission_id_frozen=source_id,
                revision=ack['revision'],
                assignee_id=body['assignee_id'],
                first_review_id=source['proof']['first_review']['id'],
                first_reviewer_id=source['proof']['first_review']['reviewer_id'],
                original_submitter_id=source['proof']['subject']['submitter_id'],
                assignee_id_frozen=body['assignee_id'],
                created_by=ctx[1],
                actor_id_frozen=ctx[1].pk,
                creator_principal=ctx[0].principal_id,
                creator_source=ctx[0].source,
                created_at=now,
                source_hash=source['source_hash'],
                first_review_hash=source['first_review_hash'],
                proof=source['proof'],
                receipt=ack,
                receipt_hash=c.digest(ack),
                idempotency_key=key,
                request_hash=request_hash,
            )
        elif action == 'decide':
            decision_id = uuid4()
            ack = receipt(
                'decision',
                ctx,
                row.pk,
                key,
                request_hash,
                now,
                decision_id=str(decision_id),
                source_hash=row.source_hash,
                first_review_hash=row.first_review_hash,
                outcome=body['outcome'],
                reason=body['reason'],
                version=2,
            )
            SecondReviewDecision.objects.create(
                id=decision_id,
                project=ctx[2],
                task=row,
                task_id_frozen=row.pk,
                created_by=ctx[1],
                actor_id_frozen=ctx[1].pk,
                creator_principal=ctx[0].principal_id,
                creator_source=ctx[0].source,
                created_at=now,
                source_hash=row.source_hash,
                first_review_hash=row.first_review_hash,
                outcome=body['outcome'],
                reason=body['reason'],
                idempotency_key=key,
                request_hash=request_hash,
                receipt=ack,
                receipt_hash=c.digest(ack),
            )
            row.status, row.version = 'completed', 2
            row.save(update_fields=['status', 'version'])
        else:
            ack = receipt(
                'cancel',
                ctx,
                row.pk,
                key,
                request_hash,
                now,
                source_hash=row.source_hash,
                reason=body['reason'],
                version=2,
            )
            cancellation = dict(body=body, receipt=ack)
            row.status, row.version, row.cancel_key = 'cancelled', 2, key
            row.cancelled_by, row.cancellation, row.cancellation_hash = ctx[1], cancellation, c.digest(cancellation)
            row.save(
                update_fields=['status', 'version', 'cancel_key', 'cancelled_by', 'cancellation', 'cancellation_hash']
            )
        # Validate the persisted union, not just the in-memory objects.
        task(ctx, row.pk, cancel_only=action == 'cancel')

        def verify():
            if source is not None:
                verify_source(
                    source,
                    ctx,
                    batch_id,
                    source_id,
                    fresh=action == 'organize',
                    frozen=row.proof if action == 'decide' else None,
                )
                if action == 'organize':
                    eligible(ctx, source, body['assignee_id'])
            task(ctx, row.pk, cancel_only=action == 'cancel')

        return c.respond(
            request,
            ctx,
            dict(
                receipt=ack,
                replayed=False,
                source_integrity='not_checked',
                viewer_hash=c.viewer(ctx),
                owner_hash=c.owner(ctx),
            ),
            verify,
            action=action,
            status=201,
        )
