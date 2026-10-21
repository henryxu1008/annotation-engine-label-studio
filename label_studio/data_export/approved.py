"""Approved immutable exports. No mutable Annotation or Task extraction path."""

import copy
import hashlib
import importlib.metadata
import json
import logging
import os
import re
import sys
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import django_rq
from access_control.authorization import authorization
from access_control.identity import (
    LOCAL_IDENTITY_SOURCE,
    LocalIdentityProvider,
    Principal,
    get_identity_provider,
    resolve_actor,
)
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from label_studio_sdk.converter import Converter
from organizations.models import OrganizationMember
from projects.models import Project, ProjectMember
from rest_framework.exceptions import APIException
from tasks.models import Submission

from .approved_models import (
    ApprovedDatasetRelease as Release,
    ApprovedDatasetReleaseItem as Item,
    ApprovedDatasetReleaseAttempt as Attempt,
    ApprovedDatasetReleaseArtifact as Artifact,
    ApprovedDatasetReleaseAccessEvent as AccessEvent,
)

ACTIVE = ("dispatch_pending", "queued", "running", "failed_retryable")
DEFAULT_LIMITS = dict(
    items=10000,
    request_bytes=2 * 1024**2,
    input_bytes=64 * 1024**2,
    artifact_bytes=256 * 1024**2,
    scratch_bytes=512 * 1024**2,
    timeout=1800,
)
MAPPING = "submission-envelope/v1"
logger = logging.getLogger(__name__)


def fail(reason, status=409):
    error = APIException(reason)
    error.status_code = status
    error.display_context = {"reason": reason}
    raise error


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
            "utf-8"
        )
    except (ValueError, TypeError, UnicodeError):
        fail("SNAPSHOT_INVALID", 422)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def stamp(value):
    return value.astimezone(timezone.get_fixed_timezone(0)).isoformat().replace("+00:00", "Z")


def limits():
    supplied = getattr(settings, "APPROVED_EXPORT_LIMITS", {})
    return {key: min(value, max(1, int(supplied.get(key, value)))) for key, value in DEFAULT_LIMITS.items()}


def policy(project):
    value = getattr(settings, "APPROVED_EXPORT_POLICY", {})
    try:
        namespace = str(uuid.UUID(value["source_namespace"]))
        entry = value["projects"][f"{project.organization_id}:{project.id}"]
        version = entry["version"]
        if entry["mode"] != "fork_approved" or not isinstance(version, str) or not 0 < len(version) <= 128:
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        fail("RELEASE_MODE_FORBIDDEN", 403)
    return {"mode": "fork_approved", "version": version, "source_namespace": namespace}


def manager(principal, actor, project):
    if (
        not actor.is_active
        or actor.pk != principal.local_user_id
        or not project.organization_id
        or project.deleted_at is not None
        or not OrganizationMember.objects.filter(
            user=actor, organization_id=project.organization_id, deleted_at__isnull=True
        ).exists()
        or not authorization.can_manage_project(principal, project)
    ):
        fail("RELEASE_FORBIDDEN", 403)


def request_scope(request, project_id, resolved=None):
    principal, actor = resolved or resolve_actor(request)
    from rest_framework.generics import get_object_or_404

    project = get_object_or_404(Project.objects.for_user(actor), pk=project_id)
    manager(principal, actor, project)
    live_policy = policy(project)
    # A background identity adapter must be available before admitting any job.
    provider = get_identity_provider()
    if not isinstance(provider, LocalIdentityProvider) and not callable(getattr(provider, "resolve_background", None)):
        fail("RELEASE_FORBIDDEN", 403)
    return project, principal, actor, live_policy


def creator(release, project):
    provider = get_identity_provider()
    if isinstance(provider, LocalIdentityProvider):
        actor = get_user_model().objects.filter(pk=release.actor_id_frozen, is_active=True).first()
        if actor is None or release.principal_id != f"{LOCAL_IDENTITY_SOURCE}:{actor.pk}":
            fail("CREATOR_AUTH_REVOKED", 403)
        principal = Principal(release.principal_id, LOCAL_IDENTITY_SOURCE, actor.email, actor.pk)
    elif callable(getattr(provider, "resolve_background", None)):
        principal, actor = provider.resolve_background(release.principal_id)
        actor = get_user_model().objects.get(pk=actor.pk)
        if principal.principal_id != release.principal_id or actor.pk != release.actor_id_frozen:
            fail("CREATOR_AUTH_REVOKED", 403)
    else:
        fail("CREATOR_AUTH_REVOKED", 403)
    manager(principal, actor, project)
    current = policy(project)
    if current != release.frozen_input["access_policy"]:
        fail("POLICY_VERSION_CHANGED")
    if project.organization_id != release.organization_id_frozen:
        fail("CREATOR_AUTH_REVOKED", 403)
    return principal, actor, current


