"""Bounded, durable classification plans over independently assigned revisions."""

import json

from django.db import connection, models
from django.db.models import OuterRef, Subquery
from django.db.models.expressions import RawSQL
from rest_framework.exceptions import NotFound
from review_batches.service import digest, fail, valid_source
from tasks.models import Submission, Task, TaskAssignment

from . import service
from .models import ClassificationBatch

SCHEMA = "classification-batches/v1"
SELECTION = "active-assignment-latest/v1"
PREVIEW_SALT = "classification-batch-preview/v1"
MAX_TASKS = 100
MAX_PARTICIPANTS = 20
MAX_INPUTS = 2000
MAX_BYTES = 8 * 1024 * 1024
MAX_TOKEN = 16384
PREVIEW_AGE = 15 * 60


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def too_large():
    fail("CLASSIFICATION_BATCH_TOO_LARGE", 422)


def positive_ids(value):
    if not isinstance(value, list) or not value or any(type(v) is not int or not 0 < v <= 2**53 - 1 for v in value):
        fail("CLASSIFICATION_BATCH_INVALID_INPUT", 400)
    if len(value) > MAX_TASKS:
        too_large()
    if len(set(value)) != len(value):
        fail("CLASSIFICATION_BATCH_INVALID_INPUT", 400)
    return sorted(value)


