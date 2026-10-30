"""GET-only field authority, bounded source loading and signed frozen replay."""

import hashlib
import json
import re

from access_control.authorization import authorization
from access_control.identity import resolve_actor
from access_control.project_operations import READ_WORK, require_project_operation
from django.conf import settings
from django.core import signing
from django.db import connection
from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from projects.models import Project
from rest_framework.exceptions import NotFound
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from review_batches.service import fail, valid_source
from tasks.models import Submission, Task
from tasks.my_work import work_projects

from . import fields
from .api import ComparisonAPI

PIN_IDS = ('submission_id', 'assignment_id', 'assignee_id', 'task_id', 'revision', 'submitter_id')
PIN_HASHES = ('result_hash', 'config_hash', 'task_data_hash')
HEX = re.compile(r'^[0-9a-f]{64}$')


def positive(value):
    return type(value) is int and 0 < value <= 2**53 - 1


def parse_id(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 16:
        raise fields.Error('FIELD_INVALID_INPUT', 400)
    result = int(value)
    if not positive(result):
        raise fields.Error('FIELD_INVALID_INPUT', 400)
    return result


def metadata(query):
    return list(
        query.order_by('pk').values(
            'id',
            'assignment_id',
            'assignment__assignee_id',
            'assignment__task_id',
            'submitted_by_id',
            'revision',
            'result_hash',
            'status',
        )[: fields.MAX_SOURCES + 1]
    )


def pin(row):
    snapshot = row.result_snapshot
    xml = snapshot['project'].get('label_config')
    return {
        'submission_id': row.pk,
        'assignment_id': row.assignment_id,
        'assignee_id': row.assignment.assignee_id,
        'task_id': row.assignment.task_id,
        'revision': row.revision,
        'submitter_id': row.submitted_by_id,
        'result_hash': row.result_hash,
        'config_hash': hashlib.sha256(xml.encode()).hexdigest() if isinstance(xml, str) else fields.digest(xml),
        'task_data_hash': fields.digest(snapshot['task'].get('data')),
    }


def viewer(context, expected=None):
    principal, actor, project, role = context
    identity = fields.digest(
        {
            'principal': principal.principal_id,
            'source': principal.source,
            'actor': actor.pk,
            'organization': project.organization_id,
            'project': project.pk,
            'role': role,
        }
    )
    candidates = [
        salted_hmac(fields.SCHEMA + '/viewer', identity, secret=key, algorithm='sha256').hexdigest()
        for key in (settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS)
    ]
    if expected is not None:
        if not isinstance(expected, str) or not any(constant_time_compare(expected, v) for v in candidates):
            raise fields.Error('FIELD_AUTHORITY_CHANGED', 409)
        return expected
    return candidates[0]


def decode(token, project_id, task_id):
    if not isinstance(token, str) or not token or token.startswith('.') or len(token.encode()) > fields.MAX_TOKEN:
        raise fields.Error('FIELD_INVALID_RECEIPT', 400)
    try:
        value = signing.loads(token, salt=fields.SCHEMA)
    except (signing.BadSignature, ValueError, TypeError, UnicodeError):
        raise fields.Error('FIELD_INVALID_RECEIPT', 400)
    keys = {
        'schema',
        'rule',
        'explanation',
        'engine',
        'project_id',
        'task_id',
        'inputs',
        'viewer',
        'calculated_at',
        'result_hash',
        'configured_fields',
    }
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or not positive(value['project_id'])
        or not positive(value['task_id'])
    ):
        raise fields.Error('FIELD_INVALID_RECEIPT', 400)
    fields.bounded(value, fields.MAX_TOKEN)
    if value['project_id'] != project_id or value['task_id'] != task_id:
        raise NotFound()
    if value['configured_fields'] is not None and (
        type(value['configured_fields']) is not int or not 1 <= value['configured_fields'] <= fields.MAX_FIELDS
    ):
        raise fields.Error('FIELD_INVALID_RECEIPT', 400)
    if (
        value['schema'] != fields.SCHEMA
        or value['rule'] != fields.RULE
        or value['explanation'] != fields.EXPLANATION
        or value['engine'] != fields.ENGINE
        or not isinstance(value['calculated_at'], str)
        or not isinstance(value['result_hash'], str)
        or not HEX.fullmatch(value['result_hash'])
        or not isinstance(value['viewer'], str)
        or not HEX.fullmatch(value['viewer'])
        or not isinstance(value['inputs'], list)
        or not 2 <= len(value['inputs']) <= fields.MAX_INPUTS
    ):
        raise fields.Error('FIELD_INVALID_RECEIPT', 400)
    try:
        when = timezone.datetime.fromisoformat(value['calculated_at'])
        if timezone.is_naive(when):
            raise ValueError()
    except (ValueError, TypeError):
        raise fields.Error('FIELD_INVALID_RECEIPT', 400)
    for item in value['inputs']:
        if (
            not isinstance(item, dict)
            or set(item) != set(PIN_IDS + PIN_HASHES)
            or any(not positive(item[k]) for k in PIN_IDS)
            or any(not isinstance(item[k], str) or not HEX.fullmatch(item[k]) for k in PIN_HASHES)
            or item['task_id'] != task_id
            or item['assignee_id'] != item['submitter_id']
        ):
            raise fields.Error('FIELD_INVALID_RECEIPT', 400)
    ids = [p['submission_id'] for p in value['inputs']]
    if ids != sorted(set(ids)) or len({p['submitter_id'] for p in value['inputs']}) != len(ids):
        raise fields.Error('FIELD_INVALID_RECEIPT', 400)
    return value


