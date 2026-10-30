"""Field analysis creation; annotation, review and arbitration stay read-only."""
from uuid import UUID

from access_control.project_operations import (
    ORGANIZE_FIELD,
    lock_project_operation,
    project_operation_policy,
    require_project_operation,
)
from django.conf import settings
from django.core import signing
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import NotFound
from rest_framework.pagination import PageNumberPagination

from . import field_api, fields
from . import field_batches as b
from .models import FieldBatch


class Pagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 100


class ListPagination(Pagination):
    page_size = 8
    max_page_size = 8

    def paginate_queryset(self, queryset, request, view=None):
        ids = super().paginate_queryset(queryset.values_list('pk', flat=True), request, view)
        return queryset.filter(pk__in=ids).iterator(chunk_size=1)


def processing(cost):
    cost = cost or dict(b.ZERO)
    return {
        'validation_passes': 2,
        'per_pass': cost,
        'total': {key: value * 2 for key, value in cost.items()},
        'scorer_calls': 0,
    }


class BatchAPI(field_api.FieldAPI):
    pagination_class = Pagination

    def context(self, request, pk, *, create=False):
        context = super().context(request, pk)
        if not settings.ANNOTATION_FIELD_BATCHES_ENABLED:
            raise NotFound()
        if create:
            if context[3] != 'manager':
                raise NotFound()
            require_project_operation(context[2], ORGANIZE_FIELD)
        return context

    def query(self, request, allowed=()):
        q = request.query_params
        if set(q) - set(allowed) or any(len(q.getlist(k)) != 1 for k in q):
            b.fail('INVALID_INPUT', 400)
        for key in ('page', 'page_size'):
            if key in q and (
                not q[key].isascii()
                or not q[key].isdigit()
                or len(q[key]) > 9
                or int(q[key]) < 1
                or (key == 'page_size' and int(q[key]) > self.pagination_class.max_page_size)
            ):
                b.fail('INVALID_INPUT', 400)

    def recheck(self, request, pk, initial, *, create=False):
        current = self.context(request, pk, create=create)
        if current[0] != initial[0] or current[1].pk != initial[1].pk or current[3] != initial[3]:
            b.fail('AUTHORITY_CHANGED')
        return current

    def batch(self, context, batch_id):
        batch = b.checked(get_object_or_404(b.query_batches(context[2]), pk=batch_id), context[2])
        b.metadata(batch, context)
        return batch

    def read(self, request, pk, materialize):
        initial = self.context(request, pk)
        data, cost = materialize(initial)

        def verify():
            current = self.recheck(request, pk, initial)
            fresh, actual = materialize(current)
            if fresh != data or actual != cost:
                b.fail('READ_CHANGED')
            self.recheck(request, pk, initial)

        return self.respond(
            request, {**data, 'observed_at': timezone.now().isoformat(), 'processing_cost': processing(cost)}, verify
        )


class CapabilityAPI(BatchAPI):
    def get(self, request, pk):
        self.query(request)

        def data(context):
            return {
                'enabled': True,
                'can_create': context[3] == 'manager' and project_operation_policy(context[2]).allows(ORGANIZE_FIELD),
                'can_view_quality_overview': False,
                'project_id': pk,
                'viewer': field_api.viewer(context),
                'schema': b.SCHEMA,
                'rule': fields.RULE,
                'selection': b.SELECTION,
                'explanation': fields.EXPLANATION,
                'engine': fields.ENGINE,
                'queue_rule': b.QUEUE_RULE,
                'limits': {
                    'tasks': b.MAX_TASKS,
                    'participants': b.MAX_PARTICIPANTS,
                    'inputs': b.MAX_INPUTS,
                    'snapshot_bytes': fields.MAX_SNAPSHOT_BYTES,
                    'stored_bytes': fields.MAX_STORED_BYTES,
                    'normalizations_per_pass': b.MAX_NORMALIZATIONS,
                    'equalities_per_pass': b.MAX_EQUALITIES,
                    'list_page_size': 8,
                    'queue_page_size': 100,
                    'scorer_calls': 0,
                },
            }, {}

        return self.read(request, pk, data)


