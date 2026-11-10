"""Manager-only frozen classification gold evidence; never an approval."""

from uuid import UUID
import re

from access_control.authorization import authorization
from access_control.identity import resolve_actor
from access_control.project_operations import ORGANIZE_CLASSIFICATION_GOLD, READ_WORK, lock_project_operation, require_project_operation
from core.permissions import all_permissions
from django.conf import settings
from django.db import connection, models, transaction
from django.db.models.expressions import RawSQL
from rest_framework import generics
from rest_framework.exceptions import NotFound
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from review_batches.service import digest, fail, identity, valid_source
from tasks.models import Submission, Task, TaskAssignment
from tasks.my_work import work_projects

from . import gold_scoring, partial
from .models import ClassificationGoldEvidence

SCHEMA = 'classification-gold-evidence/v1'
MAX_BYTES = 256 * 1024


def bounded_query(query, field):
    column = connection.ops.quote_name(query.model._meta.db_table) + '.' + connection.ops.quote_name(field)
    expression = (f'OCTET_LENGTH(CAST({column} AS TEXT))' if connection.vendor == 'postgresql'
                  else f'LENGTH(CAST({column} AS BLOB))')
    return query.defer(field).annotate(_gold_bytes=RawSQL(expression, [], output_field=models.BigIntegerField()))


def guard(request, pk):
    principal, actor = resolve_actor(request)
    from projects.models import Project
    project = get_object_or_404(Project.objects.filter(pk__in=work_projects(actor, ['manager']), deleted_at__isnull=True), pk=pk)
    enabled = settings.ANNOTATION_CLASSIFICATION_GOLD_PROJECTS
    if (connection.vendor not in ('postgresql', 'sqlite') or not settings.ANNOTATION_CLASSIFICATION_GOLD_ENABLED
            or not isinstance(enabled, dict) or enabled.get(f'{project.organization_id}:{project.pk}') is not True
            or not authorization.can_manage_project(principal, project)):
        raise NotFound()
    require_project_operation(project, READ_WORK)
    require_project_operation(project, ORGANIZE_CLASSIFICATION_GOLD)
    return principal, actor, project


def source(project, submission_id):
    if type(submission_id) is not int or submission_id <= 0:
        fail('GOLD_INVALID_INPUT', 400)
    # Discover IDs only; then lock in project -> task -> assignment -> source order.
    ids = Submission.objects.filter(pk=submission_id, assignment__project=project,
                                    assignment__task__project=project).values_list('assignment_id', 'assignment__task_id').first()
    if not ids:
        raise NotFound()
    assignment_id, task_id = ids
    task = get_object_or_404(Task.objects.select_for_update(), pk=task_id, project=project)
    assignment = get_object_or_404(TaskAssignment.objects.select_for_update(), pk=assignment_id, task=task, project=project)
    row = get_object_or_404(bounded_query(Submission.objects.select_for_update().select_related('assignment__task'),
                                          'result_snapshot'),
                            pk=submission_id, assignment=assignment)
    if type(row._gold_bytes) is not int or not 0 <= row._gold_bytes <= MAX_BYTES:
        fail('GOLD_SOURCE_UNAVAILABLE', 409)
    if (assignment.status not in TaskAssignment.ACTIVE_STATUSES or row.submitted_by_id != assignment.assignee_id
            or not valid_source(row, project.pk)):
        fail('GOLD_SOURCE_UNAVAILABLE', 409)
    partial.bounded_size(row.result_snapshot, MAX_BYTES)
    return row


def checked(project, evidence_id, kind):
    item = get_object_or_404(bounded_query(ClassificationGoldEvidence.objects.all(), 'manifest'),
                             pk=evidence_id, project=project, kind=kind)
    if type(item._gold_bytes) is not int or not 0 <= item._gold_bytes <= MAX_BYTES:
        fail('GOLD_EVIDENCE_INVALID', 409)
    partial.bounded_size(item.manifest, MAX_BYTES)
    if (not isinstance(item.manifest, dict) or item.manifest.get('schema') != SCHEMA
            or item.manifest.get('project_id') != project.pk or item.manifest.get('kind') != kind
            or item.manifest.get('actor_id') != item.actor_id_frozen or item.manifest.get('principal') != item.principal
            or item.manifest.get('principal_source') != item.principal_source
            or not isinstance(item.manifest.get('contents'), dict)
            or digest(item.manifest) != item.manifest_hash):
        fail('GOLD_EVIDENCE_INVALID', 409)
    return item