def lock_authority(actor_id, project):
    """Hold local authority through a freeze/publication transaction.

    The caller already holds the project's canonical membership fence. Lock the
    organization membership before the user, matching OrganizationMember's
    soft-delete order; account disablement serializes on the user row. Lock a
    local role row as well, covering direct membership maintenance.
    """
    list(
        OrganizationMember.objects.select_for_update()
        .filter(user_id=actor_id, organization_id=project.organization_id)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    actor = get_user_model().objects.select_for_update().filter(pk=actor_id).first()
    if actor is None:
        fail("RELEASE_FORBIDDEN", 403)
    list(
        ProjectMember.objects.select_for_update()
        .filter(project_id=project.pk, user_id=actor_id)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    return actor


def audit(project, principal, actor, phase, *, release=None, allowed=True, reason=""):
    return AccessEvent.objects.create(
        release=release,
        project_id_frozen=project.pk,
        actor_id_frozen=actor.pk if actor else None,
        principal_id=principal.principal_id if principal else "",
        phase=phase,
        policy_version=policy(project)["version"] if allowed else "",
        allowed=allowed,
        reason=reason,
    )


def runtime(format_name):
    if sys.version_info[:2] != (3, 11):
        fail("RUNTIME_VERSION_CHANGED")
    source = None
    version = None
    if format_name not in ("JSON", "JSONL"):
        distribution = importlib.metadata.distribution("label-studio-sdk")
        version = distribution.version
        try:
            source = json.loads(distribution.read_text("direct_url.json"))
        except (ValueError, TypeError):
            fail("RUNTIME_VERSION_CHANGED")
        if (
            version != "2.0.18"
            or source.get("url")
            != "https://github.com/HumanSignal/label-studio-sdk/archive/2321865c82b384f7a2983d5aed8984b75db4033c.zip"
            or source.get("archive_info", {}).get("hashes", {}).get("sha256")
            != "caab72d2f936f54cce25da5f9f5def4d199b21d9f3b4b94426b38016342fb56f"
        ):
            fail("RUNTIME_VERSION_CHANGED")
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "mapping_version": MAPPING,
        "sdk_version": version,
        "sdk_source": source,
    }


def formats(xml):
    # CoNLL is the first bounded, resource-free adapter. SDK support alone is not acceptance.
    try:
        supported = set(Converter(config=xml, project_dir=None).supported_formats)
    except Exception:
        fail("FORMAT_UNSUPPORTED", 422)
    return ["JSON", "JSONL"] + (["CONLL2003"] if "CONLL2003" in supported else [])


def normalize(body):
    if not isinstance(body, dict) or set(body) - {"items", "output", "filters"}:
        fail("INVALID_REQUEST", 400)
    items, output, filters = body.get("items"), body.get("output"), body.get("filters", {})
    if not isinstance(items, list) or not items:
        fail("EMPTY_SELECTION", 400)
    if len(items) > limits()["items"]:
        fail("BATCH_LIMIT_EXCEEDED", 413)
    for item in items:
        if (
            not isinstance(item, dict)
            or set(item) != {"submission_id", "revision", "result_hash"}
            or any(type(item[k]) is not int or item[k] < 1 for k in ("submission_id", "revision"))
            or not isinstance(item["result_hash"], str)
            or not re.fullmatch("[a-f0-9]{64}", item["result_hash"])
        ):
            fail("INVALID_REQUEST", 400)
    if len({i["submission_id"] for i in items}) != len(items):
        fail("DUPLICATE_SELECTION", 400)
    if not isinstance(output, dict) or set(output) != {"format", "resource_mode"}:
        fail("INVALID_REQUEST", 400)
    if output["resource_mode"] != "references_only":
        fail("RESOURCE_MODE_UNSUPPORTED", 422)
    if output["format"] not in ("JSON", "JSONL", "CONLL2003"):
        fail("FORMAT_UNSUPPORTED", 422)
    normalized_filters(filters)  # strict names/types/date bounds, even if null
    return {
        "items": sorted(copy.deepcopy(items), key=lambda i: i["submission_id"]),
        "output": copy.deepcopy(output),
        "filters": filters,
    }


def normalized_filters(filters):
    from django.utils.dateparse import parse_datetime

    names = {"task_id", "assignment_id", "submitted_from", "submitted_to", "reviewed_from", "reviewed_to"}
    if not isinstance(filters, dict) or set(filters) - names:
        fail("INVALID_REQUEST", 400)
    result = {}
    for key, value in filters.items():
        if value is None:
            continue
        if key.endswith("_id"):
            if type(value) is not int or value < 1:
                fail("INVALID_REQUEST", 400)
            result["assignment__" + ("task_id" if key == "task_id" else "id")] = value
        else:
            try:
                parsed = parse_datetime(value) if isinstance(value, str) else None
            except ValueError:
                fail("INVALID_REQUEST", 400)
            if parsed is None or timezone.is_naive(parsed) or parsed.utcoffset().total_seconds() != 0:
                fail("INVALID_REQUEST", 400)
            field = "submitted_at" if key.startswith("submitted") else "review__created_at"
            result[field + ("__gte" if key.endswith("from") else "__lt")] = parsed
    return result


def source_record(submission, namespace):
    snapshot = submission.result_snapshot
    try:
        review = submission.review
    except Submission.review.RelatedObjectDoesNotExist:
        fail("REVIEW_EVIDENCE_INVALID")
    if submission.status != "approved":
        fail("APPROVAL_REQUIRED")
    if review.decision != "approved" or not review.reviewer_id or not submission.submitted_by_id:
        fail("REVIEW_EVIDENCE_INVALID")
    if review.reviewer_id == submission.submitted_by_id:
        fail("REVIEW_EVIDENCE_INVALID")
    if digest(canonical(snapshot)) != submission.result_hash:
        fail("SNAPSHOT_HASH_MISMATCH")
    try:
        if (
            snapshot["project"]["id"] != submission.assignment.project_id
            or snapshot["task"]["id"] != submission.assignment.task_id
            or submission.assignment.task.project_id != submission.assignment.project_id
            or not isinstance(snapshot["task"]["data"], dict)
            or not isinstance(snapshot["annotation"]["result"], list)
            or not isinstance(snapshot["project"]["label_config"], str)
            or not snapshot["project"]["label_config"]
        ):
            fail("SNAPSHOT_INVALID", 422)
        if any(
            not isinstance(r, dict) or not isinstance(r.get("value"), dict) or not isinstance(r.get("type"), str)
            for r in snapshot["annotation"]["result"]
        ):
            fail("SNAPSHOT_INVALID", 422)
    except (KeyError, TypeError):
        fail("SNAPSHOT_INVALID", 422)
    return {
        "source": {"namespace": namespace, "kind": "submission", "id": submission.pk},
        "project_id": submission.assignment.project_id,
        "task_id": submission.assignment.task_id,
        "assignment_id": submission.assignment_id,
        "revision": submission.revision,
        "result_hash": submission.result_hash,
        "submitted_by": {"local_user_id": submission.submitted_by_id},
        "submitted_at": stamp(submission.submitted_at),
        "review": {
            "id": review.pk,
            "decision": "approved",
            "reviewer": {"local_user_id": review.reviewer_id},
            "decided_at": stamp(review.created_at),
        },
        "result_snapshot": copy.deepcopy(snapshot),
    }


def selected_records(project, normalized, namespace, lock=False):
    requested = normalized["items"]
    qs = Submission.objects.filter(assignment__project=project, pk__in=[i["submission_id"] for i in requested])
    if qs.count() != len(requested):
        fail("NOT_FOUND", 404)
    qs = (
        qs.filter(**normalized_filters(normalized["filters"]))
        .select_related("assignment__task", "review")
        .order_by("id")
    )
    if lock:
        qs = qs.select_for_update(of=("self",))
    rows, assignments, xml, size = [], set(), None, 0
    for submission, wanted in zip(qs, requested, strict=False):
        if submission.id != wanted["submission_id"]:
            fail("INVALID_REQUEST", 400)
        if submission.revision != wanted["revision"] or submission.result_hash != wanted["result_hash"]:
            fail("REVISION_MISMATCH")
        record = source_record(submission, namespace)
        if record["assignment_id"] in assignments:
            fail("AMBIGUOUS_ASSIGNMENT_REVISION", 400)
        assignments.add(record["assignment_id"])
        config = record["result_snapshot"]["project"]["label_config"]
        if xml is not None and xml != config:
            fail("MIXED_LABEL_CONFIG")
        xml = config
        size += len(canonical(record))
        if size > limits()["input_bytes"]:
            fail("BATCH_LIMIT_EXCEEDED", 413)
        rows.append(record)
    if len(rows) != len(requested):
        fail("INVALID_REQUEST", 400)
    if normalized["output"]["format"] not in formats(xml):
        fail("FORMAT_UNSUPPORTED", 422)
    if normalized["output"]["format"] == "CONLL2003":
        validate_conll(rows)
    return rows, xml


def validate_conll(rows):
    # No media/network fallback or representational loss in the first standard adapter.
    from nltk.tokenize import TreebankWordTokenizer

    for row in rows:
        snapshot = row["result_snapshot"]
        if snapshot["annotation"].get("was_cancelled"):
            fail("FORMAT_UNSUPPORTED", 422)
        text_values = list(snapshot["task"]["data"].values())
        if len(text_values) != 1 or not isinstance(text_values[0], str):
            fail("FORMAT_UNSUPPORTED", 422)
        results = snapshot["annotation"]["result"]
        if not results or any(
            r.get("type") != "labels"
            or not isinstance(r.get("value", {}).get("labels"), list)
            or not all(isinstance(r.get(key), str) for key in ("from_name", "to_name"))
            or len(r["value"]["labels"]) != 1
            or type(r["value"].get("start")) is not int
            or type(r["value"].get("end")) is not int
            for r in results
        ):
            fail("FORMAT_UNSUPPORTED", 422)
        text = text_values[0]
        tokens = list(TreebankWordTokenizer().span_tokenize(text))
        starts, ends = {a for a, _ in tokens}, {b for _, b in tokens}
        if len({(r.get("from_name"), r.get("to_name")) for r in results}) != 1:
            fail("FORMAT_UNSUPPORTED", 422)
        last_end = -1
        for value in sorted((r["value"] for r in results), key=lambda v: v.get("start", -1)):
            a, b = value.get("start"), value.get("end")
            if (
                type(a) is not int
                or type(b) is not int
                or a not in starts
                or b not in ends
                or a < last_end
                or a >= b
                or value.get("text") != text[a:b]
                or not isinstance(value["labels"][0], str)
                or not re.fullmatch(r"[^\s]+", value["labels"][0])
            ):
                fail("FORMAT_UNSUPPORTED", 422)
            last_end = b
        # Conservative bound for CoNLL columns; prevents oversized scratch output before conversion.
        if (
            max(len(r["value"]["labels"][0].encode("utf-8")) for r in results) * max(1, len(tokens))
            > limits()["artifact_bytes"]
        ):
            fail("BATCH_LIMIT_EXCEEDED", 413)


def queue_available():
    try:
        queue = api_queue()
        try:
            return bool(queue.connection.ping())
        finally:
            queue.connection.close()
    except Exception:
        return False


def api_queue():
    """Bound HTTP-side Redis waits without shortening the worker's blocking dequeue."""
    from redis import Redis

    name = getattr(settings, "APPROVED_EXPORT_QUEUE", "approved_exports")
    parsed = urlsplit(settings.RQ_QUEUES[name]["URL"])
    if parsed.scheme not in ("redis", "rediss"):
        raise ValueError("Approved export requires a configured Redis URL")
    query = dict(parse_qsl(parsed.query))
    query.update(socket_connect_timeout="3", socket_timeout="3")
    connection = Redis.from_url(urlunsplit(parsed._replace(query=urlencode(query))))
    return django_rq.get_queue(name, connection=connection)


def summary(release, actor=None):
    expired = (
        release.state in ("queued", "running")
        and release.attempts.filter(
            number=release.attempt_number,
            heartbeat__lte=timezone.now() - timedelta(seconds=limits()["timeout"] + 60),
        ).exists()
    )
    return {
        "release_id": str(release.pk),
        "state": release.state,
        "error_code": release.error_code,
        "item_count": len(release.frozen_input["content"]["items"]),
        "input_sha256": release.input_sha256,
        "content_sha256": release.content_sha256,
        "attempt_number": release.attempt_number,
        "retryable": (release.state in ("dispatch_pending", "failed_retryable") or expired)
        and (actor is None or actor.pk == release.actor_id_frozen),
        "created_at": stamp(release.created_at),
        "status_url": f"/api/projects/{release.project_id}/approved-dataset-releases/{release.pk}/",
    }


def freeze(project, principal, actor, body, key):
    normalized = normalize(body)
    if not isinstance(key, str) or not re.fullmatch(r"[\x21-\x7e]{1,128}", key):
        fail("INVALID_REQUEST", 400)
    request_hash = digest(canonical(normalized))
    with transaction.atomic():
        project = Project.objects.select_for_update().get(pk=project.pk)
        actor = lock_authority(actor.pk, project)
        manager(principal, actor, project)
        current = policy(project)
        old = Release.objects.filter(
            project=project,
            organization_id_frozen=project.organization_id,
            actor_id_frozen=actor.pk,
            idempotency_key=key,
        ).first()
        if old:
            creator(old, project)
            if old.request_hash != request_hash:
                fail("IDEMPOTENCY_CONFLICT")
            return old
        if not queue_available():
            fail("QUEUE_UNAVAILABLE", 503)
        active = Release.objects.filter(project=project, state__in=ACTIVE)
        if active.count() >= 2 or active.filter(actor_id_frozen=actor.pk).exists():
            fail("ACTIVE_JOB_LIMIT", 429)
        rows, xml = selected_records(project, normalized, current["source_namespace"], lock=True)
        fingerprints = []
        for row in rows:
            item = {k: v for k, v in row.items() if k not in ("project_id", "result_snapshot")}
            fingerprints.append(dict(item, record_sha256=digest(canonical(row))))
        content = {
            "schema": "fork.approved-export-content/v1",
            "source_namespace": current["source_namespace"],
            "organization_id": project.organization_id,
            "project_id": project.pk,
            "ordering": "submission_id:asc",
            "revision_policy": "explicit_one_per_assignment",
            "label_config": xml,
            "label_config_sha256": digest(xml.encode("utf-8")),
            "native_mapping_version": MAPPING,
            "media_integrity": "unverified_reference",
            "items": fingerprints,
        }
        release_id = uuid.uuid4()
        freeze_event = audit(project, principal, actor, "freeze")
        frozen = {
            "release_id": str(release_id),
            "content": content,
            "content_sha256": digest(canonical(content)),
            "request": normalized,
            "request_sha256": request_hash,
            "created_by": {"principal_id": principal.principal_id, "local_user_id": actor.pk},
            "frozen_at": stamp(freeze_event.created_at),
            "access_policy": current,
            "freeze_authorization": {
                "decision": "allow",
                "phase": "freeze",
                "actor_id": actor.pk,
                "principal_id": principal.principal_id,
                "receipt_id": str(freeze_event.pk),
                "organization_id": project.organization_id,
                "project_id": project.pk,
                "decided_at": stamp(freeze_event.created_at),
                "effective_role": "manager",
                "policy_version": current["version"],
            },
            "snapshot_canonicalization": "python-json-snapshot-v1",
            "record_canonicalization": "python-json-export-v1",
            "runtime": runtime(normalized["output"]["format"]),
        }
        actor.refresh_from_db()
        manager(principal, actor, project)
        if policy(project) != current:
            fail("POLICY_VERSION_CHANGED")
        release = Release.objects.create(
            id=release_id,
            project=project,
            organization_id_frozen=project.organization_id,
            actor_id_frozen=actor.pk,
            principal_id=principal.principal_id,
            idempotency_key=key,
            request_hash=request_hash,
            frozen_input=frozen,
            content_sha256=frozen["content_sha256"],
            input_sha256=digest(canonical(frozen)),
        )
        Item.objects.bulk_create(
            [
                Item(
                    release=release,
                    ordinal=i,
                    submission_id_frozen=r["source"]["id"],
                    assignment_id_frozen=r["assignment_id"],
                    record=r,
                    record_sha256=digest(canonical(r)),
                )
                for i, r in enumerate(rows)
            ]
        )
        AccessEvent.objects.filter(pk=freeze_event.pk).update(release=release)
        transaction.on_commit(lambda: dispatch(release.pk))
    release.refresh_from_db()
    return release


def dispatch(release_id):
    # DB receipt commits before enqueue. Crash/enqueue failures are recoverable via retry.
    with transaction.atomic():
        release = Release.objects.select_for_update().get(pk=release_id)
        if release.state not in ("dispatch_pending", "failed_retryable"):
            return
        number = release.attempt_number + 1
        attempt = Attempt.objects.create(release=release, number=number)
        release.attempt_number, release.state, release.error_code = number, "queued", ""
        release.save(update_fields=["attempt_number", "state", "error_code", "updated_at"])
    try:
        queue = api_queue()
        try:
            queue.enqueue(
                generate,
                str(release_id),
                str(attempt.token),
                job_id=f"approved-export-{attempt.token}",
                job_timeout=limits()["timeout"],
            )
        finally:
            queue.connection.close()
    except Exception:
        Release.objects.filter(pk=release_id, attempt_number=number, state="queued").update(
            state="dispatch_pending", error_code="QUEUE_UNAVAILABLE"
        )
        Attempt.objects.filter(pk=attempt.pk, state="queued").update(state="failed", error_code="QUEUE_UNAVAILABLE")


def integrity(release):
    frozen = release.frozen_input
    if (
        digest(canonical(frozen)) != release.input_sha256
        or digest(canonical(frozen["content"])) != release.content_sha256
    ):
        fail("SNAPSHOT_HASH_MISMATCH")
    ids = list(release.items.values_list("submission_id_frozen", flat=True))
    sources = {
        s.pk: s
        for s in Submission.objects.filter(pk__in=ids, assignment__project_id=release.project_id).select_related(
            "assignment", "review"
        )
    }
    if len(sources) != len(ids) or len(ids) != len(frozen["content"]["items"]):
        fail("SOURCE_UNAVAILABLE")
    for item, fingerprint in zip(
        release.items.all().iterator(chunk_size=100), frozen["content"]["items"], strict=True
    ):
        record = item.record
        if (
            item.record_sha256 != digest(canonical(record))
            or item.record_sha256 != fingerprint["record_sha256"]
            or record["source"]["id"] != item.submission_id_frozen
            or record["assignment_id"] != item.assignment_id_frozen
            or canonical(record)
            != canonical(source_record(sources[item.submission_id_frozen], frozen["content"]["source_namespace"]))
        ):
            fail("SNAPSHOT_HASH_MISMATCH")


def private_root():
    configured = getattr(settings, "APPROVED_EXPORT_ROOT", None)
    if not configured:
        fail("PRIVATE_STORAGE_REQUIRED", 503)
    root = Path(configured).resolve()
    for public in (settings.MEDIA_ROOT, settings.STATIC_ROOT):
        if public and (root == Path(public).resolve() or Path(public).resolve() in root.parents):
            fail("PRIVATE_STORAGE_REQUIRED", 503)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def artifact_path(key):
    root = private_root()
    path = (root / key).resolve()
    if root not in path.parents:
        fail("ARTIFACT_INTEGRITY_FAILED")
    return path


def generate(release_id, token):
    with transaction.atomic():
        release = Release.objects.select_for_update().get(pk=release_id)
        attempt = Attempt.objects.get(release=release, token=token)
        if release.state != "queued" or attempt.number != release.attempt_number or attempt.state != "queued":
            return
        attempt.state, attempt.heartbeat = "running", timezone.now()
        attempt.save(update_fields=["state", "heartbeat"])
        release.state = "running"
        release.save(update_fields=["state", "updated_at"])
    try:
        project = Project.all_objects.get(pk=release.project_id)
        principal, actor, _ = creator(release, project)
        audit(project, principal, actor, "job_start", release=release)
        integrity(release)
        if release.frozen_input["runtime"] != runtime(release.frozen_input["request"]["output"]["format"]):
            fail("RUNTIME_VERSION_CHANGED")
        directory = private_root() / str(release.pk) / str(attempt.token)
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        output = []
        native_files = [(directory / "records.json").open("xb"), (directory / "records.jsonl").open("xb")]
        try:
            native_files[0].write(b"[")
            for i, item in enumerate(release.items.all().iterator(chunk_size=100)):
                blob = canonical(item.record)
                json_size = native_files[0].tell() + len(blob) + (1 if i else 0) + 1
                jsonl_size = native_files[1].tell() + len(blob) + 1
                if (
                    max(json_size, jsonl_size) > limits()["artifact_bytes"]
                    or json_size + jsonl_size > limits()["scratch_bytes"]
                ):
                    fail("BATCH_LIMIT_EXCEEDED", 413)
                native_files[0].write((b"," if i else b"") + blob)
                native_files[1].write(blob + b"\n")
                if i % 100 == 0:
                    Attempt.objects.filter(pk=attempt.pk, token=token).update(heartbeat=timezone.now())
            native_files[0].write(b"]")
            for file in native_files:
                file.flush()
                os.fsync(file.fileno())
        finally:
            for file in native_files:
                file.close()
        for name, fmt in [("records.json", "JSON"), ("records.jsonl", "JSONL")]:
            output.append((directory / name, fmt, {"mapping_version": MAPPING, "converter": None}))
        if release.frozen_input["request"]["output"]["format"] == "CONLL2003":
            output.extend(convert_conll(release, directory))
        if sum(f.stat().st_size for f in directory.rglob("*") if f.is_file()) > limits()["scratch_bytes"]:
            fail("BATCH_LIMIT_EXCEEDED", 413)
        artifacts = []
        for path, fmt, provenance in output:
            size = path.stat().st_size
            if size > limits()["artifact_bytes"]:
                fail("BATCH_LIMIT_EXCEEDED", 413)
            with path.open("r+b") as handle:
                os.fsync(handle.fileno())
                checksum = hashlib.file_digest(handle, "sha256").hexdigest()
            artifacts.append(
                Artifact(
                    id=uuid.uuid4(),
                    release=release,
                    attempt=attempt,
                    name=path.name,
                    format=fmt,
                    storage_key=path.relative_to(private_root()).as_posix(),
                    sha256=checksum,
                    size_bytes=size,
                    record_count=release.items.count(),
                    provenance=provenance,
                )
            )
        if os.name == "posix":
            for parent in (directory, directory.parent, private_root(), private_root().parent):
                descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        with transaction.atomic():
            # Match freeze and the governed member API's project-first ordering.
            project = Project.all_objects.select_for_update().get(pk=release.project_id)
            current = Release.objects.select_for_update().get(pk=release_id)
            if current.attempt_number != attempt.number or current.state != "running":
                return
            lock_authority(current.actor_id_frozen, project)
            principal, actor, _ = creator(current, project)
            integrity(current)
            audit(project, principal, actor, "publish", release=current)
            Artifact.objects.bulk_create(artifacts)
            manifest = {
                "schema": "fork.approved-export-manifest/v1",
                "input": current.frozen_input,
                "input_sha256": current.input_sha256,
                "generated_at": stamp(timezone.now()),
                "artifacts": [artifact_metadata(a) for a in artifacts],
            }
            current.manifest, current.state, current.error_code = canonical(manifest), "ready", ""
            current.manifest_sha256 = digest(bytes(current.manifest))
            current.save(update_fields=["manifest", "manifest_sha256", "state", "error_code", "updated_at"])
            Attempt.objects.filter(pk=attempt.pk).update(state="ready", finished_at=timezone.now())
    except Exception as exc:
        logger.exception("Approved export generation failed: %s", release_id)
        code = str(exc.detail) if isinstance(exc, APIException) else "GENERATION_FAILED"
        auth_error = isinstance(exc, APIException) and exc.status_code == 403
        state = (
            "blocked_auth" if auth_error else ("failed_final" if isinstance(exc, APIException) else "failed_retryable")
        )
        Release.objects.filter(pk=release_id, attempt_number=attempt.number, state="running").update(
            state=state, error_code=code
        )
        Attempt.objects.filter(pk=attempt.pk).update(state=state, error_code=code, finished_at=timezone.now())
        AccessEvent.objects.create(
            release=release,
            project_id_frozen=release.project_id,
            actor_id_frozen=release.actor_id_frozen,
            principal_id=release.principal_id,
            phase="job_failure",
            allowed=False,
            reason=code,
        )


def artifact_metadata(artifact):
    return {
        "id": str(artifact.pk),
        "name": artifact.name,
        "format": artifact.format,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "record_count": artifact.record_count,
        "provenance": artifact.provenance,
    }


def sealed_artifacts(release):
    """Bind the entire delivery graph to the immutable manifest, once per request.

    A checksum on a mutable artifact row is insufficient: metadata, attempt and
    private pointers can drift independently of the sealed manifest after restore
    or storage maintenance. Return the validated objects so the caller never
    reloads a different metadata snapshot before opening the file.
    """
    try:
        blob = bytes(release.manifest or b"")
        manifest = json.loads(blob)
        if (
            digest(blob) != release.manifest_sha256
            or manifest["schema"] != "fork.approved-export-manifest/v1"
            or canonical(manifest["input"]) != canonical(release.frozen_input)
            or digest(canonical(manifest["input"])) != release.input_sha256
            or manifest["input_sha256"] != release.input_sha256
            or manifest["input"]["release_id"] != str(release.pk)
        ):
            raise ValueError()
        artifacts = list(release.artifacts.select_related("attempt").all())
        expected = {"JSON": "records.json", "JSONL": "records.jsonl"}
        if release.frozen_input["request"]["output"]["format"] == "CONLL2003":
            expected["CONLL2003"] = "conll2003.zip"
        if len(artifacts) != len(expected) or {a.format for a in artifacts} != set(expected):
            raise ValueError()
        for artifact in artifacts:
            if (
                artifact.name != expected[artifact.format]
                or artifact.record_count != len(release.frozen_input["content"]["items"])
                or artifact.attempt.release_id != release.pk
                or artifact.attempt.number != release.attempt_number
                or artifact.attempt.state != "ready"
                or artifact.storage_key != f"{release.pk}/{artifact.attempt.token}/{artifact.name}"
            ):
                raise ValueError()
        # Compare canonical bytes, not Python equality (which equates true and 1).
        recorded = manifest["artifacts"]
        if not isinstance(recorded, list) or len(recorded) != len(artifacts):
            raise ValueError()
        actual = sorted((artifact_metadata(a) for a in artifacts), key=lambda a: a["id"])
        if canonical(sorted(recorded, key=lambda a: a["id"])) != canonical(actual):
            raise ValueError()
        return {artifact.pk: artifact for artifact in artifacts}
    except (ValueError, TypeError, KeyError, AttributeError, APIException):
        fail("ARTIFACT_INTEGRITY_FAILED")


def convert_conll(release, directory):
    from label_studio_sdk.converter.utils import create_tokens_and_tags

    records = [i.record for i in release.items.all()]
    validate_conll(records)
    sdk = []
    source_index = []
    converted_bytes = len(b"-DOCSTART- -X- O\n")
    for ordinal, row in enumerate(records):
        snapshot = row["result_snapshot"]
        # Unique SDK task ID prevents loss of same-task, different-assignment samples.
        sid = row["source"]["id"]
        sdk.append(
            {
                "id": sid,
                "data": snapshot["task"]["data"],
                "meta": snapshot["task"]["meta"],
                "annotations": [dict(snapshot["annotation"], completed_by=row["submitted_by"]["local_user_id"])],
            }
        )
        source_index.append(
            {
                "sdk_task_id": sid,
                "source": row["source"],
                "revision": row["revision"],
                "result_hash": row["result_hash"],
                "record_sha256": digest(canonical(row)),
                "sample_ordinal": ordinal,
            }
        )
        tokens, tags = create_tokens_and_tags(
            next(iter(snapshot["task"]["data"].values())), [r["value"] for r in snapshot["annotation"]["result"]]
        )
        converted_bytes += 1 + sum(
            len(f"{token} -X- _ {tag}\n".encode("utf-8")) for token, tag in zip(tokens, tags, strict=True)
        )
    sdk_input = canonical(sdk)
    index_bytes = canonical(source_index)
    existing_bytes = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
    # Bound both uncompressed files and worst-case ZIP output, before invoking SDK writes.
    scratch_bound = existing_bytes + len(sdk_input) + 2 * (converted_bytes + len(index_bytes)) + 1024**2
    if converted_bytes > limits()["artifact_bytes"] or scratch_bound > limits()["scratch_bytes"]:
        fail("BATCH_LIMIT_EXCEEDED", 413)
    input_file = directory / "sdk-input.json"
    input_file.write_bytes(sdk_input)
    converted = directory / "converted"
    converted.mkdir(mode=0o700)
    converter = Converter(
        config=release.frozen_input["content"]["label_config"], project_dir=None, download_resources=False
    )
    converter.convert(str(input_file), str(converted), "CONLL2003", is_dir=False)
    files = [p for p in converted.rglob("*") if p.is_file()]
    if not files or any(p.is_symlink() for p in files):
        fail("FORMAT_UNSUPPORTED", 422)
    with (converted / "result.conll").open(encoding="utf-8") as corpus:
        if corpus.readline() != "-DOCSTART- -X- O\n":
            fail("FORMAT_UNSUPPORTED", 422)
        for row in records:
            snapshot = row["result_snapshot"]
            tokens, tags = create_tokens_and_tags(
                next(iter(snapshot["task"]["data"].values())), [r["value"] for r in snapshot["annotation"]["result"]]
            )
            for token, tag in zip(tokens, tags, strict=True):
                if corpus.readline() != f"{token} -X- _ {tag}\n":
                    fail("FORMAT_UNSUPPORTED", 422)
            if corpus.readline() != "\n":
                fail("FORMAT_UNSUPPORTED", 422)
        if corpus.read(1):
            fail("FORMAT_UNSUPPORTED", 422)
    (converted / "source-index.json").write_bytes(index_bytes)
    members = []
    for path in sorted(converted.rglob("*")):
        if path.is_file():
            with path.open("rb") as handle:
                checksum = hashlib.file_digest(handle, "sha256").hexdigest()
            members.append(
                {"name": path.relative_to(converted).as_posix(), "sha256": checksum, "size_bytes": path.stat().st_size}
            )
    zip_path = directory / "conll2003.zip"
    with zipfile.ZipFile(zip_path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(converted.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(converted).as_posix())
    return [(zip_path, "CONLL2003", dict(runtime("CONLL2003"), members=members))]