def preview_args(data, project):
    if not isinstance(data, dict) or set(data) != {"name", "scope"}:
        fail("CLASSIFICATION_BATCH_INVALID_INPUT", 400)
    name, scope = data["name"], data["scope"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160 or not isinstance(scope, dict):
        fail("CLASSIFICATION_BATCH_INVALID_INPUT", 400)
    if scope == {"kind": "project"}:
        ids = list(Task.objects.filter(project=project).order_by("pk").values_list("pk", flat=True)[: MAX_TASKS + 1])
    elif set(scope) == {"kind", "task_ids"} and scope["kind"] == "tasks":
        ids = scope["task_ids"]
    else:
        fail("CLASSIFICATION_BATCH_INVALID_INPUT", 400)
    return name.strip(), positive_ids(ids)


def source_pin(row):
    return {**service.pin(row), "task_data_hash": digest(row.result_snapshot["task"].get("data"))}


def outcome(report):
    if report["status"] == "unavailable":
        return "unavailable"
    return "agreement" if report["matching_pairs"] == report["total_pairs"] else "disagreement"


def frozen_result(report):
    # No answer sets or floating-point metrics are duplicated in the batch.
    return {
        "outcome": outcome(report),
        "reason": report["reason"],
        "matching_pairs": report["matching_pairs"],
        "total_pairs": report["total_pairs"],
        "locations": [
            {key: location[key] for key in ("from_name", "to_name", "matching_pairs", "total_pairs", "disagrees")}
            for location in report["locations"]
        ],
        "calculation_hash": digest(report),
    }


def load_sources(ids, *, strict=False):
    # Check each decoded snapshot before retaining it or fetching the next one.
    # Live task bodies and review comments are not classification inputs.
    query = (
        Submission.objects.filter(pk__in=ids)
        .select_related("assignment__task", "review")
        .only(
            "id",
            "assignment_id",
            "revision",
            "result_snapshot",
            "result_hash",
            "submitted_by_id",
            "status",
            "assignment__id",
            "assignment__task_id",
            "assignment__project_id",
            "assignment__assignee_id",
            "assignment__task__id",
            "assignment__task__project_id",
            "review__id",
            "review__decision",
        )
        .order_by("pk")
    )
    used_bytes, sources = 0, {}
    for row in query.iterator(chunk_size=1):
        try:
            used_bytes += len(canonical(row.result_snapshot))
        except (TypeError, ValueError):
            if strict:
                fail("CLASSIFICATION_BATCH_SOURCE_INVALID")
            return {}
        if used_bytes > MAX_BYTES:
            if strict:
                too_large()
            # A corrupted live source cannot reveal a partial frozen answer set.
            return {}
        sources[row.pk] = row
    return sources


def build_plan(project, ids, *, lock=False):
    ids = positive_ids(ids)
    tasks = Task.objects.filter(pk__in=ids).only("id", "project_id").order_by("pk")
    if lock:
        tasks = tasks.select_for_update()
    tasks = list(tasks)
    if len(tasks) != len(ids) or any(task.project_id != project.pk for task in tasks):
        fail("CLASSIFICATION_BATCH_SCOPE_CHANGED")
    assignments = TaskAssignment.objects.filter(task_id__in=ids, status__in=TaskAssignment.ACTIVE_STATUSES).order_by(
        "pk"
    )
    if lock:
        assignments = assignments.select_for_update()
    assignments = list(assignments[: MAX_INPUTS + 1])
    if len(assignments) > MAX_INPUTS:
        too_large()
    if any(a.project_id != project.pk for a in assignments):
        fail("CLASSIFICATION_BATCH_SOURCE_INVALID")
    grouped = {task_id: [] for task_id in ids}
    for assignment in assignments:
        grouped[assignment.task_id].append(assignment)
        if len(grouped[assignment.task_id]) > MAX_PARTICIPANTS:
            too_large()
    newest = Submission.objects.filter(assignment_id=OuterRef("assignment_id")).order_by("-revision", "-pk")
    selected = Submission.objects.filter(
        assignment_id__in=[a.pk for a in assignments], pk=Subquery(newest.values("pk")[:1])
    )
    source_ids = list(selected.order_by("pk").values_list("pk", flat=True))
    if lock:
        list(
            Submission.objects.filter(pk__in=source_ids)
            .select_for_update()
            .order_by("pk")
            .values_list("pk", flat=True)
        )
    sources = {row.assignment_id: row for row in load_sources(source_ids, strict=True).values()}
    items = []
    for task_id in ids:
        cohort, rows, reason = [], [], None
        for assignment in grouped[task_id]:
            row = sources.get(assignment.pk)
            if row is not None:
                try:
                    valid = valid_source(row, project.pk)
                except (TypeError, ValueError, KeyError):
                    valid = False
                if not valid or (row.submitted_by_id is not None and row.submitted_by_id != assignment.assignee_id):
                    fail("CLASSIFICATION_BATCH_SOURCE_INVALID")
                rows.append(row)
                if row.status == Submission.Status.SUPERSEDED:
                    reason = "latest_revision_unusable"
            cohort.append(
                {
                    "assignment_id": assignment.pk,
                    "assignee_id": assignment.assignee_id,
                    "source": source_pin(row) if row is not None else None,
                }
            )
        if len(cohort) < 2:
            reason = "insufficient_inputs"
        elif len({entry["assignee_id"] for entry in cohort}) != len(cohort):
            reason = "duplicate_submitter"
        elif any(entry["source"] is None for entry in cohort):
            reason = "missing_submission"
        report = service.unavailable(reason) if reason else service.calculate(rows, project.pk)
        items.append(
            {"task_id": task_id, "cohort": cohort, "selection_reason": reason, "result": frozen_result(report)}
        )
    return {"selection": SELECTION, "rule": service.RULE, "task_ids": ids, "tasks": items}


def checked(batch, project):
    manifest = batch.manifest
    try:
        valid = (
            digest(manifest) == batch.manifest_hash
            and manifest["schema"] == SCHEMA
            and manifest["project_id"] == project.pk
            and manifest["organization_id"] == project.organization_id
            and manifest["creator_principal"] == batch.creator_principal
            and manifest["actor_id_frozen"] == batch.actor_id_frozen
            and manifest["name"] == batch.name
            and manifest["selection"] == SELECTION
            and manifest["rule"] == service.RULE
            and manifest["task_ids"] == [item["task_id"] for item in manifest["tasks"]]
            and positive_ids(manifest["task_ids"]) == manifest["task_ids"]
        )
    except (TypeError, ValueError, KeyError):
        valid = False
    if not valid:
        fail("CLASSIFICATION_BATCH_MANIFEST_INVALID")
    return batch


def visible_items(batch, actor, role):
    return [
        item
        for item in batch.manifest["tasks"]
        if role == "manager"
        or not any(
            entry["assignee_id"] == actor.pk or (entry["source"] or {}).get("submitter_id") == actor.pk
            for entry in item["cohort"]
        )
    ]


def visible_batches(project, actor, role):
    rows = ClassificationBatch.objects.filter(project=project)
    if role == "reviewer":
        # Filter before SQL pagination/count; never expose hidden batch totals.
        if connection.vendor == "postgresql":
            expression = """EXISTS (SELECT 1 FROM jsonb_array_elements(manifest->'tasks') item
                WHERE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(item->'cohort') member
                WHERE (member->>'assignee_id')::bigint = %s OR (member->'source'->>'submitter_id')::bigint = %s))"""
        else:
            expression = """EXISTS (SELECT 1 FROM json_each(manifest, '$.tasks') item
                WHERE NOT EXISTS (SELECT 1 FROM json_each(item.value, '$.cohort') member
                WHERE json_extract(member.value, '$.assignee_id') = %s
                OR json_extract(member.value, '$.source.submitter_id') = %s))"""
        rows = rows.annotate(
            _visible=RawSQL(expression, (actor.pk, actor.pk), output_field=models.BooleanField())
        ).filter(_visible=True)
    return rows.order_by("-pk")


def metadata(batch, actor, role):
    items = visible_items(batch, actor, role)
    if not items:
        raise NotFound()
    data = {
        "id": batch.pk,
        "name": batch.name,
        "created_at": batch.created_at.isoformat(),
        "calculated_at": batch.manifest["calculated_at"],
        "scope_enumerated_at": batch.manifest["scope_enumerated_at"],
        "schema": SCHEMA,
        "rule": batch.manifest["rule"],
        "selection": batch.manifest["selection"],
        "visible_task_count": len(items),
        "visibility": "project" if role == "manager" else "visible_subset",
    }
    data["manifest_hash" if role == "manager" else "projection_hash"] = (
        batch.manifest_hash if role == "manager" else digest({"batch": batch.pk, "tasks": items})
    )
    return data


def _project_items(batch, actor, role, *, task_id=None, lock=False):
    items = visible_items(batch, actor, role)
    if task_id is not None:
        items = [item for item in items if item['task_id'] == task_id]
    task_ids = [item['task_id'] for item in items]
    task_query = Task.objects.filter(pk__in=task_ids).order_by('pk')
    if lock:
        task_query = task_query.select_for_update()
    tasks = dict(task_query.values_list('pk', 'project_id'))
    cohort = [entry for item in items for entry in item['cohort']]
    assignment_query = TaskAssignment.objects.filter(pk__in=[e['assignment_id'] for e in cohort]).order_by('pk')
    if lock:
        assignment_query = assignment_query.select_for_update()
    assignments = {a.pk: a for a in assignment_query}
    source_ids = [e['source']['submission_id'] for e in cohort if e['source']]
    if lock:
        list(
            Submission.objects.filter(pk__in=source_ids)
            .select_for_update()
            .order_by('pk')
            .values_list('pk', flat=True)
        )
    sources = load_sources(source_ids)
    projected = []
    for item in items:
        rows, reason = [], None
        if tasks.get(item['task_id']) != batch.project_id:
            reason = 'source_unavailable'
        for entry in item['cohort']:
            assignment = assignments.get(entry['assignment_id'])
            if (
                assignment is None
                or assignment.project_id != batch.project_id
                or assignment.task_id != item['task_id']
                or assignment.assignee_id != entry['assignee_id']
            ):
                reason = 'source_unavailable'
            pin = entry['source']
            if pin:
                row = sources.get(pin['submission_id'])
                try:
                    valid = row is not None and valid_source(row, batch.project_id) and source_pin(row) == pin
                except (TypeError, ValueError, KeyError):
                    valid = False
                if not valid:
                    reason = 'source_unavailable'
                else:
                    rows.append(row)
        report = (
            service.unavailable(reason or item['selection_reason'])
            if reason or item['selection_reason']
            else service.calculate(rows, batch.project_id)
        )
        if not reason and frozen_result(report) != item['result']:
            reason = 'source_integrity_failed'
            report = service.unavailable(reason)
        projected.append(
            {
                'task_id': item['task_id'],
                'outcome': outcome(report),
                'inputs': [] if reason else [entry['source'] for entry in item['cohort'] if entry['source']],
                **report,
            }
        )
    return projected, sources


def project_items(batch, actor, role):
    return _project_items(batch, actor, role)[0]


def project_item_sources(batch, actor, role, task_id, *, lock=False):
    """Share exact-source checks with arbitration, without fetching unrelated tasks."""
    items, sources = _project_items(batch, actor, role, task_id=task_id, lock=lock)
    if not items:
        raise NotFound()
    return items[0], sources


def counts(items):
    return {
        state: sum(item["outcome"] == state for item in items)
        for state in ("agreement", "disagreement", "unavailable")
    }
