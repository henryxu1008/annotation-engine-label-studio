"""Explicit NER handoff commands; parent receipts never borrow child create keys."""
from uuid import UUID, uuid4

from access_control.identity import resolve_actor
from access_control.project_operations import ORGANIZE_NER_ARBITRATION, lock_project_operation
from classification_disagreements import ner, ner_api
from classification_disagreements import ner_arbitration as a
from classification_disagreements import ner_arbitration_api as api
from django.db import transaction
from django.shortcuts import get_object_or_404

from . import ner_handoff as h


def command(request):
    if len(request.body) > a.MAX_COMMAND:
        h.error('BUDGET_EXCEEDED', 422)
    value = request.data
    base = {'mode', 'expected_plan_hash', 'expected_input_set_hash', 'expected_viewer_hash'}
    if not isinstance(value, dict) or value.get('mode') not in ('dispatch', 'link_existing'):
        h.error('INVALID_INPUT', 400)
    fields = {'assignee_id'} if value['mode'] == 'dispatch' else {'ner_arbitration_id', 'expected_arbitration_version'}
    if (
        set(value) != base | fields
        or any(not a.hash_value(value[k]) for k in base - {'mode'})
        or any(not a.integer(value[k]) for k in fields)
    ):
        h.error('INVALID_INPUT', 400)
    ner.bounded(value, a.MAX_COMMAND)
    try:
        key = UUID(request.headers.get('Idempotency-Key', ''))
    except (ValueError, AttributeError):
        h.error('INVALID_KEY', 400)
    return value, key


class HandoffAPI(api.ArbitrationAPI):
    def handle_exception(self, exc):
        return ner_api.NerAPI.handle_exception(self, exc)

    def context(self, request, pk, *, create=False, **options):
        principal, actor = resolve_actor(request)
        project, role = h.guard(pk, principal, actor, create=create)
        return principal, actor, project, role


class ContextAPI(HandoffAPI):
    def get(self, request, pk, batch_id, source_id):
        self.query(request)
        return self.read(request, pk, lambda c: h.context_data(c, batch_id, source_id))


