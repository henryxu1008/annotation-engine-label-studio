"""Independent NER overview GET/replay; no selection, creation or business write."""
from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import NotFound

from . import ner
from . import ner_batches as batches
from . import ner_overview as overview
from .ner_batch_api import BatchAPI


class OverviewAPI(BatchAPI):
    def context(self, request, pk, *, create=False):
        if not settings.ANNOTATION_NER_QUALITY_OVERVIEW_ENABLED:
            raise NotFound()
        return super().context(request, pk, create=create)

    def get(self, request, pk, batch_id):
        self.query(request, ('frozen', 'page', 'page_size'))
        page, size = int(request.query_params.get('page', 1)), int(request.query_params.get('page_size', 50))
        if page > 1 and 'frozen' not in request.query_params:
            overview.reject('INVALID_INPUT', 400)
        initial = self.context(request, pk)
        receipt = overview.decode(request.query_params['frozen']) if 'frozen' in request.query_params else None

        def materialize(current):
            batch = self.batch(current, batch_id)
            viewer = receipt['origin'].get('viewer') if receipt else None
            if receipt:
                scope = receipt['origin'].get('scope')
                if not isinstance(scope, dict) or scope.get('project_id') != pk or scope.get('batch_id') != batch_id:
                    raise NotFound()
                if receipt['origin'] != overview.origin(batch, current, expected_viewer=viewer):
                    overview.reject('CONTEXT_CHANGED')
            data, prepared = overview.project(batch, current, expected_viewer=viewer)
            if receipt and data['source_status'] == 'verified' and ner.digest(data) != receipt['result_hash']:
                overview.reject('RESULT_CHANGED')
            return data, prepared

        data, prepared = materialize(initial)
        verified = data['source_status'] == 'verified'
        if receipt:
            frozen, token = receipt, request.query_params['frozen']
        elif verified:
            frozen, token = overview.seal(data, timezone.now().isoformat())
        else:
            frozen, token = None, None
        people, pagination = data['annotators'], None
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
            fresh, values = materialize(current)
            if fresh != data:
                overview.reject('READ_CHANGED')
            batches.check_engine(values)
            self.recheck(request, pk, initial)

        return self.respond(request, body, verify, lambda: batches.check_engine(prepared))
