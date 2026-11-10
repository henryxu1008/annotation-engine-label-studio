"""Opt-in bbox batch preview, creation and scoped frozen-item reads."""

from access_control.authorization import authorization
from access_control.identity import resolve_actor
from access_control.project_operations import (
    ORGANIZE_BBOX,
    READ_WORK,
    project_operation_policy,
    require_project_operation,
)
from core.permissions import all_permissions
from core.utils.private_file import PrivateFileError
from django.conf import settings
from django.db import connection
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.crypto import salted_hmac
from projects.models import Project
from rest_framework import generics
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from tasks.my_work import work_projects

from . import bbox, bbox_batches, bbox_quality, bbox_storage, ner, ner_batches
from .models import BboxMediaPin


def viewer(context):
    return salted_hmac('bbox-quality-viewer/v1', bbox.digest({
        'principal': context[0].principal_id, 'source': context[0].source, 'actor': context[1].pk,
        'project': context[2].pk, 'role': context[3],
    }), algorithm='sha256').hexdigest()


def metadata(batch, context, visible):
    if not visible:
        raise NotFound()
    role = context[3]
    reference = ({'manifest_hash': batch.manifest_hash} if role == 'manager' else
                 {'projection_hash': bbox.digest({'schema': bbox_batches.SCHEMA, 'batch_id': batch.pk,
                                                  'items': visible})})
    manifest = batch.manifest
    return {'id': batch.pk, 'name': batch.name, 'project_id': context[2].pk, 'schema': bbox_batches.SCHEMA,
            'rule': bbox.RULE, 'selection': ner_batches.SELECTION, 'created_at': batch.created_at.isoformat(),
            'calculated_at': manifest['calculated_at'], 'scope_enumerated_at': manifest['scope_enumerated_at'],
            'visible_task_count': len(visible), 'visibility': 'project' if role == 'manager' else 'visible_subset',
            'viewer': viewer(context), **reference}


def visible_plan(context, batch_id):
    batch = bbox_batches.checked(get_object_or_404(bbox_batches.query(context[2]), pk=batch_id), context[2])
    try:
        plan = batch.manifest['plan']
        visible = bbox_quality.visible_items(plan, context[1], context[3])
        meta = metadata(batch, context, visible)
        filtered = {**plan, 'task_ids': [item['task_id'] for item in visible], 'items': visible}
    except (KeyError, TypeError, AttributeError) as exc:
        raise bbox.Error('BBOX_BATCH_MANIFEST_INVALID', 409) from exc
    bbox_quality.replay(context[2], filtered)
    return meta, visible, filtered


def item_row(item):
    result = item['result']
    outcome = next(state for state, count in bbox_quality.counts([item]).items() if count)
    return {'task_id': item['task_id'], 'outcome': outcome, 'reason': result.get('reason')}


class Pagination(PageNumberPagination):
    page_size = 5
    page_size_query_param = 'page_size'
    max_page_size = 5


class BatchAPI(generics.GenericAPIView):
    permission_required = all_permissions.annotations_view
    pagination_class = Pagination

    def handle_exception(self, exc):
        if isinstance(exc, (bbox.Error, ner.Error)):
            error = APIException(exc.code)
            error.status_code = exc.status
            exc = error
        return super().handle_exception(exc)

    def context(self, request, pk, *, manager=False):
        principal, actor = resolve_actor(request)
        project = get_object_or_404(Project.objects.filter(
            pk__in=work_projects(actor, ['manager', 'reviewer']), deleted_at__isnull=True), pk=pk)
        key = f'{project.organization_id}:{project.pk}'
        modes, media = settings.ANNOTATION_BBOX_QUALITY_MODES, settings.ANNOTATION_BBOX_MEDIA_PROJECTS
        role = authorization.project_role(principal, project)
        if (connection.vendor not in ('postgresql', 'sqlite') or not settings.ANNOTATION_BBOX_QUALITY_ENABLED
                or not settings.ANNOTATION_BBOX_MEDIA_ENABLED or not isinstance(modes, dict)
                or not isinstance(media, dict) or modes.get(key) != 'fork_review' or media.get(key) != 'managed'
                or role not in ('manager', 'reviewer') or manager and role != 'manager'):
            raise NotFound()
        require_project_operation(project, READ_WORK)
        if manager:
            bbox_batches.guard(project, principal)
        return principal, actor, project, role

    def recheck(self, request, pk, initial, *, manager=False):
        current = self.context(request, pk, manager=manager)
        if current[0] != initial[0] or current[1].pk != initial[1].pk or current[3] != initial[3]:
            raise NotFound()
        return current

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'private, no-store'
        response['Referrer-Policy'] = 'no-referrer'
        return response


