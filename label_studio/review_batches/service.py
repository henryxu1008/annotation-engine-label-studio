"""Bounded deterministic sampling over existing immutable Submissions."""

import hashlib
import json
from uuid import UUID

from access_control.authorization import authorization
from access_control.project_operations import READ_WORK, require_project_operation
from django.conf import settings
from django.db import connection
from django.db.models import OuterRef, Subquery
from django.utils import timezone
from projects.models import Project
from rest_framework import serializers
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.generics import get_object_or_404
from tasks.models import Submission
from tasks.my_work import work_projects
from tasks.submissions import _snapshot_hash

from .models import ReviewBatch

ALGORITHM = "sha256-ranked-v1"
MAX_CANDIDATES = 1000


def fail(code, status=409):
    error = APIException(code)
    error.status_code = status
    error.display_context = {"reason": code}
    raise error


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def enabled(project):
    if not settings.ANNOTATION_REVIEW_BATCHES_ENABLED:
        raise NotFound()
    if connection.vendor not in ('postgresql', 'sqlite'):
        fail('REVIEW_BATCH_BACKEND_UNSUPPORTED', 503)
    # Review authority is independent from export enablement and its credentials.
    modes = settings.ANNOTATION_REVIEW_BATCH_MODES
    if not isinstance(modes, dict) or modes.get(f"{project.organization_id}:{project.pk}") != "fork_review":
        fail("REVIEW_BATCH_MODE_FORBIDDEN", 403)


def guard(project_id, principal, actor, *, manager=False):
    roles = ["manager"] if manager else ["manager", "reviewer"]
    project = get_object_or_404(
        Project.objects.filter(pk__in=work_projects(actor, roles), deleted_at__isnull=True), pk=project_id
    )
    enabled(project)
    role = authorization.project_role(principal, project)
    if role not in roles:
        raise PermissionDenied()
    policy = require_project_operation(project, READ_WORK)
    return project, role, policy


class StrictInput(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            fail("REVIEW_BATCH_INVALID_INPUT", 400)
        return super().to_internal_value(data)


class PlanInput(StrictInput):
    name = serializers.CharField(max_length=160)
    rate = serializers.IntegerField(min_value=1, max_value=100)
    per_submitter = serializers.BooleanField(default=False)
    seed = serializers.CharField(max_length=128)
    package_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), max_length=100, default=list)
    submitter_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), max_length=100, default=list)
    expected_plan_hash = serializers.RegexField(r"^[0-9a-f]{64}$", required=False)

    def validate(self, attrs):
        for key in ("package_ids", "submitter_ids"):
            if len(attrs[key]) != len(set(attrs[key])):
                fail("REVIEW_BATCH_INVALID_INPUT", 400)
            attrs[key].sort()
        return attrs


def payload(request):
    serializer = PlanInput(data=request.data)
    serializer.is_valid(raise_exception=True)
    return dict(serializer.validated_data)


def key_from_request(request):
    try:
        return UUID(request.headers.get("Idempotency-Key", ""))
    except (ValueError, AttributeError):
        fail("REVIEW_BATCH_INVALID_KEY", 400)


def identity(row):
    return {
        "submission_id": row.pk,
        "assignment_id": row.assignment_id,
        "task_id": row.assignment.task_id,
        "revision": row.revision,
        "result_hash": row.result_hash,
        "submitter_id": row.submitted_by_id,
    }


def valid_source(row, project_id):
    if not isinstance(row.result_snapshot, dict):
        return False
    review = getattr(row, 'review', None)
    if row.status in ('approved', 'rejected'):
        if review is None or review.decision != row.status:
            return False
    elif row.status not in ('pending', 'superseded') or review is not None:
        return False
    return (
        row.assignment.project_id == project_id
        and row.assignment.task.project_id == project_id
        and row.result_hash == _snapshot_hash(row.result_snapshot)
        and isinstance(row.result_snapshot.get("project"), dict)
        and row.result_snapshot["project"].get("id") == project_id
        and isinstance(row.result_snapshot.get("task"), dict)
        and row.result_snapshot["task"].get("id") == row.assignment.task_id
    )


