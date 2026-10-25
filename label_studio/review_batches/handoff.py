"""Exact-review provenance for the existing frozen-adoption arbitration ledger."""
from access_control.project_operations import ORGANIZE_ARBITRATION, project_operation_policy
from classification_disagreements import arbitration as a
from classification_disagreements.models import ArbitrationTask, ClassificationBatch
from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import APIException, NotFound
from rest_framework.generics import get_object_or_404
from tasks.models import ReviewDecision, Submission

from . import priority, service
from .models import ReviewArbitrationHandoff

SCHEMA = "review-arbitration-handoffs/v1"


def error(reason, status=409):
    service.fail("REVIEW_HANDOFF_" + reason, status)


def guard(pk, principal, actor, *, create=False):
    if not getattr(settings, "ANNOTATION_REVIEW_ARBITRATION_HANDOFF_ENABLED", False):
        raise NotFound()
    project, role, _ = service.guard(pk, principal, actor, manager=create)
    priority.read_guard(pk, principal, actor)
    other, arb_role = a.guard(pk, principal, actor, create=create)
    if other.pk != project.pk or arb_role != role:
        error("AUTHORITY_CHANGED", 403)
    return project, role


def source(context, batch_id, source_id, *, pending=False, lock=False):
    _, actor, project, role = context
    batch = service.checked_batch(project, batch_id)
    if batch.plan.get("algorithm") != priority.ALGORITHM:
        error("UNSUPPORTED_BATCH")
    candidate = next(
        (c for c in service.visible_candidates(batch, actor, role) if c["submission_id"] == source_id), None
    )
    if candidate is None:
        raise NotFound()
    selected = next((e for e in batch.plan["priority"]["evidence"] if e["submission_id"] == source_id), None)
    if not candidate["selected"] or selected is None or selected["state"] != "conflict":
        error("INELIGIBLE")
    frozen = batch.plan["priority"]
    analysis = get_object_or_404(ClassificationBatch, project=project, pk=frozen["batch_id"])
    origin = a.origin(project, analysis, candidate["task_id"], frozen["manifest_hash"])
    pin = {k: v for k, v in candidate.items() if k != "selected"}
    if (
        selected["calculation_hash"] != origin["item"]["result"]["calculation_hash"]
        or frozen["metric_rule"] != origin["metric_rule"]
        or frozen["selection"] != origin["selection"]
        or not any(e["source"] and all(e["source"].get(k) == v for k, v in pin.items()) for e in origin["item"]["cohort"])
    ):
        error("SOURCE_CHANGED")
    evidence = a.evidence(project, actor, role, origin, lock=lock)
    a.require_evidence(evidence)
    # Only this source's live metadata is needed. Answers are verified by the
    # targeted cohort loader above, never checked_sources(batch)'s 1,000 rows.
    row = get_object_or_404(
        Submission.objects.only("id", "assignment_id", "revision", "result_hash", "submitted_by_id", "status"),
        pk=source_id,
    )
    review = ReviewDecision.objects.filter(submission_id=row.pk).values_list("decision", flat=True).first()
    if service.identity(row) != pin or row.assignment.project_id != project.pk:
        error("SOURCE_CHANGED")
    if (row.status in ("approved", "rejected") and review != row.status) or (
        row.status in ("pending", "superseded") and review is not None
    ) or row.status not in ("pending", "superseded", "approved", "rejected"):
        error("SOURCE_CHANGED")
    if pending and (
        row.status != "pending"
        or Submission.objects.filter(assignment_id=row.assignment_id, revision__gt=row.revision).exists()
    ):
        error("SOURCE_NOT_PENDING")
    return {
        "batch": batch,
        "origin": origin,
        "input_set_hash": a.input_hash(project, origin["item"]),
        "status": row.status,
        "proof": {
            "review": {"batch_id": batch.pk, "plan_hash": batch.plan_hash, "candidate": pin},
            "analysis": {
                "batch_id": analysis.pk, "manifest_hash": analysis.manifest_hash,
                "metric_rule": frozen["metric_rule"], "selection": frozen["selection"],
                "calculation_hash": selected["calculation_hash"],
            },
        },
    }


def same_input(context, current, target):
    project = context[2]
    a.checked(target, project)
    batch = get_object_or_404(ClassificationBatch, project=project, pk=target.batch_id_frozen)
    if (
        a.origin(project, batch, target.task_id_frozen, target.origin["batch_manifest_hash"]) != target.origin
        or target.input_set_hash != current["input_set_hash"]
        or target.origin["item"]["cohort"] != current["origin"]["item"]["cohort"]
    ):
        error("INPUT_SET_CHANGED")


