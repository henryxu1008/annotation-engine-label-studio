"""Private typed supervision routes; no bulk or answer-export surface."""
import io
import json
from uuid import UUID

from core.permissions import all_permissions
from django.http import Http404
from django.urls import path
from rest_framework import generics
from rest_framework.exceptions import APIException
from rest_framework.parsers import BaseParser

from . import second_review_contract as c
from . import second_review_service as service


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            c.fail('INVALID_INPUT', 400)
        value[key] = item
    return value


class SecondReviewJSONParser(BaseParser):
    media_type = 'application/json'

    def parse(self, stream, media_type=None, parser_context=None):
        chunks, used = [], 0
        while True:
            chunk = stream.read(c.MAX_BODY + 1 - used)
            if not chunk:
                break
            used += len(chunk)
            if used > c.MAX_BODY:
                c.fail('TOO_LARGE', 422)
            chunks.append(chunk)
        try:
            return json.load(
                io.StringIO(b''.join(chunks).decode('utf-8')),
                object_pairs_hook=unique_object,
                parse_constant=lambda _: c.fail('INVALID_INPUT', 400),
            )
        except (UnicodeError, ValueError, TypeError, RecursionError):
            c.fail('INVALID_INPUT', 400)


class BaseAPI(generics.GenericAPIView):
    permission_required = all_permissions.annotations_view
    parser_classes = [SecondReviewJSONParser]

    def finalize_response(self, request, response, *args, **kwargs):
        result = super().finalize_response(request, response, *args, **kwargs)
        result['Cache-Control'] = 'private, no-store'
        result['Referrer-Policy'] = 'no-referrer'
        return result

    def command(self, request, pk, action, task_id=None):
        ctx = c.current(request, pk, action)
        if request.query_params:
            c.fail('INVALID_INPUT', 400)
        key = request.headers.get('Idempotency-Key')
        if not c.uuid_string(key):
            c.fail('INVALID_INPUT', 400)
        return service.command(request, ctx, action, request.data, UUID(key), task_id)


class CapabilityAPI(BaseAPI):
    def get(self, request, pk):
        try:
            ctx = c.current(request, pk)
            readable = True
        except (APIException, Http404):
            ctx = c.current(request, pk, 'cancel')
            readable = False
        flags = {'can_read': readable}
        for action in ('organize', 'decide', 'cancel'):
            try:
                c.recheck(request, ctx, action)
                flags['can_' + action] = True
            except (APIException, Http404):
                flags['can_' + action] = False
        return c.respond(
            request,
            ctx,
            dict(effect=c.EFFECT, viewer_hash=c.viewer(ctx), owner_hash=c.owner(ctx), **flags),
            action='read' if readable else 'cancel',
        )


class ContextAPI(BaseAPI):
    def get(self, request, pk, batch_id, source_id):
        return service.read_context(request, c.current(request, pk, 'organize'), batch_id, source_id)


class AssigneesAPI(BaseAPI):
    def get(self, request, pk, batch_id, source_id):
        return service.read_context(request, c.current(request, pk, 'organize'), batch_id, source_id, assignees=True)


class ListAPI(BaseAPI):
    def get(self, request, pk):
        return service.history(request, c.current(request, pk))

    def post(self, request, pk):
        return self.command(request, pk, 'organize')


class DetailAPI(BaseAPI):
    def get(self, request, pk, task_id):
        if request.query_params:
            c.fail('INVALID_INPUT', 400)
        return service.history(request, c.current(request, pk), task_id)


class SourceAPI(BaseAPI):
    def get(self, request, pk, task_id):
        if request.query_params:
            c.fail('INVALID_INPUT', 400)
        return service.source_response(request, c.current(request, pk), task_id)


class DecisionAPI(BaseAPI):
    def post(self, request, pk, task_id):
        return self.command(request, pk, 'decide', task_id)


class CancelAPI(BaseAPI):
    def post(self, request, pk, task_id):
        return self.command(request, pk, 'cancel', task_id)


urlpatterns = [
    path('capability/', CapabilityAPI.as_view()),
    path('', ListAPI.as_view()),
    path('<uuid:task_id>/', DetailAPI.as_view()),
    path('<uuid:task_id>/source/', SourceAPI.as_view()),
    path('<uuid:task_id>/decision/', DecisionAPI.as_view()),
    path('<uuid:task_id>/cancel/', CancelAPI.as_view()),
]