class GoldAPI(generics.GenericAPIView):
    permission_required = all_permissions.annotations_view

    def finalize_response(self, request, response, *args, **kwargs):
        result = super().finalize_response(request, response, *args, **kwargs)
        result['Cache-Control'] = 'private, no-store'
        result['Referrer-Policy'] = 'no-referrer'
        return result

    def get(self, request, pk, evidence_id=None):
        principal, actor, project = guard(request, pk)
        if request.query_params:
            fail('GOLD_INVALID_INPUT', 400)
        if evidence_id is None:
            # No embedded answers in the collection, and the query is bounded.
            rows = list(ClassificationGoldEvidence.objects.filter(project=project).order_by('-pk')
                        .values('id', 'kind', 'created_at', 'manifest_hash')[:101])
            data = {'items': rows[:100], 'has_more': len(rows) > 100}
        else:
            item = get_object_or_404(ClassificationGoldEvidence.objects.only('id', 'kind'), pk=evidence_id, project=project)
            item = checked(project, item.pk, item.kind)
            data = {'id': item.pk, 'manifest': item.manifest, 'manifest_hash': item.manifest_hash}
        current = guard(request, pk)
        if current[0] != principal or current[1].pk != actor.pk:
            fail('GOLD_AUTHORITY_CHANGED', 409)
        return Response(data)

    def post(self, request, pk, evidence_id=None):
        principal, actor, project = guard(request, pk)
        if evidence_id is not None:
            raise NotFound()
        data = request.data
        if (request.query_params or not isinstance(data, dict) or data.get('kind') not in ('reference', 'score')
                or set(data) != ({'kind', 'submission_id', 'expected_result_hash'} if data.get('kind') == 'reference'
                                 else {'kind', 'submission_id', 'expected_result_hash', 'reference_id', 'expected_reference_hash'})
                or not isinstance(data.get('expected_result_hash'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', data['expected_result_hash'])
                or (data.get('kind') == 'score' and (not isinstance(data.get('expected_reference_hash'), str)
                    or not re.fullmatch(r'[a-f0-9]{64}', data['expected_reference_hash'])))):
            fail('GOLD_INVALID_INPUT', 400)
        try:
            key = UUID(request.headers.get('Idempotency-Key', ''))
        except (ValueError, TypeError, AttributeError):
            fail('GOLD_INVALID_KEY', 400)
        request_hash = digest(data)
        with transaction.atomic():
            project, locked_actor = lock_project_operation(project.pk, principal, ORGANIZE_CLASSIFICATION_GOLD)
            current = guard(request, pk)
            if current[0] != principal or locked_actor.pk != actor.pk:
                fail('GOLD_AUTHORITY_CHANGED', 409)
            previous = ClassificationGoldEvidence.objects.filter(project=project, idempotency_key=key).only('id', 'kind').first()
            if previous:
                previous = checked(project, previous.pk, previous.kind)
                if (previous.request_hash != request_hash or previous.actor_id_frozen != actor.pk
                        or previous.principal != principal.principal_id or previous.principal_source != principal.source):
                    fail('GOLD_KEY_CONFLICT', 409)
                return Response({'id': previous.pk, 'manifest': previous.manifest,
                                 'manifest_hash': previous.manifest_hash, 'replayed': True})
            row = source(project, data['submission_id'])
            if row.result_hash != data['expected_result_hash']:
                fail('GOLD_SOURCE_CHANGED', 409)
            snapshot = row.result_snapshot
            config = snapshot['project'].get('label_config')
            task_data = snapshot['task'].get('data')
            annotation = snapshot.get('annotation')
            if not isinstance(annotation, dict) or annotation.get('was_cancelled') is not False:
                fail('GOLD_SOURCE_UNAVAILABLE', 409)
            result = annotation.get('result')
            if data['kind'] == 'reference':
                check = gold_scoring.score(config, task_data, result, result)
                if check['status'] != 'scored':
                    fail('GOLD_SOURCE_UNAVAILABLE', 409)
                contents = {'source': identity(row), 'config': config, 'task_data': task_data, 'answer': result}
            else:
                if type(data['reference_id']) is not int or data['reference_id'] <= 0:
                    fail('GOLD_INVALID_INPUT', 400)
                reference = checked(project, data['reference_id'], 'reference')
                if reference.manifest_hash != data['expected_reference_hash']:
                    fail('GOLD_REFERENCE_CHANGED', 409)
                ref = reference.manifest['contents']
                if (set(ref) != {'source', 'config', 'task_data', 'answer'} or not isinstance(ref['source'], dict)
                        or type(ref['source'].get('task_id')) is not int
                        or type(ref['source'].get('submitter_id')) is not int):
                    fail('GOLD_EVIDENCE_INVALID', 409)
                if (ref['source']['task_id'] != row.assignment.task_id or ref['source']['submitter_id'] == row.submitted_by_id
                        or ref['config'] != config or digest(ref['task_data']) != digest(task_data)):
                    fail('GOLD_SOURCE_MISMATCH', 409)
                result = gold_scoring.score(config, task_data, ref['answer'], result)
                if result['status'] != 'scored':
                    fail('GOLD_SOURCE_UNAVAILABLE', 409)
                contents = {'reference_id': reference.pk, 'reference_hash': reference.manifest_hash,
                            'source': identity(row), 'result': result}
            manifest = {'schema': SCHEMA, 'project_id': project.pk, 'kind': data['kind'],
                        'actor_id': actor.pk, 'principal': principal.principal_id,
                        'principal_source': principal.source, 'contents': contents}
            partial.bounded_size(manifest, MAX_BYTES)
            record = ClassificationGoldEvidence.objects.create(project=project, created_by=actor,
                actor_id_frozen=actor.pk, principal=principal.principal_id, principal_source=principal.source,
                kind=data['kind'], idempotency_key=key, request_hash=request_hash,
                manifest=manifest, manifest_hash=digest(manifest))
            guard(request, pk)
        return Response({'id': record.pk, 'manifest': manifest, 'manifest_hash': record.manifest_hash,
                         'replayed': False}, status=201)


class GoldSourcesAPI(GoldAPI):
    http_method_names = ['get', 'head', 'options']

    def get(self, request, pk, task_id):
        principal, actor, project = guard(request, pk)
        if request.query_params:
            fail('GOLD_INVALID_INPUT', 400)
        get_object_or_404(Task, project=project, pk=task_id)
        rows = list(Submission.objects.filter(assignment__project=project, assignment__task_id=task_id,
                    assignment__status__in=TaskAssignment.ACTIVE_STATUSES)
                    .order_by('-pk').values('id', 'assignment_id', 'revision', 'submitted_by_id', 'result_hash')[:101])
        if len(rows) > 100:
            fail('GOLD_TOO_MANY_SOURCES', 422)
        current = guard(request, pk)
        if current[0] != principal or current[1].pk != actor.pk:
            fail('GOLD_AUTHORITY_CHANGED', 409)
        return Response({'task_id': task_id, 'sources': rows})