def current_target(context, current):
    target = ArbitrationTask.objects.filter(
        project=context[2], task_id_frozen=current["origin"]["item"]["task_id"],
        input_set_hash=current["input_set_hash"], status__in=("pending", "resolved"),
    ).first()
    if target:
        same_input(context, current, target)
    return target


def context_data(context, batch_id, source_id):
    if context[3] != "manager":
        raise NotFound()
    current = source(context, batch_id, source_id, pending=True)
    target = current_target(context, current)
    return {
        "schema": SCHEMA, "viewer_hash": a.viewer_hash(context),
        "review_batch_id": batch_id, "submission_id": source_id,
        "revision": current["proof"]["review"]["candidate"]["revision"],
        "expected_plan_hash": current["batch"].plan_hash,
        "expected_input_set_hash": current["input_set_hash"],
        "can_handoff": project_operation_policy(context[2]).allows(ORGANIZE_ARBITRATION),
        "dispatch_origin": {
            "batch_id": current["origin"]["batch_id"], "task_id": current["origin"]["item"]["task_id"],
            "expected_manifest_hash": current["origin"]["batch_manifest_hash"],
        },
        "existing_arbitration": a.meta(target, context) if target else None,
        "limits": {"participants": a.MAX_PARTICIPANTS, "snapshot_bytes": a.MAX_SNAPSHOT_BYTES, "text_bytes": a.MAX_TEXT_BYTES},
    }


def target_pin(target):
    return {
        "id": target.pk, "origin_hash": target.origin_hash, "input_set_hash": target.input_set_hash,
        "created_at": target.created_at.isoformat(), "actor_id": target.actor_id_frozen,
        "assignee_id": target.assignee_id_frozen, "batch_id": target.batch_id_frozen,
        "version_at_link": target.version, "status_at_link": target.status,
    }