class FieldAPI(ComparisonAPI):
    renderer_classes = [JSONRenderer]

    def context(self, request, pk, task_id=None):
        principal, actor = resolve_actor(request)
        project = get_object_or_404(
            Project.objects.filter(pk__in=work_projects(actor, ['manager', 'reviewer']), deleted_at__isnull=True),
            pk=pk,
        )
        modes = settings.ANNOTATION_FIELD_DISAGREEMENT_MODES
        if (
            not settings.ANNOTATION_FIELD_DISAGREEMENTS_ENABLED
            or not isinstance(modes, dict)
            or modes.get(f'{project.organization_id}:{project.pk}') != 'fork_review'
        ):
            raise NotFound()
        role = authorization.project_role(principal, project)
        if role not in ('manager', 'reviewer'):
            raise NotFound()
        require_project_operation(project, READ_WORK)
        if connection.vendor not in ('postgresql', 'sqlite'):
            fail('FIELD_BACKEND_UNSUPPORTED', 503)
        if task_id is not None and not Task.objects.filter(pk=task_id, project=project).exists():
            raise NotFound()
        return principal, actor, project, role

    def recheck(self, request, pk, task_id, initial):
        current = self.context(request, pk, task_id)
        if current[0] != initial[0] or current[1].pk != initial[1].pk or current[3] != initial[3]:
            raise fields.Error('FIELD_AUTHORITY_CHANGED', 409)
        return current

    def query_sources(self, context, task_id):
        _, actor, project, role = context
        query = Submission.objects.filter(
            assignment__project=project,
            assignment__task_id=task_id,
            assignment__task__project=project,
            submitted_by_id=F('assignment__assignee_id'),
        )
        return query.exclude(submitted_by_id=actor.pk) if role == 'reviewer' else query

    def respond(self, request, body, verify, validate=None, *, status=200):
        fields.bounded(body)
        before = fields.digest(body)
        response = self.finalize_response(request, Response(body, status=status))
        response.render()
        if len(response.content) > fields.MAX_OUTPUT:
            raise fields.Error('FIELD_TOO_LARGE')
        if fields.digest(json.loads(response.content)) != before:
            raise fields.Error('FIELD_REPRESENTATION_CHANGED', 409)
        if validate:
            validate()
        verify()
        if fields.digest(response.data) != before or fields.digest(json.loads(response.content)) != before:
            raise fields.Error('FIELD_REPRESENTATION_CHANGED', 409)
        return response

    def handle_exception(self, exc):
        if isinstance(exc, fields.Error):
            try:
                fail(exc.code, exc.status)
            except Exception as api_error:
                return super().handle_exception(api_error)
        return super().handle_exception(exc)