class ListAPI(HandoffAPI):
    def get(self, request, pk, batch_id, source_id):
        self.query(request, ('page', 'page_size'))

        def materialize(context):
            budget = h.Metadata()
            identifiers = self.paginate_queryset(
                h.scoped(context, batch_id, source_id, budget=budget).values_list('pk', flat=True)
            )
            result = [h.detail(row, context, budget) for row in h.prepare_page(context, identifiers, budget)]
            return {
                **self.get_paginated_response(result).data,
                'schema': h.SCHEMA,
                'kind': 'ner',
                'viewer_hash': a.viewer_hash(context),
                'project_id': pk,
                'review_batch_id': batch_id,
                'submission_id': source_id,
            }, {}

        return self.read(request, pk, materialize)

    def post(self, request, pk, batch_id, source_id):
        self.query(request)
        context = self.context(request, pk, create=True)
        value, key = command(request)
        request_hash = ner.digest(
            {
                'schema': h.SCHEMA,
                'action': 'handoff',
                'project_id': pk,
                'batch_id': batch_id,
                'source_id': source_id,
                'body': value,
            }
        )
        principal = context[0]
        with transaction.atomic():
            project, actor = lock_project_operation(
                pk,
                principal,
                ORGANIZE_NER_ARBITRATION,
                related_user_ids=[value['assignee_id']] if value['mode'] == 'dispatch' else (),
            )
            context = self.recheck(request, pk, context, create=True)
            if actor.pk != context[1].pk:
                h.error('AUTHORITY_CHANGED')
            previous = (
                h.NerReviewArbitrationHandoff.objects.filter(project=project, idempotency_key=key)
                .only(
                    'pk',
                    'creator_principal',
                    'creator_source',
                    'actor_id_frozen',
                    'request_hash',
                    'review_batch_id_frozen',
                    'submission_id_frozen',
                )
                .first()
            )
            current = None
            prepared = {}
            if previous:
                api.same_receipt(
                    context,
                    request_hash,
                    previous.creator_principal,
                    previous.creator_source,
                    previous.actor_id_frozen,
                    previous.request_hash,
                )
                if previous.review_batch_id_frozen != batch_id or previous.submission_id_frozen != source_id:
                    h.error('KEY_CONFLICT')
                row = h.checked(get_object_or_404(h.rows(project), pk=previous.pk), project)
            else:
                if value['expected_viewer_hash'] != a.viewer_hash(context):
                    h.error('AUTHORITY_CHANGED')
                budget = h.Metadata()
                target = (
                    h.target(context, value['ner_arbitration_id'], budget, lock=True)
                    if value['mode'] == 'link_existing'
                    else None
                )
                batch = h.review_batch(context, batch_id, budget)
                if batch.plan.get('algorithm') != h.p.ALGORITHM:
                    h.error('UNSUPPORTED_BATCH')
                # Keep the existing target-before-sources order, then all source batch IDs sorted.
                ids = {batch.plan.get('priority', {}).get('batch_id')}
                if target:
                    ids.add(target.ner_batch_id_frozen)
                if None in ids:
                    h.error('UNSUPPORTED_BATCH')
                for identifier in sorted(ids):
                    h.analysis(context, identifier, budget, lock=True)
                current = h.source(context, batch_id, source_id, budget, lock=True)
                if (
                    current['batch'].plan_hash != value['expected_plan_hash']
                    or current['input_set_hash'] != value['expected_input_set_hash']
                ):
                    h.error('SOURCE_CHANGED')
                if target:
                    if (
                        target.status not in ('pending', 'resolved')
                        or target.version != value['expected_arbitration_version']
                    ):
                        h.error('CAS_CONFLICT')
                    h.same_input(context, current, target, budget)
                else:
                    dispatch = {
                        'ner_batch_id': current['origin']['ner_batch_id'],
                        'task_id': current['origin']['item']['task_id'],
                        'expected_manifest_hash': current['origin']['batch_manifest_hash'],
                        'assignee_id': value['assignee_id'],
                    }
                    target, _, _ = api.create_task(
                        context,
                        dispatch,
                        uuid4(),
                        api.command_hash('create', pk, dispatch),
                        verified=(current['origin'], current['evidence'], current['prepared']),
                    )
                    target = h.target(context, target.pk, budget)
                row = h.create_record(context, current, target, value['mode'], key, request_hash, budget)
                prepared = current['prepared']
            result = h.receipt(row, context, previous is not None)

            def verify():
                fresh_context = self.recheck(request, pk, context, create=True)
                a.check_engine(prepared)
                fresh = get_object_or_404(h.rows(project), pk=row.pk)
                if h.receipt(fresh, fresh_context, previous is not None) != result:
                    h.error('RECEIPT_CHANGED')
                if current is not None:
                    budget = h.Metadata()
                    target = h.target(fresh_context, fresh.ner_arbitration_id, budget)
                    reloaded = h.source(fresh_context, batch_id, source_id, budget)
                    h.same_input(fresh_context, reloaded, target, budget)
                    if (
                        reloaded['proof'] != current['proof']
                        or reloaded['evidence'] != current['evidence']
                        or h.target_pin(target) != fresh.origin['target']
                    ):
                        h.error('SOURCE_CHANGED')
                    if value['mode'] == 'dispatch':
                        a.assignee(fresh_context, value['assignee_id'], target.origin['item'])
                self.recheck(request, pk, context, create=True)

            return self.respond(
                request, result, verify, lambda: a.check_engine(prepared), status=200 if previous else 201
            )


class DetailAPI(HandoffAPI):
    def get(self, request, pk, batch_id, handoff_id):
        self.query(request)

        def materialize(context):
            budget = h.Metadata()
            visible = get_object_or_404(h.scoped(context, batch_id, budget=budget).values('pk'), pk=handoff_id)
            page = h.prepare_page(context, [visible['pk']], budget)
            if not page:
                raise h.NotFound()
            return h.detail(page[0], context, budget), {}

        return self.read(request, pk, materialize)