class PreviewAPI(BatchAPI):
    def post(self, request, pk):
        if request.query_params or not isinstance(request.data, dict):
            raise ValidationError('Invalid input.')
        initial = self.context(request, pk, manager=True)
        if set(request.data) == {'name', 'scope'}:
            name, task_ids = ner_batches.preview_args(request.data, initial[2])
        elif set(request.data) == {'name', 'task_ids'}:
            name, task_ids = request.data['name'], request.data['task_ids']
        else:
            raise ValidationError('Invalid input.')
        result = bbox_batches.preview(initial[2], initial[0], name=name, task_ids=task_ids)
        self.recheck(request, pk, initial, manager=True)
        return Response({**result, 'preview_token': result['token'], 'schema': bbox_batches.SCHEMA,
                         'project_id': pk, 'viewer': viewer(initial)})


class CapabilityAPI(BatchAPI):
    def get(self, request, pk):
        if request.query_params:
            raise ValidationError('Invalid input.')
        initial = self.context(request, pk)
        body = {'enabled': True, 'project_id': pk, 'viewer': viewer(initial), 'schema': bbox_batches.SCHEMA,
                'can_create': initial[3] == 'manager' and project_operation_policy(initial[2]).allows(ORGANIZE_BBOX)}
        self.recheck(request, pk, initial)
        return Response(body)


class CreateAPI(BatchAPI):
    def get(self, request, pk):
        if (set(request.query_params) - {'page', 'page_size'}
                or any(len(request.query_params.getlist(key)) != 1 for key in request.query_params)):
            raise ValidationError('Invalid input.')
        initial = self.context(request, pk)

        def materialize(context):
            page = self.paginate_queryset(bbox_batches.visible_query(context[2], context[1], context[3]))
            results = []
            for row in page:
                batch = bbox_batches.checked(row, context[2])
                try:
                    visible = bbox_quality.visible_items(batch.manifest['plan'], context[1], context[3])
                except (KeyError, TypeError, AttributeError) as exc:
                    raise bbox.Error('BBOX_BATCH_MANIFEST_INVALID', 409) from exc
                if not visible:
                    raise bbox.Error('BBOX_BATCH_MANIFEST_INVALID', 409)
                results.append(metadata(batch, context, visible))
            return {**self.get_paginated_response(results).data, 'project_id': pk, 'viewer': viewer(context)}

        body = materialize(initial)
        current = self.recheck(request, pk, initial)
        if materialize(current) != body:
            raise bbox.Error('BBOX_BATCH_SOURCE_CHANGED', 409)
        self.recheck(request, pk, initial)
        return Response(body)

    def post(self, request, pk):
        if (request.query_params or not isinstance(request.data, dict)
                or set(request.data) not in ({'preview_token'}, {'preview_token', 'expected_plan_hash'})):
            raise ValidationError('Invalid input.')
        initial = self.context(request, pk, manager=True)
        batch, replayed = bbox_batches.create(initial[2], initial[0], token=request.data['preview_token'],
                                              idempotency_key=request.headers.get('Idempotency-Key'),
                                              expected_plan_hash=request.data.get('expected_plan_hash'))
        self.recheck(request, pk, initial, manager=True)
        visible = bbox_quality.visible_items(batch.manifest['plan'], initial[1], initial[3])
        return Response({**metadata(batch, initial, visible), 'replayed': replayed,
                         'idempotency_key': str(batch.idempotency_key), 'plan_hash': bbox.digest(batch.manifest['plan'])},
                        status=200 if replayed else 201)


