"""GET-only NER authority, bounded source loading and signed frozen replay."""

import hashlib
import json
import re

from access_control.authorization import authorization
from access_control.identity import resolve_actor
from access_control.project_operations import READ_WORK, require_project_operation
from django.conf import settings
from django.core import signing
from django.db import connection
from django.db.models import BigIntegerField, Case, F, JSONField, Sum, Value, When, Window
from django.db.models.expressions import RawSQL
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

from . import ner
from .api import ComparisonAPI

PIN_IDS = ('submission_id', 'assignment_id', 'assignee_id', 'task_id', 'revision', 'submitter_id')
PIN_HASHES = ('result_hash', 'config_hash', 'task_data_hash')
HEX = re.compile(r'^[0-9a-f]{64}$')


def positive(value):
    return type(value) is int and 0 < value <= 2**53 - 1


def parse_id(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 16:
        raise ner.Error('NER_INVALID_INPUT', 400)
    result = int(value)
    if not positive(result):
        raise ner.Error('NER_INVALID_INPUT', 400)
    return result


def metadata(query):
    return list(query.order_by('pk').values('id', 'assignment_id', 'assignment__assignee_id',
                'assignment__task_id', 'submitted_by_id', 'revision', 'result_hash', 'status')[:ner.MAX_SOURCES + 1])


def pin(row):
    snapshot = row.result_snapshot
    xml = snapshot['project'].get('label_config')
    return {'submission_id': row.pk, 'assignment_id': row.assignment_id,
            'assignee_id': row.assignment.assignee_id, 'task_id': row.assignment.task_id,
            'revision': row.revision, 'submitter_id': row.submitted_by_id, 'result_hash': row.result_hash,
            'config_hash': hashlib.sha256(xml.encode()).hexdigest() if isinstance(xml, str) else ner.digest(xml),
            'task_data_hash': ner.digest(snapshot['task'].get('data'))}


def viewer(context, expected=None):
    principal, actor, project, role = context
    identity = ner.digest({'principal': principal.principal_id, 'source': principal.source, 'actor': actor.pk,
                           'organization': project.organization_id, 'project': project.pk, 'role': role})
    candidates = [salted_hmac(ner.SCHEMA + '/viewer', identity, secret=key, algorithm='sha256').hexdigest()
                  for key in (settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS)]
    if expected is not None:
        if not isinstance(expected, str) or not any(constant_time_compare(expected, v) for v in candidates):
            raise ner.Error('NER_AUTHORITY_CHANGED', 409)
        return expected
    return candidates[0]


def decode(token, project_id, task_id):
    if not isinstance(token, str) or not token or token.startswith('.') or len(token.encode()) > ner.MAX_TOKEN:
        raise ner.Error('NER_INVALID_RECEIPT', 400)
    try:
        value = signing.loads(token, salt=ner.SCHEMA)
    except (signing.BadSignature, ValueError, TypeError, UnicodeError):
        raise ner.Error('NER_INVALID_RECEIPT', 400)
    keys = {'schema', 'rule', 'explanation', 'engine', 'project_id', 'task_id', 'inputs',
            'viewer', 'calculated_at', 'result_hash'}
    if not isinstance(value, dict) or set(value) != keys:
        raise ner.Error('NER_INVALID_RECEIPT', 400)
    ner.bounded(value, ner.MAX_TOKEN)
    if value['project_id'] != project_id or value['task_id'] != task_id:
        raise NotFound()
    if (value['schema'] != ner.SCHEMA or value['rule'] != ner.RULE or value['explanation'] != ner.EXPLANATION
            or value['engine'] != ner.ENGINE or not isinstance(value['calculated_at'], str)
            or not isinstance(value['result_hash'], str) or not HEX.fullmatch(value['result_hash'])
            or not isinstance(value['viewer'], str) or not HEX.fullmatch(value['viewer'])
            or not isinstance(value['inputs'], list) or not 1 <= len(value['inputs']) <= ner.MAX_INPUTS):
        raise ner.Error('NER_INVALID_RECEIPT', 400)
    try:
        when = timezone.datetime.fromisoformat(value['calculated_at'])
        if timezone.is_naive(when):
            raise ValueError()
    except (ValueError, TypeError):
        raise ner.Error('NER_INVALID_RECEIPT', 400)
    for item in value['inputs']:
        if (not isinstance(item, dict) or set(item) != set(PIN_IDS + PIN_HASHES)
                or any(not positive(item[k]) for k in PIN_IDS)
                or any(not isinstance(item[k], str) or not HEX.fullmatch(item[k]) for k in PIN_HASHES)
                or item['task_id'] != task_id or item['assignee_id'] != item['submitter_id']):
            raise ner.Error('NER_INVALID_RECEIPT', 400)
    ids = [p['submission_id'] for p in value['inputs']]
    if ids != sorted(set(ids)) or len({p['submitter_id'] for p in value['inputs']}) != len(ids):
        raise ner.Error('NER_INVALID_RECEIPT', 400)
    return value


class NerAPI(ComparisonAPI):
    renderer_classes = [JSONRenderer]

    def context(self, request, pk, task_id=None):
        principal, actor = resolve_actor(request)
        project = get_object_or_404(Project.objects.filter(
            pk__in=work_projects(actor, ['manager', 'reviewer']), deleted_at__isnull=True), pk=pk)
        modes = settings.ANNOTATION_NER_DISAGREEMENT_MODES
        if (not settings.ANNOTATION_NER_DISAGREEMENTS_ENABLED or not isinstance(modes, dict)
                or modes.get(f'{project.organization_id}:{project.pk}') != 'fork_review'):
            raise NotFound()
        role = authorization.project_role(principal, project)
        if role not in ('manager', 'reviewer'):
            raise NotFound()
        require_project_operation(project, READ_WORK)
        if connection.vendor not in ('postgresql', 'sqlite'):
            fail('NER_BACKEND_UNSUPPORTED', 503)
        if task_id is not None and not Task.objects.filter(pk=task_id, project=project).exists():
            raise NotFound()
        return principal, actor, project, role

    def recheck(self, request, pk, task_id, initial):
        current = self.context(request, pk, task_id)
        if current[0] != initial[0] or current[1].pk != initial[1].pk or current[3] != initial[3]:
            raise ner.Error('NER_AUTHORITY_CHANGED', 409)
        return current

    def query_sources(self, context, task_id):
        _, actor, project, role = context
        query = Submission.objects.filter(assignment__project=project, assignment__task_id=task_id,
                                          assignment__task__project=project,
                                          submitted_by_id=F('assignment__assignee_id'))
        return query.exclude(submitted_by_id=actor.pk) if role == 'reviewer' else query

    def respond(self, request, body, verify, validate=None):
        ner.bounded(body)
        before = ner.digest(body)
        response = self.finalize_response(request, Response(body))
        response.render()
        if len(response.content) > ner.MAX_OUTPUT:
            raise ner.Error('NER_TOO_LARGE')
        if ner.digest(json.loads(response.content)) != before:
            raise ner.Error('NER_REPRESENTATION_CHANGED', 409)
        if validate:
            validate()
        verify()
        if ner.digest(response.data) != before or ner.digest(json.loads(response.content)) != before:
            raise ner.Error('NER_REPRESENTATION_CHANGED', 409)
        return response

    def handle_exception(self, exc):
        if isinstance(exc, ner.Error):
            try:
                fail(exc.code, exc.status)
            except Exception as api_error:
                return super().handle_exception(api_error)
        return super().handle_exception(exc)


class CapabilityAPI(NerAPI):
    def get(self, request, pk):
        if request.query_params:
            raise ner.Error('NER_INVALID_INPUT', 400)
        initial = self.context(request, pk)
        body = {'enabled': True, 'schema': ner.SCHEMA, 'viewer': viewer(initial)}
        return self.respond(request, body, lambda: self.recheck(request, pk, None, initial))


class SourcesAPI(NerAPI):
    def get(self, request, pk, task_id):
        if request.query_params:
            raise ner.Error('NER_INVALID_INPUT', 400)
        initial = self.context(request, pk, task_id)

        def read(context):
            rows = metadata(self.query_sources(context, task_id))
            if len(rows) > ner.MAX_SOURCES:
                raise ner.Error('NER_TOO_MANY_SOURCES')
            if any(not HEX.fullmatch(row['result_hash']) for row in rows):
                raise ner.Error('NER_SOURCE_INTEGRITY_FAILED', 409)
            return rows

        rows = read(initial)
        body = {'schema': ner.SCHEMA, 'viewer': viewer(initial), 'sources': rows, 'max_inputs': ner.MAX_INPUTS,
                'scope': {'project_id': pk, 'task_id': task_id}}

        def verify():
            current = self.recheck(request, pk, task_id, initial)
            if read(current) != rows:
                raise ner.Error('NER_SOURCES_CHANGED', 409)
            self.recheck(request, pk, task_id, initial)
        return self.respond(request, body, verify)


def load(query):
    # Scope first; count bytes in SQL before hydrating a JSONField or live task body.
    column = connection.ops.quote_name(Submission._meta.db_table) + '.' + connection.ops.quote_name('result_snapshot')
    expression = f'OCTET_LENGTH(CAST({column} AS TEXT))' if connection.vendor == 'postgresql' else f'LENGTH(CAST({column} AS BLOB))'
    size = RawSQL(expression, [], output_field=BigIntegerField())
    sizes = list(query.annotate(ner_size=size).values_list('ner_size', flat=True))
    if any(type(size) is not int or size < 0 for size in sizes) or sum(sizes) > ner.MAX_STORED_BYTES:
        raise ner.Error('NER_TOO_LARGE')
    rows, used = [], 0
    # The preflight and this SELECT may observe different committed rows. Gate
    # the entire selected payload inside the hydrating statement, so a growing
    # snapshot is never transferred/decoded before its budget is enforced.
    guarded = query.annotate(ner_total=Window(Sum(size))).annotate(ner_snapshot=Case(
        When(ner_total__lte=ner.MAX_STORED_BYTES, then=F('result_snapshot')),
        default=Value(None), output_field=JSONField()))
    for row in guarded.select_related('assignment__task', 'review').only(
            'id', 'assignment_id', 'revision', 'result_hash', 'submitted_by_id', 'status',
            'assignment__id', 'assignment__task_id', 'assignment__project_id', 'assignment__assignee_id',
            'assignment__task__id', 'assignment__task__project_id', 'review__id', 'review__decision').order_by('pk').iterator(chunk_size=1):
        if type(row.ner_total) is not int or row.ner_total < 0 or row.ner_total > ner.MAX_STORED_BYTES:
            raise ner.Error('NER_TOO_LARGE')
        row.result_snapshot = row.ner_snapshot
        try:
            used += ner.bounded(row.result_snapshot, ner.MAX_SNAPSHOT_BYTES)
            if used > ner.MAX_SNAPSHOT_BYTES:
                raise ner.Error('NER_TOO_LARGE')
            if not valid_source(row, row.assignment.project_id):
                raise ner.Error('NER_SOURCE_INTEGRITY_FAILED', 409)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ner.Error('NER_SOURCE_INTEGRITY_FAILED', 409)
        rows.append(row)
    return rows


class ReportAPI(NerAPI):
    def get(self, request, pk, task_id):
        query = request.query_params
        allowed = ({'submissions'}, {'frozen'}, {'frozen', 'left', 'right'})
        if set(query) not in allowed or any(len(query.getlist(k)) != 1 for k in query):
            raise ner.Error('NER_INVALID_INPUT', 400)
        initial = self.context(request, pk, task_id)
        receipt = decode(query['frozen'], pk, task_id) if 'frozen' in query else None
        expected_viewer = viewer(initial, receipt['viewer'] if receipt else None)
        ids = ([p['submission_id'] for p in receipt['inputs']] if receipt else
               [parse_id(v) for v in query['submissions'].split(',')])
        if len(ids) > ner.MAX_INPUTS:
            raise ner.Error('NER_TOO_LARGE')
        if not ids or len(set(ids)) != len(ids):
            raise ner.Error('NER_INVALID_INPUT', 400)
        ids.sort()
        selected = tuple(sorted((parse_id(query['left']), parse_id(query['right'])))) if 'left' in query else tuple(ids[:2])
        if 'left' in query and (selected[0] == selected[1] or not set(selected).issubset(ids)):
            raise ner.Error('NER_INVALID_PAIR', 400)

        def materialize(context):
            scoped = self.query_sources(context, task_id).filter(pk__in=ids)
            authorized = set(scoped.values_list('pk', flat=True))
            if len(authorized) != len(ids):
                if receipt is None or Submission.objects.filter(pk__in=set(ids) - authorized).exists():
                    raise NotFound()
            rows = load(scoped)
            if {row.pk for row in rows} != authorized:
                raise ner.Error('NER_READ_CHANGED', 409)
            pins = [pin(row) for row in rows]
            if len({p['submitter_id'] for p in pins}) != len(pins):
                raise ner.Error('NER_DUPLICATE_PARTICIPANT', 400)
            if receipt:
                original = {p['submission_id']: p for p in receipt['inputs']}
                if any(p != original[p['submission_id']] for p in pins):
                    raise ner.Error('NER_INPUT_CHANGED', 409)
            if len(rows) != len(ids):
                return {'pins': receipt['inputs'], 'prepared': None, 'result_hash': None}
            prepared = ner.prepare(ids, [row.result_snapshot for row in rows])
            result_hash = ner.digest({'schema': ner.SCHEMA, 'rule': ner.RULE, 'explanation': ner.EXPLANATION,
                                      'engine': ner.ENGINE, 'project_id': pk, 'task_id': task_id,
                                      'inputs': pins, 'result': prepared['logical']})
            if receipt and result_hash != receipt['result_hash']:
                raise ner.Error('NER_RESULT_CHANGED', 409)
            return {'pins': pins, 'prepared': prepared, 'result_hash': result_hash}

        data = materialize(initial)
        if receipt is None:
            receipt = {'schema': ner.SCHEMA, 'rule': ner.RULE, 'explanation': ner.EXPLANATION, 'engine': ner.ENGINE,
                       'project_id': pk, 'task_id': task_id, 'inputs': data['pins'], 'viewer': expected_viewer,
                       'calculated_at': timezone.now().isoformat(), 'result_hash': data['result_hash']}
            ner.bounded(receipt, ner.MAX_TOKEN)
            token = signing.dumps(receipt, salt=ner.SCHEMA, compress=False)
            if len(token.encode()) > ner.MAX_TOKEN:
                raise ner.Error('NER_TOO_LARGE')
        else:
            token = query['frozen']
        result = (ner.public(data['prepared'], selected if len(selected) == 2 else None) if data['prepared'] else
                  {'availability': 'unavailable', 'reason': 'source_unavailable', 'input_count': len(ids),
                   'total_pairs': len(ids) * (len(ids) - 1) // 2, 'pairs': None, 'selected_pair': None})
        body = {'schema': ner.SCHEMA, 'rule': ner.RULE, 'explanation': ner.EXPLANATION, 'engine': ner.ENGINE,
                'scope': {'project_id': pk, 'task_id': task_id, 'input_ids': ids}, 'viewer': expected_viewer,
                'manifest': receipt, 'manifest_hash': ner.digest(receipt), 'frozen': token,
                'calculated_at': receipt['calculated_at'], 'observed_at': timezone.now().isoformat(),
                'source_status': 'verified' if data['prepared'] else 'unavailable',
                'result_hash': data['result_hash'], 'receipt_verified': data['prepared'] is not None, **result}
        # Both logical and actual response/receipt budgets precede the scorer.
        ner.bounded(body)
        def verify():
            current = self.recheck(request, pk, task_id, initial)
            viewer(current, expected_viewer)
            fresh = materialize(current)
            if fresh['pins'] != data['pins'] or fresh['result_hash'] != data['result_hash']:
                raise ner.Error('NER_READ_CHANGED', 409)
            if fresh['prepared']:
                ner.check_engine(fresh['prepared'])
            self.recheck(request, pk, task_id, initial)
        return self.respond(request, body, verify,
                            lambda: ner.check_engine(data['prepared']) if data['prepared'] else None)