class CapabilityAPI(FieldAPI):
    def get(self, request, pk):
        if request.query_params:
            raise fields.Error('FIELD_INVALID_INPUT', 400)
        initial = self.context(request, pk)
        body = {'enabled': True, 'schema': fields.SCHEMA, 'viewer': viewer(initial)}
        return self.respond(request, body, lambda: self.recheck(request, pk, None, initial))


class SourcesAPI(FieldAPI):
    def get(self, request, pk, task_id):
        if request.query_params:
            raise fields.Error('FIELD_INVALID_INPUT', 400)
        initial = self.context(request, pk, task_id)

        def read(context):
            rows = metadata(self.query_sources(context, task_id))
            if len(rows) > fields.MAX_SOURCES:
                raise fields.Error('FIELD_TOO_MANY_SOURCES')
            if any(not HEX.fullmatch(row['result_hash']) for row in rows):
                raise fields.Error('FIELD_SOURCE_INTEGRITY_FAILED', 409)
            return rows

        rows = read(initial)
        body = {
            'schema': fields.SCHEMA,
            'viewer': viewer(initial),
            'sources': rows,
            'max_inputs': fields.MAX_INPUTS,
            'scope': {'project_id': pk, 'task_id': task_id},
        }

        def verify():
            current = self.recheck(request, pk, task_id, initial)
            if read(current) != rows:
                raise fields.Error('FIELD_SOURCES_CHANGED', 409)
            self.recheck(request, pk, task_id, initial)

        return self.respond(request, body, verify)


def load(query, *, validate_source=True, projection='ner'):
    from .snapshot_loading import load as guarded_load

    return guarded_load(
        query, policy=fields, validate=valid_source, validate_source=validate_source, projection=projection
    )