class PreviewAPI(BatchAPI):
    def post(self, request, pk):
        self.query(request)
        initial = self.context(request, pk, create=True)
        principal, actor, project, _ = initial
        name, ids = b.preview_args(request.data, project)
        plan, prepared = b.build_plan(project, ids)
        payload = {
            'schema': b.SCHEMA,
            'project_id': pk,
            'organization_id': project.organization_id,
            'creator_principal': principal.principal_id,
            'creator_source': principal.source,
            'actor_id_frozen': actor.pk,
            'role': initial[3],
            'name': name,
            'task_ids': ids,
            'scope_enumerated_at': timezone.now().isoformat(),
            'plan_hash': fields.digest(plan),
            'selection': b.SELECTION,
            'rule': fields.RULE,
            'explanation': fields.EXPLANATION,
            'engine': fields.ENGINE,
            'queue_rule': b.QUEUE_RULE,
        }
        token = signing.dumps(payload, salt=b.PREVIEW_SALT, compress=False)
        if len(token.encode()) > fields.MAX_TOKEN:
            b.fail('TOO_LARGE', 422)

        def verify():
            current = self.recheck(request, pk, initial, create=True)
            fresh, values = b.build_plan(current[2], ids)
            if fresh != plan:
                b.fail('PREVIEW_CHANGED')
            if values != prepared:
                b.fail('COST_CHANGED')
            self.recheck(request, pk, initial, create=True)

        return self.respond(
            request,
            {
                'schema': b.SCHEMA,
                'project_id': pk,
                'viewer': field_api.viewer(initial),
                'preview_token': token,
                'plan_hash': payload['plan_hash'],
                'task_ids': ids,
                'display_counts': b.counts([i['result'] for i in plan['tasks']]),
                'scope_enumerated_at': payload['scope_enumerated_at'],
                'processing_cost': processing(prepared),
            },
            verify,
        )


class ListAPI(BatchAPI):
    pagination_class = ListPagination

    def get(self, request, pk):
        self.query(request, ('page', 'page_size'))

        def materialize(context):
            rows = self.paginate_queryset(b.visible_batches(context[2], context[1], context[3]))
            try:
                data = self.get_paginated_response(
                    [b.metadata(b.checked(row, context[2]), context) for row in rows]
                ).data
            finally:
                # Close the named PostgreSQL cursor before a rejected read's
                # transaction/savepoint ends, including failed Manifest checks.
                rows.close()
            return {**data, 'project_id': pk, 'viewer': field_api.viewer(context)}, {}

        return self.read(request, pk, materialize)

    def post(self, request, pk):
        self.query(request)
        initial = self.context(request, pk, create=True)
        body = request.data
        if (
            not isinstance(body, dict)
            or set(body) != {'preview_token', 'expected_plan_hash'}
            or not isinstance(body['expected_plan_hash'], str)
            or not field_api.HEX.fullmatch(body['expected_plan_hash'])
        ):
            b.fail('INVALID_INPUT', 400)
        token = b.validated_text(body['preview_token'], fields.MAX_TOKEN)
        if token.startswith('.') or len(token.encode()) > fields.MAX_TOKEN:
            b.fail('INVALID_INPUT', 400)
        try:
            key = UUID(request.headers.get('Idempotency-Key', ''))
        except (ValueError, AttributeError):
            b.fail('INVALID_KEY', 400)
        request_hash = fields.digest(body)
        with transaction.atomic():
            project, actor = lock_project_operation(pk, initial[0], ORGANIZE_FIELD)
            current = self.recheck(request, pk, initial, create=True)
            if actor.pk != current[1].pk:
                b.fail('AUTHORITY_CHANGED')
            previous = b.query_batches(project).filter(idempotency_key=key).first()
            plan, prepared = None, {}
            if previous is not None:
                if (
                    previous.request_hash != request_hash
                    or previous.creator_principal != initial[0].principal_id
                    or previous.actor_id_frozen != actor.pk
                ):
                    b.fail('KEY_CONFLICT')
                batch = b.checked(previous, project)
                if batch.manifest['creator_source'] != initial[0].source:
                    b.fail('KEY_CONFLICT')
            else:
                try:
                    payload = signing.loads(token, salt=b.PREVIEW_SALT, max_age=b.PREVIEW_AGE)
                except (signing.BadSignature, ValueError, TypeError):
                    b.fail('PREVIEW_INVALID', 400)
                keys = {
                    'schema',
                    'project_id',
                    'organization_id',
                    'creator_principal',
                    'creator_source',
                    'actor_id_frozen',
                    'role',
                    'name',
                    'task_ids',
                    'scope_enumerated_at',
                    'plan_hash',
                    'selection',
                    'rule',
                    'explanation',
                    'engine',
                    'queue_rule',
                }
                if (
                    not isinstance(payload, dict)
                    or set(payload) != keys
                    or payload['schema'] != b.SCHEMA
                    or payload['project_id'] != pk
                    or payload['organization_id'] != project.organization_id
                    or payload['creator_principal'] != initial[0].principal_id
                    or payload['actor_id_frozen'] != actor.pk
                    or payload['creator_source'] != initial[0].source
                    or payload['role'] != current[3]
                    or payload['selection'] != b.SELECTION
                    or payload['rule'] != fields.RULE
                    or payload['explanation'] != fields.EXPLANATION
                    or payload['engine'] != fields.ENGINE
                    or payload['queue_rule'] != b.QUEUE_RULE
                    or payload['plan_hash'] != body['expected_plan_hash']
                    or not isinstance(payload['name'], str)
                    or not 1 <= len(payload['name']) <= 160
                ):
                    b.fail('PREVIEW_INVALID', 400)
                # Older previews can have a valid signature but contain text
                # unsafe for UTF-8/PostgreSQL. Preserve the exact signed intent.
                if b.validated_text(payload['name'], 160, trim=True, code='PREVIEW_INVALID') != payload['name']:
                    b.fail('PREVIEW_INVALID', 400)
                plan, prepared = b.build_plan(project, payload['task_ids'], lock=True)
                if fields.digest(plan) != payload['plan_hash']:
                    b.fail('PREVIEW_CHANGED')
                manifest = {
                    **plan,
                    'schema': b.SCHEMA,
                    'project_id': pk,
                    'organization_id': project.organization_id,
                    'creator_principal': initial[0].principal_id,
                    'creator_source': initial[0].source,
                    'actor_id_frozen': actor.pk,
                    'name': payload['name'],
                    'scope_enumerated_at': payload['scope_enumerated_at'],
                    'calculated_at': timezone.now().isoformat(),
                }
                fields.bounded(manifest, b.MAX_MANIFEST)
                batch = FieldBatch.objects.create(
                    project=project,
                    created_by=actor,
                    creator_principal=initial[0].principal_id,
                    actor_id_frozen=actor.pk,
                    name=payload['name'],
                    idempotency_key=key,
                    request_hash=request_hash,
                    manifest=manifest,
                    manifest_hash=fields.digest(manifest),
                )

            def verify():
                context = self.recheck(request, pk, initial, create=True)
                fresh = self.batch(context, batch.pk)
                if fresh.manifest_hash != batch.manifest_hash:
                    b.fail('MANIFEST_INVALID')
                if plan is not None:
                    value, prepared_again = b.build_plan(project, plan['task_ids'])
                    if value != plan:
                        b.fail('PREVIEW_CHANGED')
                    if prepared_again != prepared:
                        b.fail('COST_CHANGED')
                self.recheck(request, pk, initial, create=True)

            return self.respond(
                request,
                {
                    **b.metadata(batch, current),
                    'replayed': previous is not None,
                    'idempotency_key': str(key),
                    'plan_hash': b.plan_hash(batch),
                    'processing_cost': processing(prepared),
                },
                verify,
                status=200 if previous else 201,
            )


