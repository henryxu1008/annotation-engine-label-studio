"""Self-contained frozen follow-up validation; never consults an origin row."""

from datetime import datetime

from rest_framework.exceptions import APIException

from . import confidence_contract as types
from . import confidence_priority, priority, service
from . import followup_contract as c
from . import second_review_origin as origin
from .models import SecondReviewConcernFollowup

LIMIT = (32 * 1024, 128 * 1024)
PAYLOADS = {'origin_ref': LIMIT, 'target_ref': LIMIT, 'receipt': LIMIT, 'reason': (8192, 8192)}


def scope(ctx):
    return SecondReviewConcernFollowup.objects.filter(
        project=ctx[2], organization_id_frozen=ctx[2].organization_id, instance_id=c.base.instance()
    )


def actor(ctx):
    return dict(principal=ctx[0].principal_id, source=ctx[0].source, actor_id=ctx[1].pk)


def context_hash(project_id, organization_id, instance_id, concern_hash, target):
    return c.digest(
        dict(
            schema=c.SCHEMA,
            rule=c.RULE,
            effect=c.EFFECT,
            project_id=project_id,
            organization_id=organization_id,
            instance_id=str(instance_id),
            concern_hash=concern_hash,
            target=target,
        )
    )


def receipt(row):
    return dict(
        schema=c.RECEIPT,
        record_schema=c.SCHEMA,
        rule=c.RULE,
        command='record',
        followup_id=str(row.pk),
        project_id=row.project_id,
        organization_id=row.organization_id_frozen,
        instance_id=str(row.instance_id),
        owner=dict(principal=row.creator_principal, source=row.creator_source, actor_id=row.actor_id_frozen),
        key=str(row.idempotency_key),
        request_hash=row.request_hash,
        created_at=origin.utc(row.created_at),
        effect=c.EFFECT,
        version=1,
        second_review_task_id=str(row.task_id_frozen),
        opinion_id=str(row.opinion_id_frozen),
        concern_hash=row.concern_hash,
        origin_ref_hash=c.digest(row.origin_ref),
        context_hash=context_hash(
            row.project_id, row.organization_id_frozen, row.instance_id, row.concern_hash, row.target_ref
        ),
        disposition=row.disposition,
        reason=row.reason,
        related_submission_id=row.related_submission_id_frozen,
        target_ref_hash=c.digest(row.target_ref),
    )


def pins(value):
    return (
        isinstance(value, dict)
        and set(value) == set(types.PINS)
        and all(c.positive(value[k]) for k in types.PINS[:6])
        and all(c.hexadecimal(value[k]) for k in types.PINS[6:])
    )


def first(value, subject):
    if not isinstance(value, dict) or set(value) != {
        'schema',
        'id',
        'submission_id',
        'reviewer_id',
        'decision',
        'reason',
        'created_at',
    }:
        return False
    try:
        timestamp = datetime.fromisoformat(value['created_at'].replace('Z', '+00:00'))
        c.bounded(value['reason'], 8192)
        return (
            value['schema'] == 'first-review-proof/v1'
            and all(c.positive(value[k]) for k in ('id', 'submission_id', 'reviewer_id'))
            and value['submission_id'] == subject['submission_id']
            and value['reviewer_id'] != subject['submitter_id']
            and value['decision'] in ('approved', 'rejected')
            and isinstance(value['reason'], str)
            and timestamp.tzinfo is not None
            and origin.utc(timestamp) == value['created_at']
        )
    except (TypeError, ValueError, AttributeError):
        return False


