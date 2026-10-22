"""ReleaseManifest v1: bounded capture, immutable evidence, no Host side effects."""

import hashlib
import json
import os
import re
import stat
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import rfc8785
from data_import.models import FileUpload
from defusedxml import ElementTree
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from jsonschema import ValidationError
from rest_framework.exceptions import APIException

from . import approved as base
from .approved_models import ApprovedDatasetReleaseAccessEvent as AccessEvent
from .release_manifest import validator
from .snapshot_models import ReleaseSnapshot as Snapshot

SCHEMA = "ReleaseManifest/v1"
MEDIA_LIMIT = 64 * 1024**2
TOTAL_MEDIA_LIMIT = 256 * 1024**2
MEDIA_TAGS = {"Image", "Audio", "AudioPlus", "Video", "Pdf", "HyperText", "TimeSeries"}
ACTIVE = ("dispatch_pending", "queued", "running")


def canonical(value):
    try:
        return rfc8785.dumps(value)
    except (ValueError, TypeError, OverflowError):
        base.fail("MANIFEST_INVALID", 422)


def identity(snapshot, project):
    """Revalidate both snapshot creator and the #79 receipt's creator."""
    principal, actor, _ = base.creator(
        SimpleNamespace(
            actor_id_frozen=snapshot.actor_id,
            principal_id=snapshot.principal_id,
            frozen_input={"access_policy": snapshot.policy},
            organization_id_frozen=snapshot.release.organization_id_frozen,
        ),
        project,
    )
    base.creator(snapshot.release, project)
    return principal, actor


def receipt_gate(release):
    base.integrity(release)
    if release.state != "ready":
        base.fail("SOURCE_NOT_READY")
    return base.sealed_artifacts(release)


@contextmanager
def checked_file(path, expected_hash, size):
    """Hash and serve the same descriptor; never follow a substituted symlink."""
    try:
        if path.absolute() != path.resolve(strict=True):
            base.fail("SNAPSHOT_INTEGRITY_FAILED")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != size:
                base.fail("SNAPSHOT_INTEGRITY_FAILED")
            if hashlib.file_digest(handle, "sha256").hexdigest() != expected_hash:
                base.fail("SNAPSHOT_INTEGRITY_FAILED")
            after = os.fstat(handle.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                base.fail("SNAPSHOT_INTEGRITY_FAILED")
            handle.seek(0)
            yield handle
    except OSError:
        base.fail("SNAPSHOT_INTEGRITY_FAILED")


def directory(snapshot):
    return base.private_root() / "snapshots" / str(snapshot.pk) / str(snapshot.token)


def media_fields(xml):
    try:
        tree = ElementTree.fromstring(xml)
    except Exception:
        base.fail("MEDIA_SCHEMA_UNSUPPORTED", 422)
    fields = set()
    for node in tree.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "Text" and node.attrib.get("valueType", "text") != "text":
            base.fail("MEDIA_SCHEMA_UNSUPPORTED", 422)
        if tag in MEDIA_TAGS:
            # HTML, time series and multi-resource transforms require separate resolvers.
            if tag not in {"Image", "Audio", "AudioPlus", "Video", "Pdf"}:
                base.fail("MEDIA_SCHEMA_UNSUPPORTED", 422)
            value = node.attrib.get("value", "")
            if not re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", value):
                base.fail("MEDIA_SCHEMA_UNSUPPORTED", 422)
            fields.add(value[1:])
        elif node.attrib.get("value", "").startswith("$") and tag != "Text":
            base.fail("MEDIA_SCHEMA_UNSUPPORTED", 422)
    return sorted(fields)


def upload_for(reference, project):
    if not isinstance(reference, str):
        base.fail("MEDIA_SOURCE_UNSUPPORTED", 422)
    parsed = urlsplit(reference)
    # Only native upload URLs; no arbitrary HTTP, LocalFiles, query credentials or traversal.
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        base.fail("MEDIA_SOURCE_UNSUPPORTED", 422)
    prefix = "/data/upload/"
    decoded = unquote(parsed.path)
    if not decoded.startswith(prefix) or any(p in ("", ".", "..") for p in decoded[len(prefix) :].split("/")):
        base.fail("MEDIA_SOURCE_UNSUPPORTED", 422)
    name = "upload/" + decoded[len(prefix) :]
    matches = list(FileUpload.objects.filter(project=project, file=name)[:2])
    if len(matches) != 1:
        base.fail("MEDIA_NOT_FOUND", 422)
    upload = matches[0]
    try:
        root = Path(settings.MEDIA_ROOT).resolve()
        path = Path(upload.file.path)
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root) or path.is_symlink() or base.private_root() in resolved.parents:
            base.fail("MEDIA_SOURCE_UNSUPPORTED", 422)
        # Every path component must be literal, including parent directories.
        if path.absolute() != resolved:
            base.fail("MEDIA_SOURCE_UNSUPPORTED", 422)
        return upload, resolved
    except (OSError, ValueError, NotImplementedError):
        base.fail("MEDIA_NOT_FOUND", 422)