class DetailAPI(BatchAPI):
    def get(self, request, pk, batch_id):
        self.query(request)

        def materialize(context):
            batch = self.batch(context, batch_id)
            items, prepared = b.project_items(batch, context)
            return {**b.metadata(batch, context), 'display_counts': b.counts(items)}, prepared

        return self.read(request, pk, materialize)


class ItemsAPI(BatchAPI):
    def get(self, request, pk, batch_id):
        self.query(request, ('state', 'page', 'page_size'))
        state = request.query_params.get('state', 'disagreement')
        if state not in ('all', 'agreement', 'disagreement', 'unavailable'):
            b.fail('INVALID_INPUT', 400)

        def materialize(context):
            batch = self.batch(context, batch_id)
            items, prepared = b.project_items(batch, context)
            page = self.paginate_queryset([i for i in items if state == 'all' or i['outcome'] == state])
            return {
                **self.get_paginated_response([b.public_row(row) for row in page]).data,
                **b.metadata(batch, context),
                'batch_id': batch.pk,
                'display_counts': b.counts(items),
            }, prepared

        return self.read(request, pk, materialize)


class ItemAPI(BatchAPI):
    def get(self, request, pk, batch_id, task_id):
        self.query(request, ('left', 'right'))
        if bool('left' in request.query_params) != bool('right' in request.query_params):
            b.fail('INVALID_INPUT', 400)

        def materialize(context):
            batch = self.batch(context, batch_id)
            if not any(i['task_id'] == task_id for i in b.visible_items(batch, context[1], context[3])):
                raise NotFound()
            items, prepared = b.project_items(batch, context)
            row = next(i for i in items if i['task_id'] == task_id)
            frozen_item = next(i for i in b.visible_items(batch, context[1], context[3]) if i['task_id'] == task_id)
            ids = sorted(e['source']['submission_id'] for e in frozen_item['cohort'] if e['source'])
            selected = ids[:2]
            if 'left' in request.query_params:
                selected = sorted(field_api.parse_id(request.query_params[k]) for k in ('left', 'right'))
                if len(set(selected)) != 2 or not set(selected).issubset(ids):
                    b.fail('INVALID_PAIR', 400)
            comparison = fields.public(row['_prepared'], tuple(selected) if len(selected) == 2 else None)
            evidence = b.item_evidence(batch, context, row)
            return {
                **b.metadata(batch, context),
                'item': {
                    **b.public_row(row),
                    'inputs': row['inputs'],
                    'comparison': comparison,
                    'evidence': evidence,
                    'evidence_hash': fields.digest(evidence) if evidence else None,
                },
            }, prepared

        return self.read(request, pk, materialize)
