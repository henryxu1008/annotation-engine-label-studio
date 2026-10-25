from uuid import UUID

from access_control.identity import resolve_actor
from access_control.project_operations import ORGANIZE_ARBITRATION, lock_project_operation
from classification_disagreements import arbitration as a
from classification_disagreements.arbitration_api import ArbitrationAPI
from classification_disagreements.models import ArbitrationTask
from django.db import transaction
from rest_framework.generics import get_object_or_404

from . import handoff as h, service
from .models import ReviewArbitrationHandoff


class HandoffAPI(ArbitrationAPI):
    def context(self, request, pk, task_id=None, *, create=False, **options):
        principal, actor = resolve_actor(request)
        project, role = h.guard(pk, principal, actor, create=create)
        return principal, actor, project, role


class ContextAPI(HandoffAPI):
    def get(self, request, pk, batch_id, source_id):
        self.query(request)
        return self.read(request, pk, lambda c: h.context_data(c, batch_id, source_id))


def command(request):
    value = request.data
    base = {"mode", "expected_plan_hash", "expected_input_set_hash"}
    if not isinstance(value, dict) or value.get("mode") not in ("dispatch", "link_existing"):
        h.error("INVALID_INPUT", 400)
    fields = {"assignee_id"} if value["mode"] == "dispatch" else {"arbitration_id", "expected_arbitration_version"}
    if set(value) != base | fields or any(not a.hash_value(value[k]) for k in ("expected_plan_hash", "expected_input_set_hash")):
        h.error("INVALID_INPUT", 400)
    if any(not a.integer(value[k]) for k in fields):
        h.error("INVALID_INPUT", 400)
    try:
        key = UUID(request.headers.get("Idempotency-Key", ""))
    except (ValueError, AttributeError):
        h.error("INVALID_KEY", 400)
    return value, key


class ListAPI(HandoffAPI):
    def get(self, request, pk, batch_id, source_id):
        self.query(request, ("page", "page_size"))

        def materialize(context):
            rows = h.scoped(context, batch_id, source_id)
            page = self.paginate_queryset(rows)
            return {
                **self.get_paginated_response([h.detail(row, context) for row in page]).data,
                "schema": h.SCHEMA, "viewer_hash": a.viewer_hash(context),
                "review_batch_id": batch_id, "submission_id": source_id,
            }

        return self.read(request, pk, materialize)

    def post(self, request, pk, batch_id, source_id):
        self.query(request)
        context = self.context(request, pk, create=True)
        value, key = command(request)
        request_hash = service.digest({
            "action": "review-arbitration-handoff", "project_id": pk,
            "batch_id": batch_id, "source_id": source_id, "body": value,
        })
        principal = context[0]
        with transaction.atomic():
            _, actor = lock_project_operation(
                pk, principal, ORGANIZE_ARBITRATION,
                related_user_ids=[value["assignee_id"]] if value["mode"] == "dispatch" else (),
            )
            context = self.recheck(request, pk, context, create=True)
            if actor.pk != context[1].pk:
                h.error("AUTHORITY_CHANGED", 403)
            previous = ReviewArbitrationHandoff.objects.filter(project=context[2], idempotency_key=key).first()
            initial = None
            if previous:
                if (
                    previous.creator_principal != principal.principal_id
                    or previous.actor_id_frozen != actor.pk or previous.request_hash != request_hash
                    or previous.review_batch_id_frozen != batch_id or previous.submission_id_frozen != source_id
                ):
                    h.error("KEY_CONFLICT")
                row = h.checked(previous, context[2])
            else:
                # Existing arbitration commands lock the arbitration before its
                # sources, under the shared project fence. Keep that order.
                target = a.get_row(context, value["arbitration_id"], lock=True) if value["mode"] == "link_existing" else None
                current = h.source(context, batch_id, source_id, pending=True, lock=True)
                if current["batch"].plan_hash != value["expected_plan_hash"] or current["input_set_hash"] != value["expected_input_set_hash"]:
                    h.error("SOURCE_CHANGED")
                if target is None:
                    # A new handoff cannot borrow an unrelated create receipt.
                    if ArbitrationTask.objects.filter(project=context[2], idempotency_key=key).exists():
                        h.error("KEY_CONFLICT")
                    dispatch = {
                        "batch_id": current["origin"]["batch_id"],
                        "task_id": current["origin"]["item"]["task_id"],
                        "expected_manifest_hash": current["origin"]["batch_manifest_hash"],
                        "assignee_id": value["assignee_id"],
                    }
                    dispatch_hash = a.digest({"action": "create", "project_id": pk, "body": dispatch})
                    target, _ = a.create_task(context, dispatch, key, dispatch_hash)
                else:
                    if target.status not in ("pending", "resolved") or target.version != value["expected_arbitration_version"]:
                        h.error("CAS_CONFLICT")
                    h.same_input(context, current, target)
                row = h.create_record(context, current, target, value["mode"], key, request_hash)
                initial = current["proof"]

            result = h.receipt(row, context, previous is not None)

            def verify():
                fresh_context = self.recheck(request, pk, context, create=True)
                fresh = get_object_or_404(ReviewArbitrationHandoff, project=fresh_context[2], pk=row.pk)
                if h.receipt(fresh, fresh_context, previous is not None) != result:
                    h.error("RECEIPT_CHANGED")
                if initial is not None:
                    current = h.source(fresh_context, batch_id, source_id, pending=True)
                    if current["proof"] != initial:
                        h.error("SOURCE_CHANGED")
                    target = a.get_row(fresh_context, fresh.arbitration_id)
                    h.same_input(fresh_context, current, target)
                    if h.target_pin(target) != fresh.origin["target"]:
                        h.error("TARGET_CHANGED")
                    if value["mode"] == "dispatch":
                        a.assignee(fresh_context[2], value["assignee_id"], target.origin["item"])
                self.recheck(request, pk, context, create=True)

            return self.respond(request, result, verify, status=200 if previous else 201)


class DetailAPI(HandoffAPI):
    def get(self, request, pk, batch_id, handoff_id):
        self.query(request)
        return self.read(
            request, pk,
            lambda c: h.detail(get_object_or_404(h.scoped(c, batch_id), pk=handoff_id), c),
        )