def build_plan(project, args):
    newest = Submission.objects.filter(assignment_id=OuterRef("assignment_id")).order_by("-revision")
    rows = Submission.objects.filter(
        assignment__project=project,
        assignment__task__project=project,
        status=Submission.Status.PENDING,
        pk=Subquery(newest.values("pk")[:1]),
    )
    if args["package_ids"]:
        from task_packages.access import live_items
        from task_packages.models import TaskPackage

        if not settings.ANNOTATION_FIXED_PACKAGES_ENABLED:
            fail("REVIEW_BATCH_PACKAGES_DISABLED", 400)
        packages = TaskPackage.objects.filter(project=project, pk__in=args["package_ids"])
        if packages.count() != len(args["package_ids"]):
            fail("REVIEW_BATCH_SCOPE_UNAVAILABLE", 400)
        rows = rows.filter(
            assignment__task_id__in=live_items(project).filter(package_id__in=args["package_ids"]).values("task_id")
        )
    if args["submitter_ids"]:
        rows = rows.filter(submitted_by_id__in=args["submitter_ids"])
    sources = list(rows.select_related("assignment__task", "review").order_by("pk")[: MAX_CANDIDATES + 1])
    if len(sources) > MAX_CANDIDATES:
        fail("REVIEW_BATCH_TOO_LARGE", 422)
    if not sources:
        fail("REVIEW_BATCH_EMPTY", 400)
    if any(not valid_source(row, project.pk) for row in sources):
        fail("REVIEW_BATCH_SOURCE_INVALID")
    candidates = [identity(row) for row in sources]
    groups = {}
    for item in candidates:
        groups.setdefault(item["submitter_id"] if args["per_submitter"] else "all", []).append(item)
    selected = set()
    for group in groups.values():
        ranked = sorted(
            group, key=lambda item: (digest([ALGORITHM, project.pk, args["seed"], item]), item["submission_id"])
        )
        count = (len(group) * args["rate"] + 99) // 100
        selected.update(item["submission_id"] for item in ranked[:count])
    return {
        "algorithm": ALGORITHM,
        "project_id": project.pk,
        "rule": {key: args[key] for key in ("rate", "per_submitter", "seed", "package_ids", "submitter_ids")},
        "candidates": [{**item, "selected": item["submission_id"] in selected} for item in candidates],
    }


def checked_batch(project, batch_id):
    batch = get_object_or_404(ReviewBatch, project=project, pk=batch_id)
    if digest(batch.plan) != batch.plan_hash or batch.plan.get("project_id") != project.pk:
        fail("REVIEW_BATCH_PLAN_INVALID")
    return batch


def checked_sources(batch):
    ids = [item["submission_id"] for item in batch.plan["candidates"]]
    rows = {row.pk: row for row in Submission.objects.filter(pk__in=ids).select_related("assignment__task", "review")}
    for item in batch.plan["candidates"]:
        row = rows.get(item["submission_id"])
        if row is not None and (
            identity(row) != {k: v for k, v in item.items() if k != "selected"}
            or not valid_source(row, batch.project_id)
        ):
            fail("REVIEW_BATCH_SOURCE_INVALID")
    return rows


def require_selected(project, batch_id, submission, actor):
    enabled(project)
    batch = checked_batch(project, batch_id)
    rows = checked_sources(batch)
    selected = any(item["selected"] and item["submission_id"] == submission.pk for item in batch.plan["candidates"])
    if not selected or submission.pk not in rows or submission.submitted_by_id == actor.pk:
        raise PermissionDenied()
    return batch


def metadata(batch):
    return {"id": batch.pk, "name": batch.name, "created_at": batch.created_at, "plan_hash": batch.plan_hash}


def projection(batch, actor, role):
    rows = checked_sources(batch)
    candidates = batch.plan["candidates"]
    if role == "reviewer":
        candidates = [item for item in candidates if item["submitter_id"] != actor.pk]
    # Minimal successor metadata only: never copy answers into a plan.
    newest = Submission.objects.filter(assignment_id=OuterRef("assignment_id")).order_by("-revision")
    successors = {
        row.assignment_id: row
        for row in Submission.objects.filter(
            assignment_id__in=[item["assignment_id"] for item in candidates],
            assignment__project_id=batch.project_id,
            assignment__task__project_id=batch.project_id,
            pk=Subquery(newest.values("pk")[:1]),
        ).only("pk", "assignment_id", "revision", "status")
    }
    items = []
    for item in candidates:
        source = rows.get(item["submission_id"])
        state = source.status if source else "unavailable"
        successor = successors.get(item["assignment_id"])
        items.append(
            {
                **item,
                "status": state,
                "review_reason": source.review.reason if source and hasattr(source, "review") else "",
                "can_review": bool(item["selected"] and state == "pending" and role == "reviewer"),
                "successor": (
                    {"id": successor.pk, "revision": successor.revision, "status": successor.status}
                    if successor and successor.revision > item["revision"]
                    else None
                ),
            }
        )
    return {
        **metadata(batch),
        "rule": batch.plan["rule"],
        "algorithm": batch.plan["algorithm"],
        "candidate_count": len(items),
        "selected_count": sum(item["selected"] for item in items),
        "items": items,
        "as_of": timezone.now(),
    }