def checked(row, project):
    try:
        o = row.origin
        review, analysis, target = o["review"], o["analysis"], o["target"]
        candidate = review["candidate"]
        valid_pin = (
            set(candidate) == {"submission_id", "assignment_id", "task_id", "revision", "result_hash", "submitter_id"}
            and all(a.integer(candidate[k]) for k in ("submission_id", "assignment_id", "task_id", "revision"))
            and (candidate["submitter_id"] is None or a.integer(candidate["submitter_id"]))
            and a.hash_value(candidate["result_hash"])
            and a.hash_value(review["plan_hash"]) and a.integer(review["batch_id"])
            and a.integer(analysis["batch_id"]) and a.hash_value(analysis["manifest_hash"])
            and a.hash_value(analysis["calculation_hash"])
            and analysis["metric_rule"] == a.service.RULE and analysis["selection"] == a.batches.SELECTION
            and all(a.integer(target[k]) for k in ("id", "actor_id", "assignee_id", "batch_id"))
            and all(a.hash_value(target[k]) for k in ("origin_hash", "input_set_hash"))
            and isinstance(target["created_at"], str) and a.integer(target["version_at_link"])
            and (target["status_at_link"], target["version_at_link"]) in (("pending", 1), ("resolved", 2))
            and (o["mode"] != "dispatch" or target["status_at_link"] == "pending")
        )
        valid = (
            valid_pin and row.origin_hash == service.digest(o) and o["schema"] == SCHEMA
            and o["project_id"] == row.project_id == project.pk
            and o["organization_id"] == project.organization_id
            and o["actor_id"] == row.actor_id_frozen and o["principal"] == row.creator_principal
            and o["created_at"] == row.created_at.isoformat()
            and o["key"] == str(row.idempotency_key) and o["request_hash"] == row.request_hash
            and o["review"]["batch_id"] == row.review_batch_id_frozen
            and o["review"]["candidate"]["submission_id"] == row.submission_id_frozen
            and o["target"]["id"] == row.arbitration_id_frozen
            and o["mode"] in ("dispatch", "link_existing")
            and row.review_batch_id in (None, row.review_batch_id_frozen)
            and row.arbitration_id in (None, row.arbitration_id_frozen)
            and row.created_by_id in (None, row.actor_id_frozen)
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        error("RECORD_INVALID")
    return row


def scoped(context, batch_id, source_id=None):
    rows = ReviewArbitrationHandoff.objects.filter(project=context[2], review_batch_id_frozen=batch_id)
    if source_id is not None:
        rows = rows.filter(submission_id_frozen=source_id)
    if context[3] == "reviewer":
        batch = service.checked_batch(context[2], batch_id)
        visible = [c["submission_id"] for c in service.visible_candidates(batch, context[1], context[3])]
        rows = rows.filter(
            review_batch_id=batch.pk, submission_id_frozen__in=visible,
            arbitration_id__in=a.scoped(context[2], context[1], context[3]).values("pk"),
        )
    return rows.order_by("-pk")


def detail(row, context):
    checked(row, context[2])
    data = {
        "schema": SCHEMA, "id": row.pk, "review_batch_id": row.review_batch_id_frozen,
        "submission_id": row.submission_id_frozen, "revision": row.origin["review"]["candidate"]["revision"],
        "arbitration_id": row.arbitration_id_frozen, "mode": row.origin["mode"],
        "created_at": row.created_at.isoformat(), "actor_id": row.actor_id_frozen,
        "target": {k: row.origin["target"][k] for k in ("id", "input_set_hash", "created_at", "assignee_id", "batch_id")},
        "viewer_hash": a.viewer_hash(context), "available": False, "reason": "source_unavailable",
        "source_status": "unavailable", "arbitration_status": None, "arbitration_version": None, "decision": None,
    }
    if row.review_batch_id is None or row.arbitration_id is None:
        return data
    # Nullable FKs are never rebound by looking up their frozen integer IDs.
    try:
        current = source(context, row.review_batch_id, row.submission_id_frozen)
        if current["proof"] != {k: row.origin[k] for k in ("review", "analysis")}:
            error("SOURCE_CHANGED")
        target = a.get_row(context, row.arbitration_id)
        same_input(context, current, target)
        pin = target_pin(target)
        if any(pin[k] != row.origin["target"][k] for k in pin if k not in ("version_at_link", "status_at_link")):
            error("TARGET_CHANGED")
        data.update(source_status=current["status"], arbitration_status=target.status, arbitration_version=target.version)
        outcome = a.detail(target, context)
        if outcome["available"]:
            data.update(available=True, reason=None, decision=outcome["decision"])
    except APIException:
        # Authority is checked again by the view; no old answers or alternate
        # revision/target is substituted on source loss.
        pass
    return data


def create_record(context, current, target, mode, key, request_hash):
    principal, actor, project, _ = context
    if ReviewArbitrationHandoff.objects.filter(
        project=project, review_batch_id_frozen=current["batch"].pk,
        submission_id_frozen=current["proof"]["review"]["candidate"]["submission_id"],
        arbitration_id_frozen=target.pk,
    ).exists():
        error("ALREADY_EXISTS")
    created = timezone.now()
    origin = {
        "schema": SCHEMA, "project_id": project.pk, "organization_id": project.organization_id,
        "actor_id": actor.pk, "principal": principal.principal_id, "created_at": created.isoformat(),
        "key": str(key), "request_hash": request_hash, "mode": mode,
        **current["proof"], "target": target_pin(target),
    }
    return ReviewArbitrationHandoff.objects.create(
        project=project, review_batch=current["batch"], arbitration=target, created_by=actor,
        review_batch_id_frozen=current["batch"].pk,
        submission_id_frozen=origin["review"]["candidate"]["submission_id"],
        arbitration_id_frozen=target.pk, actor_id_frozen=actor.pk, creator_principal=principal.principal_id,
        created_at=created, origin=origin, origin_hash=service.digest(origin),
        idempotency_key=key, request_hash=request_hash,
    )


def receipt(row, context, replayed):
    checked(row, context[2])
    return {
        "schema": SCHEMA, "id": row.pk, "review_batch_id": row.review_batch_id_frozen,
        "submission_id": row.submission_id_frozen, "arbitration_id": row.arbitration_id_frozen,
        "input_set_hash": row.origin["target"]["input_set_hash"],
        "viewer_hash": a.viewer_hash(context), "replayed": replayed,
        "receipt": {
            "operation": "handoff", "key": str(row.idempotency_key), "mode": row.origin["mode"],
            "plan_hash": row.origin["review"]["plan_hash"],
            "arbitration_version_at_commit": row.origin["target"]["version_at_link"],
            "arbitration_status_at_commit": row.origin["target"]["status_at_link"],
        },
    }
