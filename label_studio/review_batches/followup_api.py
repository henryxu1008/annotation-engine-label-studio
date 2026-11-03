"""Private Manager-only routes, using the existing bounded JSON parser."""

from uuid import UUID

from django.urls import path

from . import followup_contract as c
from . import followup_service as service
from .second_review_api import BaseAPI


class ListAPI(BaseAPI):
    def get(self, request, pk):
        return service.history(request, c.current(request, pk))

    def post(self, request, pk):
        ctx = c.current(request, pk, 'record')
        if request.query_params:
            c.fail('INVALID_INPUT', 400)
        key = request.headers.get('Idempotency-Key')
        if not c.uuid_string(key):
            c.fail('INVALID_INPUT', 400)
        return service.command(request, ctx, request.data, UUID(key))


class ContextAPI(BaseAPI):
    def get(self, request, pk, task_id):
        ctx = c.current(request, pk, 'record')
        if set(request.query_params) - {'related_submission_id'} or any(
            len(v) != 1 for _, v in request.query_params.lists()
        ):
            c.fail('INVALID_INPUT', 400)
        raw = request.query_params.get('related_submission_id')
        target = None
        if raw is not None:
            if not raw.isascii() or not raw.isdecimal() or len(raw) > 16 or not c.positive(int(raw)):
                c.fail('INVALID_INPUT', 400)
            target = int(raw)
        return service.context(request, ctx, task_id, target)


class DetailAPI(BaseAPI):
    def get(self, request, pk, followup_id):
        if request.query_params:
            c.fail('INVALID_INPUT', 400)
        return service.detail(request, c.current(request, pk), followup_id)


urlpatterns = [
    path('', ListAPI.as_view()),
    path('context/<uuid:task_id>/', ContextAPI.as_view()),
    path('<uuid:followup_id>/', DetailAPI.as_view()),
]