class DetailAPI(BatchAPI):
    def get(self, request, pk, batch_id):
        if request.query_params:
            raise ValidationError('Invalid input.')
        initial = self.context(request, pk)

        def materialize(context):
            meta, visible, filtered = visible_plan(context, batch_id)
            return {**meta, 'display_counts': bbox_quality.counts(visible), 'plan': filtered}

        body = materialize(initial)
        bbox.bounded(body, bbox_quality.MAX_PLAN_BYTES + 65536)
        response = Response(body)
        response = self.finalize_response(request, response)
        response.render()
        if len(response.content) > bbox_quality.MAX_PLAN_BYTES + 65536:
            raise bbox.Error('BBOX_BATCH_TOO_LARGE', 422)
        current = self.recheck(request, pk, initial)
        if bbox.digest(materialize(current)) != bbox.digest(body):
            raise bbox.Error('BBOX_BATCH_SOURCE_CHANGED', 409)
        self.recheck(request, pk, initial)
        return response


class ItemsAPI(BatchAPI):
    def get(self, request, pk, batch_id):
        if (set(request.query_params) - {'state', 'page', 'page_size'}
                or any(len(request.query_params.getlist(key)) != 1 for key in request.query_params)):
            raise ValidationError('Invalid input.')
        state = request.query_params.get('state', 'disagreement')
        if state not in ('all', 'agreement', 'disagreement', 'unavailable'):
            raise ValidationError('Invalid input.')
        initial = self.context(request, pk)

        def materialize(context):
            meta, visible, _ = visible_plan(context, batch_id)
            rows = [item_row(item) for item in visible]
            page = self.paginate_queryset([row for row in rows if state == 'all' or row['outcome'] == state])
            return {**meta, **self.get_paginated_response(page).data, 'batch_id': batch_id,
                    'display_counts': bbox_quality.counts(visible)}

        body = materialize(initial)
        current = self.recheck(request, pk, initial)
        if materialize(current) != body:
            raise bbox.Error('BBOX_BATCH_SOURCE_CHANGED', 409)
        self.recheck(request, pk, initial)
        return Response(body)


class ItemAPI(BatchAPI):
    def get(self, request, pk, batch_id, task_id):
        if request.query_params:
            raise ValidationError('Invalid input.')
        initial = self.context(request, pk)

        def materialize(context):
            meta, visible, _ = visible_plan(context, batch_id)
            item = next((item for item in visible if item['task_id'] == task_id), None)
            if item is None:
                raise NotFound()
            return {**meta, 'item': {**item_row(item), 'cohort': item['cohort'], 'comparison': item['result']}}

        body = materialize(initial)
        bbox.bounded(body, bbox_quality.MAX_PLAN_BYTES + 65536)
        current = self.recheck(request, pk, initial)
        if bbox.digest(materialize(current)) != bbox.digest(body):
            raise bbox.Error('BBOX_BATCH_SOURCE_CHANGED', 409)
        self.recheck(request, pk, initial)
        return Response({**body, 'observed_at': timezone.now().isoformat()})


class ImageAPI(BatchAPI):
    def get(self, request, pk, batch_id, task_id):
        if request.query_params:
            raise ValidationError('Invalid input.')
        initial = self.context(request, pk)

        def verify(context):
            meta, visible, _ = visible_plan(context, batch_id)
            item = next((item for item in visible if item['task_id'] == task_id), None)
            if item is None or item['result'].get('availability') != 'available':
                raise NotFound()
            pin = get_object_or_404(BboxMediaPin, project=context[2], task_id=task_id)
            sources = [entry['source'] for entry in item['cohort'] if entry['source']]
            if (not sources or any(not source.get('media') or source['media']['token'] != str(pin.token)
                                   or source['media']['sha256'] != pin.sha256 for source in sources)
                    or not 0 < pin.size_bytes <= bbox.MAX_IMAGE_BYTES):
                raise NotFound()
            return meta, pin

        meta, pin = verify(initial)
        try:
            with bbox_storage.open_verified(pin.token, pin.storage_binding, pin.sha256, pin.size_bytes) as reader:
                data = reader.read(pin.size_bytes + 1)
            if bbox.image_identity(data) != {'sha256': pin.sha256, 'width': pin.width, 'height': pin.height}:
                raise PrivateFileError()
        except (PrivateFileError, bbox.Incomparable, OSError, ValueError) as exc:
            raise NotFound() from exc
        current = self.recheck(request, pk, initial)
        fresh, fresh_pin = verify(current)
        if fresh != meta or fresh_pin.pk != pin.pk:
            raise NotFound()
        self.recheck(request, pk, initial)
        response = HttpResponse(data, content_type='image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg')
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        response['Referrer-Policy'] = 'no-referrer'
        return response
