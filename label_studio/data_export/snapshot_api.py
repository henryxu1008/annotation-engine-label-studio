"""Authenticated source evidence APIs; never create a Host version or approval."""

import uuid
from datetime import timedelta

from django.core import signing
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.exceptions import APIException
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response

from . import approved as base
from . import snapshots as service
from .approved_api import ApprovedExportAPI, BoundedRangeReader, PrivateRangeResponse
from .snapshot_models import ReleaseSnapshot as Snapshot


class ReleaseSnapshotAPI(ApprovedExportAPI):
    def snapshot(self):
        return get_object_or_404(
            Snapshot.objects.select_related("release"), pk=self.kwargs["snapshot_id"], release=self.receipt()
        )

    def gate(self, snapshot, *, content=True):
        service.identity(snapshot, self.project)
        service.receipt_gate(snapshot.release)
        if content:
            try:
                manifest = service.verify(snapshot)
                # All immutable payloads, including media, must still be available.
                for asset in service.assets(snapshot, manifest).values():
                    with service.checked_file(asset["path"], asset["sha256"], asset["size_bytes"]):
                        pass
            except APIException as exc:
                if str(exc.detail) in {"SNAPSHOT_INTEGRITY_FAILED", "MANIFEST_INVALID"}:
                    Snapshot.objects.filter(pk=snapshot.pk, state="ready").update(
                        state="quarantined", error_code="SNAPSHOT_INTEGRITY_FAILED"
                    )
                raise
        else:
            manifest = None
        base.audit(self.project, self.principal, self.actor, "snapshot_read", release=snapshot.release)
        return manifest

    def summary(self, snapshot):
        return {
            "snapshot_id": str(snapshot.pk),
            "release_id": str(snapshot.release_id),
            "state": snapshot.state,
            "content_digest": snapshot.content_digest,
            "manifest_sha256": snapshot.manifest_sha256,
            "error_code": snapshot.error_code,
            "created_at": base.stamp(snapshot.created_at),
            "retryable": self.actor.pk == snapshot.actor_id
            and (
                snapshot.state == "dispatch_pending"
                or (snapshot.state == "queued" and snapshot.created_at < timezone.now() - timedelta(minutes=30))
                or (
                    snapshot.state == "running"
                    and snapshot.started_at
                    and snapshot.started_at < timezone.now() - timedelta(minutes=31)
                )
            ),
        }

    def get(self, request, *args, **kwargs):
        action = kwargs.get("action", "list")
        if action == "list":
            release = self.receipt()
            base.creator(release, self.project)
            service.receipt_gate(release)
            rows = Snapshot.objects.filter(release=release).order_by("-created_at", "-id")
            # History is intentionally bounded and exposes an explicit continuation.
            cursor = request.query_params.get("cursor")
            binding = [str(self.project.pk), str(release.pk), self.principal.principal_id]
            if cursor:
                try:
                    value = signing.loads(cursor, salt="release-snapshot-history")
                    if value["binding"] != binding:
                        raise ValueError()
                    from django.db.models import Q

                    rows = rows.filter(
                        Q(created_at__lt=value["at"]) | Q(created_at=value["at"], id__lt=uuid.UUID(value["id"]))
                    )
                except (ValueError, KeyError, TypeError, signing.BadSignature):
                    base.fail("INVALID_CURSOR", 400)
            page = list(rows[:51])
            more, page = len(page) > 50, page[:50]
            base.audit(self.project, self.principal, self.actor, "snapshot_list", release=release)
            return Response(
                {
                    "results": [self.summary(s) for s in page],
                    "next_cursor": signing.dumps(
                        {"binding": binding, "at": base.stamp(page[-1].created_at), "id": str(page[-1].pk)},
                        salt="release-snapshot-history",
                    )
                    if more
                    else None,
                }
            )
        snapshot = self.snapshot()
        if action == "detail":
            manifest = self.gate(snapshot, content=snapshot.state == "ready")
            result = self.summary(snapshot)
            if manifest:
                result["media_count"] = len({p["blob_id"] for p in manifest["media"]})
                result["assets"] = [
                    {k: a[k] for k in ("id", "name", "sha256", "size_bytes")}
                    for a in service.assets(snapshot, manifest).values()
                ]
            return Response(result)
        manifest = self.gate(snapshot)
        if action == "manifest":
            response = HttpResponse(bytes(snapshot.manifest), content_type="application/json")
            response["X-Manifest-SHA256"] = snapshot.manifest_sha256
            response["Content-Disposition"] = f'attachment; filename="release-manifest-{snapshot.pk}.json"'
        elif action == "diff":
            try:
                other_id = uuid.UUID(request.query_params.get("against", ""))
            except (ValueError, TypeError):
                base.fail("INVALID_REQUEST", 400)
            other = get_object_or_404(
                Snapshot.objects.select_related("release"), pk=other_id, release__project=self.project
            )
            other_manifest = self.gate(other)
            result = service.difference(manifest, other_manifest)
            offset = request.query_params.get("offset", "0")
            if not offset.isascii() or not offset.isdecimal() or len(offset) > 8:
                base.fail("INVALID_REQUEST", 400)
            offset = int(offset)
            changes = result.pop("changes")
            result.update(
                changes=changes[offset : offset + 50], next_offset=offset + 50 if offset + 50 < len(changes) else None
            )
            return Response(result)
        elif action == "download":
            asset = service.assets(snapshot, manifest).get(str(kwargs["asset_id"]))
            if asset is None:
                base.fail("NOT_FOUND", 404)
            # The descriptor used for the final checksum is the one streamed to the caller.
            checked = service.checked_file(asset["path"], asset["sha256"], asset["size_bytes"])
            handle = checked.__enter__()
            try:
                etag = f'"{asset["sha256"]}"'
                if request.META.get("HTTP_IF_RANGE") not in (None, etag):
                    request.META.pop("HTTP_RANGE", None)
                if request.META.get("HTTP_RANGE"):
                    try:
                        ranges = BoundedRangeReader(handle).parse_range_header(
                            request.META["HTTP_RANGE"], asset["size_bytes"]
                        )
                    except ValueError:
                        ranges = None
                    if (
                        not ranges
                        or len(ranges) != 1
                        or not (0 <= ranges[0][0] < asset["size_bytes"] and ranges[0][0] < ranges[0][1])
                    ):
                        checked.__exit__(None, None, None)
                        response = HttpResponse(status=416)
                        response["Content-Range"] = f"bytes */{asset['size_bytes']}"
                        response["Cache-Control"] = "private, no-store"
                        return response
                response = PrivateRangeResponse(request, handle, content_type="application/octet-stream")
                response._resource_closers.append(lambda: checked.__exit__(None, None, None))
                if response.status_code == 200:
                    response["Content-Length"] = asset["size_bytes"]
                response["ETag"], response["Accept-Ranges"] = f'"{asset["sha256"]}"', "bytes"
                response["Content-Disposition"] = f'attachment; filename="{asset["name"]}"'
            except Exception:
                checked.__exit__(None, None, None)
                raise
        else:
            base.fail("NOT_FOUND", 404)
        response["Cache-Control"], response["X-Content-Type-Options"] = "private, no-store", "nosniff"
        return response

    def post(self, request, *args, **kwargs):
        action = kwargs.get("action", "list")
        if action == "list":
            snapshot = service.create(
                self.receipt(), self.principal, self.actor, request.headers.get("Idempotency-Key"), self.body()
            )
        elif action == "retry":
            if self.body() != {}:
                base.fail("INVALID_REQUEST", 400)
            snapshot = self.snapshot()
            self.gate(snapshot, content=False)
            snapshot = service.retry(snapshot, self.actor)
        else:
            base.fail("NOT_FOUND", 404)
        return Response(self.summary(snapshot), status=202)