def pin_media(snapshot, records, target):
    fields = media_fields(snapshot.release.frozen_input["content"]["label_config"])
    pins, by_upload, total = [], {}, 0
    for record in records:
        for field in fields:
            if len(pins) >= 10000:
                base.fail("MEDIA_LIMIT_EXCEEDED", 413)
            reference = record["result_snapshot"]["task"]["data"].get(field)
            upload, path = upload_for(reference, snapshot.release.project)
            if upload.pk not in by_upload:
                blob_id = str(uuid.uuid4())
                dest = target / blob_id
                try:
                    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(fd, "rb") as source, dest.open("xb") as output:
                        before = os.fstat(source.fileno())
                        if not stat.S_ISREG(before.st_mode) or before.st_size > MEDIA_LIMIT:
                            base.fail("MEDIA_LIMIT_EXCEEDED", 413)
                        total += before.st_size
                        if total > TOTAL_MEDIA_LIMIT:
                            base.fail("MEDIA_LIMIT_EXCEEDED", 413)
                        checksum, size = hashlib.sha256(), 0
                        for chunk in iter(lambda: source.read(1024**2), b""):
                            size += len(chunk)
                            if size > before.st_size:
                                base.fail("MEDIA_CHANGED")
                            checksum.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                        after = os.fstat(source.fileno())
                        source.seek(0)
                        if (
                            (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                            or size != before.st_size
                            or hashlib.file_digest(source, "sha256").hexdigest() != checksum.hexdigest()
                            or path.stat().st_ino != before.st_ino
                        ):
                            base.fail("MEDIA_CHANGED")
                    by_upload[upload.pk] = {
                        "blob_id": blob_id,
                        "sha256": checksum.hexdigest(),
                        "size_bytes": str(size),
                        "upload_id": str(upload.pk),
                    }
                except OSError:
                    base.fail("MEDIA_NOT_FOUND", 422)
            pins.append(
                {
                    **by_upload[upload.pk],
                    "submission_id": str(record["source"]["id"]),
                    "field": field,
                    "reference": reference,
                }
            )
    return pins


def content(release, records, media):
    frozen = release.frozen_input
    # All external IDs/sizes are decimal strings; legacy numeric snapshots stay opaque bytes.
    items = []
    media_by_source = defaultdict(list)
    for pin in media:
        media_by_source[pin["submission_id"]].append(
            {k: pin[k] for k in ("field", "reference", "sha256", "size_bytes", "upload_id")}
        )
    for row in records:
        item = {
            "source": {
                "namespace": row["source"]["namespace"],
                "kind": "IMMUTABLE_SUBMISSION",
                "id": str(row["source"]["id"]),
            },
            "project_id": str(row["project_id"]),
            "task_id": str(row["task_id"]),
            "assignment_id": str(row["assignment_id"]),
            "revision": str(row["revision"]),
            "result_hash": row["result_hash"],
            "record_sha256": base.digest(base.canonical(row)),
            "media": media_by_source[str(row["source"]["id"])],
        }
        items.append(item)
    return {
        "schema": "release-content/v1",
        "source_namespace": frozen["content"]["source_namespace"],
        "organization_id": str(release.organization_id_frozen),
        "project_id": str(release.project_id),
        "ordering": "submission_id:numeric:asc",
        "revision_policy": "explicit_one_per_assignment",
        "label_config_sha256": frozen["content"]["label_config_sha256"],
        "schema_sha256": frozen["content"]["label_config_sha256"],
        "schema_profile": "label-studio-exact-xml/v1",
        "record_canonicalization": frozen["record_canonicalization"],
        "snapshot_canonicalization": frozen["snapshot_canonicalization"],
        "native_mapping_version": frozen["content"]["native_mapping_version"],
        "runtime": frozen["runtime"],
        "output": frozen["request"]["output"],
        "filters": normalized_filters(frozen["request"]["filters"]),
        "media_profile": "managed-upload-capture/v1" if media else "no-media/v1",
        "media_freeze_semantics": "snapshot_capture_not_submission_time",
        "items": items,
    }


def normalized_filters(filters):
    from django.utils.dateparse import parse_datetime

    return {
        k: str(v) if k.endswith("_id") else base.stamp(parse_datetime(v))
        for k, v in sorted(filters.items())
        if v is not None
    }


def inventory(artifacts):
    return sorted((base.artifact_metadata(a) for a in artifacts.values()), key=lambda a: (a["name"], a["id"]))


def create(release, principal, actor, key, body):
    if (
        body != {}
        or not isinstance(body, dict)
        or not isinstance(key, str)
        or not re.fullmatch(r"[\x21-\x7e]{1,128}", key)
    ):
        base.fail("INVALID_REQUEST", 400)
    with transaction.atomic():
        project = type(release.project).all_objects.select_for_update().get(pk=release.project_id)
        for actor_id in sorted({actor.pk, release.actor_id_frozen}):
            base.lock_authority(actor_id, project)
        base.manager(principal, actor, project)
        base.creator(release, project)
        receipt_gate(release)
        old = Snapshot.objects.filter(project=project, actor_id=actor.pk, idempotency_key=key).first()
        if old:
            if old.release_id != release.pk:
                base.fail("IDEMPOTENCY_CONFLICT")
            identity(old, project)
            return old
        if Snapshot.objects.filter(release__project=project, state__in=ACTIVE).count() >= 2:
            base.fail("ACTIVE_JOB_LIMIT", 429)
        snapshot = Snapshot.objects.create(
            project=project,
            release=release,
            actor_id=actor.pk,
            principal_id=principal.principal_id,
            idempotency_key=key,
            policy=base.policy(project),
        )
        base.audit(project, principal, actor, "snapshot_create", release=release)
        transaction.on_commit(lambda: dispatch(snapshot.pk))
        return snapshot


def dispatch(snapshot_id):
    from .approved import api_queue

    try:
        with transaction.atomic():
            snapshot = Snapshot.objects.select_for_update().get(pk=snapshot_id)
            if snapshot.state != "dispatch_pending":
                return
            identity(snapshot, snapshot.release.project)
            token = str(snapshot.token)
            api_queue().enqueue(generate, str(snapshot.pk), token, job_timeout=1800, result_ttl=0)
            snapshot.state = "queued"
            snapshot.save(update_fields=["state"])
    except Exception:
        # DB-first: retrying dispatch never reselects source content.
        base.logger.exception("Snapshot dispatch pending: %s", snapshot_id)


def retry(snapshot, actor):
    with transaction.atomic():
        current = Snapshot.objects.select_for_update().get(pk=snapshot.pk)
        identity(current, current.release.project)
        if actor.pk != current.actor_id:
            base.fail("RELEASE_FORBIDDEN", 403)
        if current.state == "dispatch_pending":
            transaction.on_commit(lambda: dispatch(current.pk))
        elif current.state == "queued" and current.created_at < timezone.now() - timedelta(minutes=30):
            current.state, current.token = "dispatch_pending", uuid.uuid4()
            current.save(update_fields=["state", "token"])
            transaction.on_commit(lambda: dispatch(current.pk))
        elif current.state == "running" and current.started_at < timezone.now() - timedelta(minutes=31):
            # Fence a crashed/timed-out worker without recapturing changed media.
            current.state, current.error_code = "failed", "CAPTURE_INTERRUPTED"
            current.save(update_fields=["state", "error_code"])
        elif current.state != "ready":
            base.fail("SNAPSHOT_RETRY_UNSAFE")
    return current


def generate(snapshot_id, token):
    with transaction.atomic():
        current = Snapshot.objects.select_for_update().get(pk=snapshot_id)
        if str(current.token) != token or current.state != "queued":
            return
        current.state, current.started_at = "running", timezone.now()
        current.save(update_fields=["state", "started_at"])
    try:
        principal, actor = identity(current, current.release.project)
        artifacts = receipt_gate(current.release)
        # Validate actual native/converter bytes before certifying the captured inventory.
        for artifact in artifacts.values():
            with checked_file(base.artifact_path(artifact.storage_key), artifact.sha256, artifact.size_bytes):
                pass
        records = [item.record for item in current.release.items.order_by("ordinal")]
        target = directory(current)
        target.mkdir(parents=True, exist_ok=False, mode=0o700)
        media = pin_media(current, records, target)
        captured = content(current.release, records, media)
        manifest = {
            "schema": SCHEMA,
            "canonicalization": "RFC8785",
            "snapshot_id": str(current.pk),
            "origin_mode": "fork_approved",
            "content": captured,
            "content_digest": base.digest(canonical(captured)),
            "receipt": {
                "release_id": str(current.release_id),
                "manifest_sha256": current.release.manifest_sha256,
                "input_sha256": current.release.input_sha256,
            },
            "artifacts": inventory(artifacts),
            "media": media,
            "generated_at": base.stamp(timezone.now()),
            "created_by": {"principal_id": current.principal_id, "local_user_id": str(current.actor_id)},
            "access_policy": current.policy,
        }
        for pin in {p["blob_id"]: p for p in media}.values():
            with checked_file(target / pin["blob_id"], pin["sha256"], int(pin["size_bytes"])):
                pass
        if os.name == "posix":
            for parent in (
                target,
                target.parent,
                target.parent.parent,
                base.private_root(),
                base.private_root().parent,
            ):
                descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        with transaction.atomic():
            project = type(current.release.project).all_objects.select_for_update().get(pk=current.release.project_id)
            for actor_id in sorted({current.actor_id, current.release.actor_id_frozen}):
                base.lock_authority(actor_id, project)
            current = Snapshot.objects.select_for_update().get(pk=snapshot_id)
            if current.state != "running" or str(current.token) != token:
                return
            principal, actor = identity(current, project)
            receipt_gate(current.release)
            event = base.audit(project, principal, actor, "snapshot_finalize", release=current.release)
            manifest["capture_authorization"] = {
                "effective_role": "manager",
                "decision": "allow",
                "phase": "snapshot_finalize",
                "receipt_id": str(event.pk),
                "decided_at": base.stamp(event.created_at),
            }
            validator().validate(manifest)
            blob = canonical(manifest)
            current.manifest, current.manifest_sha256 = blob, base.digest(blob)
            current.content_digest, current.state = manifest["content_digest"], "ready"
            current.save(update_fields=["manifest", "manifest_sha256", "content_digest", "state"])
    except Exception as exc:
        base.logger.exception("Snapshot capture failed: %s", snapshot_id)
        code = str(exc.detail) if isinstance(exc, APIException) else "SNAPSHOT_GENERATION_FAILED"
        Snapshot.objects.filter(pk=snapshot_id, token=token, state="running").update(state="failed", error_code=code)
        # Captured files are retained for diagnosis. No broad cleanup or automatic recapture.


def verify(snapshot):
    if snapshot.state != "ready":
        base.fail("SNAPSHOT_NOT_READY")
    try:
        blob = bytes(snapshot.manifest)
        manifest = json.loads(blob)
        validator().validate(manifest)
        if (
            manifest["schema"] != SCHEMA
            or canonical(manifest) != blob
            or base.digest(blob) != snapshot.manifest_sha256
            or base.digest(canonical(manifest["content"])) != snapshot.content_digest
            or manifest["content_digest"] != snapshot.content_digest
            or manifest["snapshot_id"] != str(snapshot.pk)
            or manifest["origin_mode"] != "fork_approved"
            or manifest["receipt"]["release_id"] != str(snapshot.release_id)
            or manifest["receipt"]["manifest_sha256"] != snapshot.release.manifest_sha256
            or manifest["receipt"]["input_sha256"] != snapshot.release.input_sha256
            or manifest["access_policy"] != snapshot.policy
            or manifest["created_by"]
            != {"principal_id": snapshot.principal_id, "local_user_id": str(snapshot.actor_id)}
        ):
            base.fail("SNAPSHOT_INTEGRITY_FAILED")
        records = [item.record for item in snapshot.release.items.order_by("ordinal")]
        fields = media_fields(snapshot.release.frozen_input["content"]["label_config"])
        expected_media = sorted(
            (str(row["source"]["id"]), field, row["result_snapshot"]["task"]["data"].get(field))
            for row in records
            for field in fields
        )
        actual_media = sorted((pin["submission_id"], pin["field"], pin["reference"]) for pin in manifest["media"])
        if expected_media != actual_media:
            base.fail("SNAPSHOT_INTEGRITY_FAILED")
        blobs = {}
        for pin in manifest["media"]:
            metadata = (pin["sha256"], pin["size_bytes"], pin["upload_id"])
            if pin["blob_id"] in blobs and blobs[pin["blob_id"]] != metadata:
                base.fail("SNAPSHOT_INTEGRITY_FAILED")
            blobs[pin["blob_id"]] = metadata
        if content(snapshot.release, records, manifest["media"]) != manifest["content"]:
            base.fail("SNAPSHOT_INTEGRITY_FAILED")
        authorization = manifest["capture_authorization"]
        event = AccessEvent.objects.filter(
            pk=authorization["receipt_id"],
            release=snapshot.release,
            phase="snapshot_finalize",
            allowed=True,
            actor_id_frozen=snapshot.actor_id,
            principal_id=snapshot.principal_id,
            policy_version=snapshot.policy["version"],
        ).first()
        if event is None or base.stamp(event.created_at) != authorization["decided_at"]:
            base.fail("SNAPSHOT_INTEGRITY_FAILED")
        artifacts = receipt_gate(snapshot.release)
        if manifest["artifacts"] != inventory(artifacts):
            base.fail("SNAPSHOT_INTEGRITY_FAILED")
        for pin in manifest["media"]:
            if str(uuid.UUID(pin["blob_id"])) != pin["blob_id"]:
                base.fail("SNAPSHOT_INTEGRITY_FAILED")
        return manifest
    except (KeyError, TypeError, ValueError, ValidationError):
        base.fail("SNAPSHOT_INTEGRITY_FAILED")


def assets(snapshot, manifest):
    sealed = {str(a.pk): a for a in base.sealed_artifacts(snapshot.release).values()}
    result = {a["id"]: {**a, "path": base.artifact_path(sealed[a["id"]].storage_key)} for a in manifest["artifacts"]}
    for pin in manifest["media"]:
        result[pin["blob_id"]] = {
            "id": pin["blob_id"],
            "name": f"media-{pin['blob_id']}.bin",
            "sha256": pin["sha256"],
            "size_bytes": int(pin["size_bytes"]),
            "path": directory(snapshot) / pin["blob_id"],
        }
    return result


def difference(left, right):
    a, b = left["content"], right["content"]
    if (a["source_namespace"], a["organization_id"], a["project_id"]) != (
        b["source_namespace"],
        b["organization_id"],
        b["project_id"],
    ):
        base.fail("SNAPSHOTS_NOT_COMPARABLE")
    by_source = {}
    for item in a["items"] + b["items"]:
        key = item["source"]["id"]
        if key in by_source and by_source[key]["record_sha256"] != item["record_sha256"]:
            base.fail("SOURCE_IDENTITY_CONFLICT")
        by_source[key] = item
    before = {i["assignment_id"]: i for i in a["items"]}
    after = {i["assignment_id"]: i for i in b["items"]}
    schema_changed = a["label_config_sha256"] != b["label_config_sha256"]
    changes = []
    for key in sorted(before.keys() | after.keys(), key=int):
        old, new = before.get(key), after.get(key)
        kind = (
            "added"
            if old is None
            else "removed"
            if new is None
            else "revised"
            if old != new or schema_changed
            else "unchanged"
        )
        if kind != "unchanged":
            changes.append({"kind": kind, "assignment_id": key, "before": old, "after": new})
    return {
        "schema": "release-diff/v1",
        "schema_changed": schema_changed,
        "counts": {kind: sum(c["kind"] == kind for c in changes) for kind in ("added", "removed", "revised")},
        "changes": changes,
    }
