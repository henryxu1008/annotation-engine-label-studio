from access_control.identity import resolve_actor
from core.permissions import all_permissions
from django.core import signing
from django.conf import settings
from django.utils import timezone
from rest_framework import generics
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from review_batches.service import digest, fail

from . import service


class ComparisonAPI(generics.GenericAPIView):
    permission_required = all_permissions.annotations_view

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'private, no-store'
        response['Referrer-Policy'] = 'no-referrer'
        return response

    def context(self, request, pk, task_id=None):
        principal, actor = resolve_actor(request)
        project, role = service.guard(pk, principal, actor)
        if task_id is not None:
            service.task_scope(project, task_id)
        return principal, actor, project, role

    def recheck_context(self, request, pk, task_id, principal, actor, role):
        current_principal, current_actor, project, current_role = self.context(request, pk, task_id)
        if current_principal != principal or current_actor.pk != actor.pk or current_role != role:
            fail('CLASSIFICATION_AUTHORITY_CHANGED')
        return project


class CapabilityAPI(ComparisonAPI):
    def get(self, request, pk):
        if request.query_params:
            raise ValidationError('Unexpected query parameters.')
        self.context(request, pk)
        return Response({'enabled': True, 'can_explain_partial': settings.ANNOTATION_CLASSIFICATION_PARTIAL_MATCH_ENABLED})


class SourcesAPI(ComparisonAPI):
    def get(self, request, pk, task_id):
        if request.query_params:
            raise ValidationError('Unexpected query parameters.')
        principal, actor, project, role = self.context(request, pk, task_id)
        rows = list(
            service.sources(project, task_id, actor, role).order_by('submitted_by_id', '-revision', '-pk')[:101]
        )
        if len(rows) > 100:
            fail('CLASSIFICATION_TOO_MANY_SOURCES', 422)
        data = [{**service.pin(row), 'status': row.status} for row in rows]
        current_project = self.recheck_context(request, pk, task_id, principal, actor, role)
        current = list(
            service.sources(current_project, task_id, actor, role).order_by('submitted_by_id', '-revision', '-pk')[
                :101
            ]
        )
        if data != [{**service.pin(row), 'status': row.status} for row in current]:
            fail('CLASSIFICATION_SOURCES_CHANGED')
        self.recheck_context(request, pk, task_id, principal, actor, role)
        return Response({'sources': data, 'max_inputs': service.MAX_INPUTS,
                         'can_explain_partial': settings.ANNOTATION_CLASSIFICATION_PARTIAL_MATCH_ENABLED})


class ReportAPI(ComparisonAPI):
    def get(self, request, pk, task_id):
        principal, actor, project, role = self.context(request, pk, task_id)
        query = request.query_params
        if set(query) not in ({'submissions'}, {'frozen'}) or any(len(query.getlist(key)) != 1 for key in query):
            raise ValidationError('Choose explicit revisions or a frozen input receipt.')
        token = query.get('frozen')
        if token:
            if len(token) > 16384:
                raise ValidationError('Invalid frozen input receipt.')
            try:
                frozen = signing.loads(token, salt=service.SALT)
            except signing.BadSignature:
                raise ValidationError('Invalid frozen input receipt.')
            if (
                not isinstance(frozen, dict)
                or not isinstance(frozen.get('inputs'), list)
                or not 1 <= len(frozen['inputs']) <= service.MAX_INPUTS
                or not isinstance(frozen.get('calculated_at'), str)
                or any(
                    not isinstance(item, dict)
                    or type(item.get('submission_id')) is not int
                    or item['submission_id'] <= 0
                    for item in frozen['inputs']
                )
            ):
                raise ValidationError('Invalid frozen input receipt.')
            if (
                frozen.get('project_id') != pk
                or frozen.get('task_id') != task_id
                or frozen.get('rule') != service.RULE
            ):
                raise NotFound()
            ids = [item['submission_id'] for item in frozen['inputs']]
        else:
            try:
                ids = [int(value) for value in query.get('submissions', '').split(',')]
            except ValueError:
                raise ValidationError('Invalid revision selection.')
            if (
                not 1 <= len(ids) <= service.MAX_INPUTS
                or any(value <= 0 for value in ids)
                or len(set(ids)) != len(ids)
            ):
                raise ValidationError('Choose up to 20 distinct revisions.')
            ids.sort()
            frozen = None
        rows = list(service.sources(project, task_id, actor, role).filter(pk__in=ids).order_by('pk'))
        if len(rows) != len(ids):
            # A signed receipt is not authority. Distinguish a deleted input from
            # a still-present foreign/self source without revealing that source.
            from tasks.models import Submission

            if not token or Submission.objects.filter(pk__in=set(ids) - {row.pk for row in rows}).exists():
                raise NotFound()
            result = service.unavailable('source_unavailable')
        else:
            result = service.calculate(rows, project.pk)
        inputs = [service.pin(row) for row in rows]
        if frozen is None:
            frozen = {
                'project_id': pk,
                'task_id': task_id,
                'rule': service.RULE,
                'calculated_at': timezone.now().isoformat(),
                'inputs': inputs,
            }
            token = signing.dumps(frozen, salt=service.SALT, compress=True)
        elif len(rows) == len(ids) and inputs != frozen['inputs']:
            fail('CLASSIFICATION_INPUT_CHANGED')
        # Refetch the exact input set and live authority after constructing the
        # projection. Never substitute newest revisions or another submitter.
        current_project = self.recheck_context(request, pk, task_id, principal, actor, role)
        current = list(service.sources(current_project, task_id, actor, role).filter(pk__in=ids).order_by('pk'))
        if [service.pin(row) for row in current] != inputs:
            fail('CLASSIFICATION_INPUT_CHANGED')
        if len(rows) == len(ids) and service.calculate(current, project.pk) != result:
            fail('CLASSIFICATION_INPUT_CHANGED')
        self.recheck_context(request, pk, task_id, principal, actor, role)
        return Response(
            {
                'schema': service.SALT,
                'frozen': token,
                'manifest': frozen,
                'manifest_hash': digest(frozen),
                'observed_at': timezone.now().isoformat(),
                **result,
            }
        )
