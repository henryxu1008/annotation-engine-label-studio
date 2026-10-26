"""Joint authorized exact-set and partial-match projections, without writes."""

from django.conf import settings
from django.utils import timezone
from django.utils.crypto import salted_hmac
from rest_framework.exceptions import NotFound
from review_batches.service import digest

from . import partial, service
from .api import ComparisonAPI
from .batch_api import BatchAPI


class PartialAPI(BatchAPI):
    def get(self, request, pk, task_id, batch_id=None):
        query = request.query_params
        allowed = ({'frozen'}, set()) if batch_id is not None else ({'frozen'}, {'source_frozen'})
        if set(query) not in allowed or any(len(query.getlist(k)) != 1 for k in query):
            partial.reject('INVALID_INPUT')

        def context():
            if not settings.ANNOTATION_CLASSIFICATION_PARTIAL_MATCH_ENABLED:
                raise NotFound()
            # Do not require a live task object for a deleted frozen source.
            return BatchAPI.context(self, request, pk) if batch_id is not None else ComparisonAPI.context(self, request, pk)

        initial = context()
        receipt = partial.decode(query['frozen']) if 'frozen' in query else None
        if receipt is not None:
            if (set(receipt) != {'schema', 'rule', 'origin', 'inputs', 'explained_at', 'result_hash'}
                    or receipt['schema'] != partial.SCHEMA or receipt['rule'] != partial.RULE
                    or not isinstance(receipt['origin'], dict) or not isinstance(receipt['inputs'], list)
                    or not isinstance(receipt['explained_at'], str) or not isinstance(receipt['result_hash'], str)):
                partial.reject('INVALID_RECEIPT')
            origin = receipt['origin']
            if (origin.get('project_id') != pk or origin.get('task_id') != task_id
                    or origin.get('kind') != ('batch' if batch_id is not None else 'manual')
                    or (batch_id is not None and origin.get('batch_id') != batch_id)):
                raise NotFound()
        if batch_id is None:
            manifest = ({'project_id': pk, 'task_id': task_id, 'rule': service.RULE,
                         'calculated_at': receipt['origin'].get('calculated_at'), 'inputs': receipt['inputs']}
                        if receipt else partial.decode(query['source_frozen'], source=True))

        def materialize(current):
            if batch_id is not None:
                batch = BatchAPI.batch(self, current, batch_id)
                origin, base = partial.batch_base(batch, current, task_id)
            else:
                origin, base = partial.manual_base(manifest, current, task_id)
            result = partial.calculate(base)
            principal, actor, project, role = current
            viewer = salted_hmac(partial.SCHEMA + '/viewer', digest({
                'principal': principal.principal_id, 'source': principal.source, 'actor': actor.pk,
                'project': project.pk, 'organization': project.organization_id, 'role': role,
            }), algorithm='sha256').hexdigest()
            return {'origin': origin, 'base': base, 'partial': result, 'viewer': viewer}

        data = materialize(initial)
        if receipt:
            if data['origin'] != receipt['origin']:
                partial.reject('INPUT_CHANGED', 409)
            if data['partial']['status'] == 'comparable' and (
                    data['base']['manifest']['inputs'] != receipt['inputs']
                    or digest(data['partial']) != receipt['result_hash']):
                partial.reject('INPUT_CHANGED', 409)
            token, frozen = query['frozen'], receipt
        else:
            frozen, token = partial.seal(data['origin'], data['base'], data['partial'], timezone.now().isoformat())
        partial.bounded_size(data)
        body = {**data, 'schema': partial.SCHEMA, 'rule': partial.RULE, 'frozen': token,
                'receipt': frozen, 'observed_at': timezone.now().isoformat()}

        def verify():
            current = context()
            if current[0] != initial[0] or current[1].pk != initial[1].pk or current[3] != initial[3]:
                partial.reject('AUTHORITY_CHANGED', 409)
            if materialize(current) != data:
                partial.reject('READ_CHANGED', 409)
            final = context()
            if final[0] != initial[0] or final[1].pk != initial[1].pk or final[3] != initial[3]:
                partial.reject('AUTHORITY_CHANGED', 409)

        return BatchAPI.respond(self, request, body, verify)
