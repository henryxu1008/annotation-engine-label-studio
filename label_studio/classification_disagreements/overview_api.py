"""An independently gated read and replay; no writes or latest-source selection."""

from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import NotFound
from review_batches.service import digest

from . import overview
from .batch_api import BatchAPI


def enabled():
    return (settings.ANNOTATION_CLASSIFICATION_QUALITY_OVERVIEW_ENABLED
            and settings.ANNOTATION_CLASSIFICATION_PARTIAL_MATCH_ENABLED)


class OverviewAPI(BatchAPI):
    def context(self, request, pk, task_id=None, *, create=False):
        if not enabled():
            raise NotFound()
        return super().context(request, pk, task_id, create=create)

    def get(self, request, pk, batch_id):
        self.query(request, ('frozen', 'page', 'page_size'))
        page, size = int(request.query_params.get('page', 1)), int(request.query_params.get('page_size', 50))
        if page > 1 and 'frozen' not in request.query_params:
            overview.reject('INVALID_INPUT', 400)
        initial = self.context(request, pk)
        receipt = overview.decode(request.query_params['frozen']) if 'frozen' in request.query_params else None

        def materialize(current):
            batch = self.batch(current, batch_id)
            if receipt:
                origin = overview.origin(batch, current)
                if receipt['origin'] != origin:
                    scope = receipt['origin'].get('scope', {})
                    if not isinstance(scope, dict) or scope.get('project_id') != pk or scope.get('batch_id') != batch_id:
                        raise NotFound()
                    overview.reject('CONTEXT_CHANGED')
            data = overview.project(batch, current)
            if receipt and data['source_status'] == 'verified' and digest(data) != receipt['result_hash']:
                overview.reject('RESULT_CHANGED')
            return data

        data = materialize(initial)
        verified = data['source_status'] == 'verified'
        if receipt:
            frozen, token = receipt, request.query_params['frozen']
        elif verified:
            frozen, token = overview.seal(data, timezone.now().isoformat())
        else:
            frozen, token = None, None
        people = data['annotators']
        pagination = None
        if people is not None:
            count = len(people)
            if page > 1 and (page - 1) * size >= count:
                raise NotFound()
            pagination = {'count': count, 'page': page, 'page_size': size,
                          'next_page': page + 1 if page * size < count else None,
                          'previous_page': page - 1 if page > 1 else None}
        body = {**data, 'annotators': people[(page - 1) * size:page * size] if people is not None else None,
                'pagination': pagination, 'frozen': token, 'receipt_verified': verified,
                'result_hash': frozen['result_hash'] if verified else None,
                'summarized_at': frozen['summarized_at'] if frozen else None,
                'observed_at': timezone.now().isoformat()}

        def verify():
            current = self.recheck(request, pk, initial)
            if materialize(current) != data:
                overview.reject('READ_CHANGED')
            self.recheck(request, pk, initial)

        return self.respond(request, body, verify)
