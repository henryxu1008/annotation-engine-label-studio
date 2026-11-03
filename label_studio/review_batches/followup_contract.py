"""Current Manager authority and strict transport for supervisory dispositions."""

from access_control.identity import resolve_actor
from access_control.project_operations import RECORD_SECOND_REVIEW_FOLLOWUP, require_project_operation
from django.conf import settings
from django.utils.crypto import salted_hmac
from rest_framework.exceptions import APIException

from . import second_review_contract as base

SCHEMA = 'second-review-concern-followup/v1'
RULE = 'independent-disposition/v1'
RECEIPT = 'second-review-followup-receipt/v1'
EFFECT = base.EFFECT
MAX_BODY, MAX_OUTPUT = base.MAX_BODY, base.MAX_OUTPUT
positive, hexadecimal, uuid_string = base.positive, base.hexadecimal, base.uuid_string
bounded, digest, fail = base.bounded, base.digest, base.fail


def guard(pk, principal, actor, action='read'):
    ctx = base.guard(pk, principal, actor)
    if not settings.ANNOTATION_SECOND_REVIEW_FOLLOWUP_ENABLED or ctx[3] != 'manager':
        fail('FORBIDDEN', 403)
    if action == 'record':
        require_project_operation(ctx[2], RECORD_SECOND_REVIEW_FOLLOWUP)
    return ctx


def current(request, pk, action='read'):
    return guard(pk, *resolve_actor(request), action)


def owner(ctx):
    return digest(dict(schema=SCHEMA, owner=base.owner(ctx)))


def viewer(ctx):
    return salted_hmac(SCHEMA + '/viewer', owner(ctx), algorithm='sha256').hexdigest()


def recheck(request, ctx, action='read'):
    fresh = current(request, ctx[2].pk, action)
    if owner(fresh) != owner(ctx):
        fail('AUTHORITY_CHANGED', 403)
    return fresh


def respond(request, ctx, data, verify=None, *, action='read', status=200):
    # Retain the existing final-render checks, then enforce the extension's
    # stricter current Manager/operation authority inside the same transaction.
    result = base.respond(request, ctx, data, verify, status=status)
    recheck(request, ctx, action)
    return result


def parse(data):
    try:
        bounded(data, MAX_BODY)
    except APIException as exc:
        if exc.status_code == 409:
            fail('INVALID_INPUT', 400)
        raise
    keys = {
        'second_review_task_id',
        'expected_concern_hash',
        'expected_context_hash',
        'expected_viewer_hash',
        'expected_version',
        'disposition',
        'reason',
        'related_submission_id',
    }
    if not isinstance(data, dict) or set(data) != keys:
        fail('INVALID_INPUT', 400)
    if (
        not uuid_string(data['second_review_task_id'])
        or any(
            not hexadecimal(data[k])
            for k in ('expected_concern_hash', 'expected_context_hash', 'expected_viewer_hash')
        )
        or type(data['expected_version']) is not int
        or data['expected_version'] != 0
        or data['disposition'] not in ('no_change', 'revision_linked')
    ):
        fail('INVALID_INPUT', 400)
    target = data['related_submission_id']
    if (data['disposition'] == 'no_change' and target is not None) or (
        data['disposition'] == 'revision_linked' and not positive(target)
    ):
        fail('INVALID_INPUT', 400)
    # The existing mandatory cancellation reason has exactly the required
    # Unicode/character/byte contract, without introducing a second normalizer.
    base.parse(dict(expected_version=1, expected_viewer_hash='0' * 64, reason=data['reason']), 'cancel')
    return data


def envelope(ctx, **data):
    return dict(effect=EFFECT, viewer_hash=viewer(ctx), owner_hash=owner(ctx), **data)


def page(params, binding):
    # Bound decimal conversion and signed-cursor decoding before the shared
    # pagination parser. Only the extension's new wire is affected.
    if len(params.get('page_size', '50')) > 3 or len(params.get('cursor', '')) > 4096:
        fail('INVALID_INPUT', 400)
    return base.page(params, binding)