def checked(row):
    try:
        ref = row.origin_ref
        source, opinion = ref['source'], ref['opinion']
        subject, initial = source['subject'], source['first_review']
        excluded = source['excluded_actor_ids']
        valid = (
            set(ref) == {'task_id', 'opinion_id', 'dispatch_receipt_hash', 'opinion_receipt_hash', 'source', 'opinion'}
            and set(source)
            == {
                'schema',
                'input_contract',
                'rule',
                'effect',
                'instance_id',
                'organization_id',
                'project_id',
                'batch_id',
                'plan_hash',
                'algorithm',
                'priority_schema',
                'priority_hash',
                'candidate',
                'subject',
                'first_review',
                'first_review_hash',
                'excluded_actor_ids',
            }
            and set(opinion)
            == {
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
                'first_review_hash',
                'decision_id',
                'outcome',
                'reason',
            }
            and set(opinion['owner']) == {'principal', 'source', 'actor_id'}
            and types.text(opinion['owner']['principal'], 512)
            and types.text(opinion['owner']['source'], 128)
            and c.uuid_string(opinion['key'])
            and c.hexadecimal(opinion['request_hash'])
            and ref['task_id'] == str(row.task_id_frozen)
            and ref['opinion_id'] == str(row.opinion_id_frozen)
            and c.hexadecimal(ref['dispatch_receipt_hash'])
            and ref['opinion_receipt_hash'] == c.digest(opinion)
            and source['schema'] == c.base.SCHEMA
            and source['rule'] == c.base.RULE
            and source['input_contract'] == c.base.INPUT
            and source['effect'] == c.EFFECT
            and source['project_id'] == row.project_id
            and source['organization_id'] == row.organization_id_frozen
            and c.positive(source['project_id'])
            and c.positive(source['organization_id'])
            and c.uuid_string(source['instance_id'])
            and source['instance_id'] == str(row.instance_id)
            and pins(subject)
            and first(initial, subject)
            and subject['assignee_id'] == subject['submitter_id']
            and c.positive(source['batch_id'])
            and c.hexadecimal(source['plan_hash'])
            and source['algorithm'] in (service.ALGORITHM, priority.ALGORITHM, confidence_priority.ALGORITHM)
            and (
                source['algorithm'] == service.ALGORITHM
                and source['priority_schema'] is None
                and source['priority_hash'] is None
                or source['algorithm'] != service.ALGORITHM
                and types.text(source['priority_schema'], 256)
                and c.hexadecimal(source['priority_hash'])
            )
            and c.digest(source['candidate'])
            == c.digest(
                {
                    **{
                        k: subject[k]
                        for k in (
                            'submission_id',
                            'assignment_id',
                            'task_id',
                            'revision',
                            'result_hash',
                            'submitter_id',
                        )
                    },
                    'selected': True,
                }
            )
            and source['first_review_hash'] == c.digest(initial)
            and isinstance(excluded, list)
            and len(excluded) <= 100
            and all(c.positive(v) for v in excluded)
            and excluded == sorted(set(excluded))
            and {subject['submitter_id'], subject['assignee_id'], initial['reviewer_id']}.issubset(excluded)
            and opinion['schema'] == 'second-review-command-receipt/v1'
            and opinion['command'] == 'decision'
            and opinion['decision_id'] == str(row.opinion_id_frozen)
            and opinion['task_id'] == str(row.task_id_frozen)
            and opinion['project_id'] == row.project_id
            and opinion['organization_id'] == row.organization_id_frozen
            and c.positive(opinion['project_id'])
            and c.positive(opinion['organization_id'])
            and opinion['instance_id'] == str(row.instance_id)
            and opinion['outcome'] == 'concern'
            and opinion['effect'] == c.EFFECT
            and type(opinion['version']) is int
            and opinion['version'] == 2
            and opinion['source_hash'] == c.digest(source)
            and opinion['first_review_hash'] == source['first_review_hash']
            and c.positive(opinion['owner']['actor_id'])
            and row.actor_id_frozen not in {*excluded, opinion['owner']['actor_id']}
            and opinion['created_at'] == origin.utc(row.opinion_created_at)
            and row.concern_hash == c.digest(ref)
            and c.hexadecimal(row.request_hash)
            and row.created_by_id in (None, row.actor_id_frozen)
            and row.second_review_task_id in (None, row.task_id_frozen)
            and row.opinion_id in (None, row.opinion_id_frozen)
            and row.related_submission_id in (None, row.related_submission_id_frozen)
            and type(row.version) is int
            and row.version == 1
            and types.text(row.creator_principal, 512)
            and types.text(row.creator_source, 128)
        )
        c.base.parse(
            dict(
                expected_version=1,
                expected_source_hash=opinion['source_hash'],
                expected_viewer_hash='0' * 64,
                outcome='concern',
                reason=opinion['reason'],
            ),
            'decide',
        )
        target = row.target_ref
        if row.disposition == 'no_change':
            valid = valid and target is None and row.related_submission_id_frozen is None
        elif row.disposition == 'revision_linked':
            target_pins = target['subject']
            review = target['first_review']
            valid = (
                valid
                and set(target) == {'subject', 'status_at_record', 'first_review', 'first_review_hash'}
                and pins(target_pins)
                and target_pins['submission_id'] == row.related_submission_id_frozen
                and target_pins['revision'] > subject['revision']
                and all(
                    target_pins[k] == subject[k]
                    for k in (
                        'assignment_id',
                        'task_id',
                        'assignee_id',
                        'submitter_id',
                        'task_data_hash',
                        'label_config_sha256',
                    )
                )
                and (
                    target['status_at_record'] == 'pending'
                    and review is None
                    and target['first_review_hash'] is None
                    or target['status_at_record'] in ('approved', 'rejected')
                    and first(review, target_pins)
                    and review['decision'] == target['status_at_record']
                    and target['first_review_hash'] == c.digest(review)
                    and review['reviewer_id'] != row.actor_id_frozen
                )
            )
        else:
            valid = False
        c.base.parse(dict(expected_version=1, expected_viewer_hash='0' * 64, reason=row.reason), 'cancel')
        expected = receipt(row)
        if not valid or c.digest(row.receipt) != row.receipt_hash or c.digest(row.receipt) != c.digest(expected):
            c.fail()
    except APIException as error:
        if error.status_code == 400:
            c.fail()
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        c.fail()
    return row


def rows(query, budget):
    return [checked(row) for row in budget.rows(query, PAYLOADS, union=LIMIT)]


def get(ctx, pk, budget):
    found = rows(scope(ctx).filter(pk=pk), budget)
    if not found:
        from django.http import Http404

        raise Http404
    return found[0]


def metadata(row, ctx):
    source = row.origin_ref['source']
    return c.envelope(
        ctx,
        id=str(row.pk),
        receipt=row.receipt,
        source_integrity='not_checked',
        metric_integrity='not_checked',
        original=dict(
            subject=source['subject'], first_review=source['first_review'], opinion=row.origin_ref['opinion']
        ),
        target=row.target_ref,
    )