class ReportAPI(FieldAPI):
    def get(self, request, pk, task_id):
        query = request.query_params
        allowed = ({'submissions'}, {'frozen'}, {'frozen', 'left', 'right'})
        if set(query) not in allowed or any(len(query.getlist(k)) != 1 for k in query):
            raise fields.Error('FIELD_INVALID_INPUT', 400)
        initial = self.context(request, pk, task_id)
        receipt = decode(query['frozen'], pk, task_id) if 'frozen' in query else None
        expected_viewer = viewer(initial, receipt['viewer'] if receipt else None)
        ids = (
            [p['submission_id'] for p in receipt['inputs']]
            if receipt
            else [parse_id(v) for v in query['submissions'].split(',')]
        )
        if len(ids) > fields.MAX_INPUTS:
            raise fields.Error('FIELD_TOO_LARGE')
        if len(ids) < 2 or len(set(ids)) != len(ids):
            raise fields.Error('FIELD_INVALID_INPUT', 400)
        ids.sort()
        selected = (
            tuple(sorted((parse_id(query['left']), parse_id(query['right'])))) if 'left' in query else tuple(ids[:2])
        )
        if 'left' in query and (selected[0] == selected[1] or not set(selected).issubset(ids)):
            raise fields.Error('FIELD_INVALID_PAIR', 400)

        def materialize(context):
            scoped = self.query_sources(context, task_id).filter(pk__in=ids)
            authorized = set(scoped.values_list('pk', flat=True))
            if len(authorized) != len(ids):
                if receipt is None or Submission.objects.filter(pk__in=set(ids) - authorized).exists():
                    raise NotFound()
            rows = load(scoped)
            if {row.pk for row in rows} != authorized:
                raise fields.Error('FIELD_READ_CHANGED', 409)
            pins = [pin(row) for row in rows]
            if len({p['submitter_id'] for p in pins}) != len(pins):
                raise fields.Error('FIELD_DUPLICATE_PARTICIPANT', 400)
            if receipt:
                original = {p['submission_id']: p for p in receipt['inputs']}
                if any(p != original[p['submission_id']] for p in pins):
                    raise fields.Error('FIELD_INPUT_CHANGED', 409)
            if len(rows) != len(ids):
                return {'pins': receipt['inputs'], 'prepared': None, 'result_hash': None}
            prepared = fields.prepare(ids, [row.result_snapshot for row in rows])
            result_hash = fields.digest(
                {
                    'schema': fields.SCHEMA,
                    'rule': fields.RULE,
                    'explanation': fields.EXPLANATION,
                    'engine': fields.ENGINE,
                    'project_id': pk,
                    'task_id': task_id,
                    'inputs': pins,
                    'result': prepared['logical'],
                }
            )
            if receipt and (
                result_hash != receipt['result_hash']
                or prepared['logical']['configured_fields'] != receipt['configured_fields']
            ):
                raise fields.Error('FIELD_RESULT_CHANGED', 409)
            return {'pins': pins, 'prepared': prepared, 'result_hash': result_hash}

        data = materialize(initial)
        if receipt is None:
            receipt = {
                'schema': fields.SCHEMA,
                'rule': fields.RULE,
                'explanation': fields.EXPLANATION,
                'engine': fields.ENGINE,
                'project_id': pk,
                'task_id': task_id,
                'inputs': data['pins'],
                'viewer': expected_viewer,
                'calculated_at': timezone.now().isoformat(),
                'result_hash': data['result_hash'],
                'configured_fields': data['prepared']['logical']['configured_fields'],
            }
            fields.bounded(receipt, fields.MAX_TOKEN)
            token = signing.dumps(receipt, salt=fields.SCHEMA, compress=False)
            if len(token.encode()) > fields.MAX_TOKEN:
                raise fields.Error('FIELD_TOO_LARGE')
        else:
            token = query['frozen']
        result = (
            fields.public(data['prepared'], selected if len(selected) == 2 else None)
            if data['prepared']
            else {
                'availability': 'unavailable',
                'reason': 'source_unavailable',
                'input_count': len(ids),
                'total_pairs': len(ids) * (len(ids) - 1) // 2,
                'configured_fields': receipt['configured_fields'],
                'pairs': None,
                'selected_pair': None,
            }
        )
        body = {
            'schema': fields.SCHEMA,
            'rule': fields.RULE,
            'explanation': fields.EXPLANATION,
            'engine': fields.ENGINE,
            'scope': {'project_id': pk, 'task_id': task_id, 'input_ids': ids},
            'viewer': expected_viewer,
            'manifest': receipt,
            'manifest_hash': fields.digest(receipt),
            'frozen': token,
            'calculated_at': receipt['calculated_at'],
            'observed_at': timezone.now().isoformat(),
            'source_status': 'verified' if data['prepared'] else 'unavailable',
            'result_hash': data['result_hash'],
            'receipt_verified': data['prepared'] is not None,
            **result,
        }
        # Both logical and actual response/receipt budgets precede the final source pass.
        fields.bounded(body)

        def verify():
            current = self.recheck(request, pk, task_id, initial)
            viewer(current, expected_viewer)
            # The last source check follows rendering. Its
            # input is immutable; pins and the complete logical hash bind replay.
            fresh = materialize(current)
            if fresh['pins'] != data['pins'] or fresh['result_hash'] != data['result_hash']:
                raise fields.Error('FIELD_READ_CHANGED', 409)
            self.recheck(request, pk, task_id, initial)

        return self.respond(request, body, verify)
