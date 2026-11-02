"""Independent supervision: strict transport and current authority, never release policy."""
import json

from access_control.authorization import authorization
from access_control.identity import resolve_actor
from access_control.project_operations import (
    ORGANIZE_SECOND_REVIEW,
    READ_WORK,
    SECOND_REVIEW,
    require_project_operation,
)
from classification_disagreements import ner
from django.conf import settings
from django.core import signing
from django.db import connection
from django.shortcuts import get_object_or_404
from django.utils.crypto import salted_hmac
from projects.models import Project
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from tasks.my_work import work_projects

from . import confidence_contract as types
from . import service

SCHEMA = 'second-review-source/v1'
RULE = 'independent-supervision/v1'
INPUT = 'static-text-choices/v1'
EFFECT = 'supervisory_only'
MIB = 1024 * 1024
MAX_BODY = 16 * 1024
MAX_OUTPUT = 4 * MIB
positive, hexadecimal, uuid_string = types.positive, types.hexadecimal, types.uuid_string


def fail(code='CORRUPT', status=409):
    service.fail('SECOND_REVIEW_' + code, status)


def bounded(value, maximum=8 * MIB):
    try:
        return ner.bounded(value, maximum)
    except ner.Error:
        fail('TOO_LARGE', 422)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        fail()


def digest(value):
    bounded(value)
    return service.digest(value)


def instance():
    value = settings.ANNOTATION_SECOND_REVIEW_INSTANCE_ID
    if not uuid_string(value):
        fail('UNAVAILABLE', 403)
    return value


def guard(pk, principal, actor, action='read'):
    roles = ('manager',) if action in ('organize', 'cancel') else ('manager', 'reviewer')
    if not positive(pk) or not types.text(principal.principal_id, 512) or not types.text(principal.source, 128):
        fail('FORBIDDEN', 403)
    project = get_object_or_404(
        Project.objects.filter(pk__in=work_projects(actor, roles), deleted_at__isnull=True), pk=pk
    )
    modes = settings.ANNOTATION_SECOND_REVIEW_MODES
    if (
        not settings.ANNOTATION_SECOND_REVIEW_ENABLED
        or not isinstance(modes, dict)
        or modes.get(f'{project.organization_id}:{pk}') != 'fork_review'
        or connection.vendor not in ('postgresql', 'sqlite')
    ):
        fail('UNAVAILABLE', 403)
    project._second_instance = instance()
    role = authorization.project_role(principal, project)
    if role not in roles or action == 'decide' and role != 'reviewer':
        fail('FORBIDDEN', 403)
    if action != 'cancel':
        require_project_operation(project, READ_WORK)
    if action in ('organize', 'decide'):
        require_project_operation(project, ORGANIZE_SECOND_REVIEW if action == 'organize' else SECOND_REVIEW)
    return principal, actor, project, role


def current(request, pk, action='read'):
    return guard(pk, *resolve_actor(request), action)


def owner(ctx):
    p, a, project, role = ctx
    return digest(
        dict(
            principal=p.principal_id,
            source=p.source,
            actor=a.pk,
            project=project.pk,
            organization=project.organization_id,
            role=role,
            instance_id=getattr(project, '_second_instance', instance()),
        )
    )


def viewer(ctx):
    return salted_hmac(SCHEMA + '/viewer', owner(ctx), algorithm='sha256').hexdigest()


def recheck(request, ctx, action='read'):
    fresh = current(request, ctx[2].pk, action)
    if owner(fresh) != owner(ctx) or instance() != str(getattr(ctx[2], '_second_instance', instance())):
        fail('AUTHORITY_CHANGED', 403)
    return fresh


def respond(request, ctx, data, verify=None, *, action='read', status=200, maximum=MAX_OUTPUT):
    bounded(data, maximum)
    result = Response(data, status=status)
    result.accepted_renderer = JSONRenderer()
    result.accepted_media_type = 'application/json'
    result.renderer_context = {}
    emitted = digest(data)
    result.render()
    if len(result.content) > maximum:
        fail('TOO_LARGE', 422)
    if digest(json.loads(result.content)) != emitted:
        fail('REPRESENTATION_CHANGED')
    if verify is not None:
        verify()
    recheck(request, ctx, action)
    result['Cache-Control'] = 'private, no-store'
    result['Referrer-Policy'] = 'no-referrer'
    return result


def parse(data, action):
    keys = {
        'organize': {
            'batch_id',
            'submission_id',
            'expected_plan_hash',
            'expected_source_hash',
            'assignee_id',
            'expected_viewer_hash',
        },
        'decide': {'expected_version', 'expected_source_hash', 'outcome', 'reason', 'expected_viewer_hash'},
        'cancel': {'expected_version', 'reason', 'expected_viewer_hash'},
    }[action]
    bounded(data, MAX_BODY)
    if not isinstance(data, dict) or set(data) != keys:
        fail('INVALID_INPUT', 400)
    for key in keys:
        value = data[key]
        if key.endswith('_id') or key == 'expected_version':
            if not positive(value):
                fail('INVALID_INPUT', 400)
        elif key.endswith('_hash') and not hexadecimal(value):
            fail('INVALID_INPUT', 400)
    if action == 'decide' and data['outcome'] not in ('confirmed', 'concern'):
        fail('INVALID_INPUT', 400)
    if 'reason' in data:
        value = data['reason']
        if not isinstance(value, str) or '\x00' in value or len(value) > 2000:
            fail('INVALID_INPUT', 400)
        try:
            if len(value.encode('utf-8')) > 8192:
                fail('TOO_LARGE', 422)
        except UnicodeError:
            fail('INVALID_INPUT', 400)
        bounded(value, 8192)
        if (action == 'cancel' or data.get('outcome') == 'concern') and not value.strip():
            fail('INVALID_INPUT', 400)
    return data


def page(params, binding):
    if set(params) - {'cursor', 'page_size'} or any(len(v) != 1 for _, v in params.lists()):
        fail('INVALID_INPUT', 400)
    raw = params.get('page_size', '50')
    if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= 100:
        fail('INVALID_INPUT', 400)
    offset = 0
    token = params.get('cursor')
    if token:
        try:
            value = signing.loads(token, salt=SCHEMA, max_age=900)
            if (
                set(value) != {'binding', 'offset'}
                or value['binding'] != binding
                or not types.integer(value['offset'], 0, 2**31 - 1)
            ):
                raise ValueError()
            offset = value['offset']
        except (signing.BadSignature, ValueError, TypeError):
            fail('CURSOR_INVALID', 400)
    return offset, int(raw)


def cursor(binding, offset):
    return signing.dumps({'binding': binding, 'offset': offset}, salt=SCHEMA, compress=True)
