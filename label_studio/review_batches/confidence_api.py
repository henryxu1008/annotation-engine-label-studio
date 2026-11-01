"""Producer-only transport, separate from human review planning."""
import io
import json
from uuid import UUID

from access_control.identity import resolve_actor
from core.permissions import all_permissions
from rest_framework import generics
from rest_framework.parsers import BaseParser

from . import confidence_contract as c
from . import confidence_observations as observations


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            c.fail('INVALID_INPUT', 400)
        result[key] = value
    return result


class ConfidenceJSONParser(BaseParser):
    media_type = 'application/json'

    def parse(self, stream, media_type=None, parser_context=None):
        data = stream.read(c.MAX_BODY + 1)
        if len(data) > c.MAX_BODY:
            c.fail('TOO_LARGE', 422)
        try:
            return json.load(
                io.StringIO(data.decode('utf-8')),
                object_pairs_hook=unique_object,
                parse_constant=lambda value: c.fail('INVALID_INPUT', 400),
            )
        except (UnicodeError, ValueError, TypeError, RecursionError):
            c.fail('INVALID_INPUT', 400)


class ProducerAPI(generics.GenericAPIView):
    permission_required = all_permissions.annotations_view
    parser_classes = [ConfidenceJSONParser]

    def current(self, request, pk):
        if not c.positive(pk):
            c.fail('INVALID_INPUT', 400)
        principal, actor = resolve_actor(request)
        return c.guard(pk, principal, actor, producer=True)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'private, no-store'
        response['Referrer-Policy'] = 'no-referrer'
        return response


class InputAPI(ProducerAPI):
    def get(self, request, pk, source_id):
        initial = self.current(request, pk)
        if not c.positive(source_id):
            c.fail('INVALID_INPUT', 400)
        if set(request.query_params) != {'profile_id', 'profile_digest'} or any(
            len(v) != 1 for _, v in request.query_params.lists()
        ):
            c.fail('INVALID_INPUT', 400)
        selection = dict(request.query_params.items())
        if not c.uuid_string(selection['profile_id']) or not c.hexadecimal(selection['profile_digest']):
            c.fail('INVALID_INPUT', 400)
        return observations.input_response(request, initial, source_id, selection)


class ObservationAPI(ProducerAPI):
    def post(self, request, pk):
        initial = self.current(request, pk)
        if request.query_params:
            c.fail('INVALID_INPUT', 400)
        body = observations.parse(request.data)
        event = request.headers.get('Idempotency-Key')
        if not c.uuid_string(event):
            c.fail('INVALID_INPUT', 400)
        return observations.create(request, initial, body, UUID(event))
