"""Project-scoped batch endpoints. Every route, including HEAD/Range, authorizes."""

import hashlib
import os
from datetime import timedelta

from django.core import signing
from django.db import transaction
from django.http import HttpResponse, FileResponse
from django.utils import timezone
from ranged_fileresponse import RangedFileResponse, RangedFileReader
from rest_framework.generics import GenericAPIView, get_object_or_404
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from core.permissions import all_permissions

from . import approved as service
from .approved_models import ApprovedDatasetRelease as Release, ApprovedDatasetReleaseAttempt as Attempt


class BoundedRangeReader(RangedFileReader):
    """Retain upstream range parsing/iteration, without its whole-file size read."""

    def __init__(self, file):
        self.f = file
        self.size = os.fstat(file.fileno()).st_size
        self.block_size = RangedFileReader.block_size
        self.start, self.stop = 0, float("inf")


class PrivateRangeResponse(RangedFileResponse):
    def __init__(self, request, file, **kwargs):
        self.ranged_file = BoundedRangeReader(file)
        FileResponse.__init__(self, self.ranged_file, **kwargs)
        self._resource_closers.append(file.close)
        if request.META.get("HTTP_RANGE"):
            self.add_range_headers(request.META["HTTP_RANGE"])


class ApprovedExportAPI(GenericAPIView):
    permission_required = all_permissions.projects_change

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.scope()

    def scope(self):
        self.principal, self.actor = service.resolve_actor(self.request)
        result = service.request_scope(self.request, self.kwargs["pk"], (self.principal, self.actor))
        self.project, self.principal, self.actor, self.policy = result
        return result

    def handle_exception(self, exc):
        # Deny audit is best-effort; allow audit failures always prevent delivery.
        from projects.models import Project
        from .approved_models import ApprovedDatasetReleaseAccessEvent

        try:
            project = Project.objects.filter(pk=self.kwargs.get("pk")).first()
            if project:
                actor = getattr(self, "actor", None)
                principal = getattr(self, "principal", None)
                ApprovedDatasetReleaseAccessEvent.objects.create(
                    project_id_frozen=project.pk,
                    actor_id_frozen=actor.pk if actor else None,
                    principal_id=principal.principal_id if principal else "",
                    phase="request_denied",
                    allowed=False,
                    reason=getattr(exc, "display_context", {}).get("reason", "REQUEST_DENIED"),
                )
        except Exception:
            pass
        return super().handle_exception(exc)

    def receipt(self):
        release = get_object_or_404(
            Release.objects.filter(project=self.project, organization_id_frozen=self.project.organization_id),
            pk=self.kwargs["release_id"],
        )
        if release.frozen_input["access_policy"] != self.policy:
            service.fail("POLICY_VERSION_CHANGED")
        return release

    def content_gate(self, release):
        service.creator(release, self.project)
        service.integrity(release)
        artifacts = self.sealed_artifacts(release) if release.state == "ready" else {}
        service.audit(self.project, self.principal, self.actor, "content_read", release=release)
        return artifacts

    def sealed_artifacts(self, release):
        try:
            return service.sealed_artifacts(release)
        except APIException:
            self.quarantine(release)

    def quarantine(self, release):
        Release.objects.filter(pk=release.pk, state="ready").update(
            state="failed_final", error_code="ARTIFACT_INTEGRITY_FAILED"
        )
        service.fail("ARTIFACT_INTEGRITY_FAILED")

    def body(self):
        try:
            if int(self.request.META.get("CONTENT_LENGTH") or 0) > service.limits()["request_bytes"]:
                service.fail("BATCH_LIMIT_EXCEEDED", 413)
        except ValueError:
            service.fail("INVALID_REQUEST", 400)
        if len(self.request.body) > service.limits()["request_bytes"]:
            service.fail("BATCH_LIMIT_EXCEEDED", 413)
        return self.request.data

    def page(self, queryset, binding):
        raw_size = self.request.query_params.get("page_size", "50")
        try:
            size = int(raw_size)
            if not 1 <= size <= 100:
                raise ValueError()
        except (TypeError, ValueError):
            service.fail("INVALID_REQUEST", 400)
        binding = dict(binding, project=self.project.pk, principal=self.principal.principal_id)
        cursor = self.request.query_params.get("cursor")
        last = None
        if cursor:
            try:
                data = signing.loads(cursor, salt="approved-export-page-v1", max_age=900)
                if data["binding"] != binding:
                    raise ValueError()
                last = data["last"]
            except (signing.BadSignature, KeyError, ValueError, TypeError):
                service.fail("INVALID_REQUEST", 400)
        if last is not None:
            queryset = queryset.filter(pk__gt=last)
        records = list(queryset.order_by("pk")[: size + 1])
        next_cursor = None
        if len(records) > size:
            records = records[:size]
            next_cursor = signing.dumps(
                {"binding": binding, "last": str(records[-1].pk)}, salt="approved-export-page-v1"
            )
        return records, next_cursor

    def get(self, request, *args, **kwargs):
        action = kwargs.get("action", "list")
        if action in ("preview", "retry"):
            service.fail("METHOD_NOT_ALLOWED", 405)
        if action == "capabilities":
            offered = ["JSON", "JSONL"]
            if request.query_params.get("submission_id"):
                source = get_object_or_404(
                    service.Submission.objects.select_related("assignment__task", "review").filter(
                        assignment__project=self.project
                    ),
                    pk=request.query_params["submission_id"],
                )
                record = service.source_record(source, self.policy["source_namespace"])
                offered = service.formats(record["result_snapshot"]["project"]["label_config"])
            return Response({"mode": "fork_approved", "formats": offered, "limits": service.limits()})
        if action == "candidates":
            filters = {k: v for k, v in request.query_params.items() if k not in ("cursor", "page_size")}
            for key in ("task_id", "assignment_id"):
                if key in filters:
                    try:
                        filters[key] = int(filters[key])
                    except ValueError:
                        service.fail("INVALID_REQUEST", 400)
            qs = service.Submission.objects.filter(
                assignment__project=self.project,
                status="approved",
                review__decision="approved",
                review__reviewer__isnull=False,
                submitted_by__isnull=False,
                **service.normalized_filters(filters),
            ).select_related("assignment__task", "review")
            rows, cursor = self.page(qs, {"kind": "candidates", "filters": filters})
            result = []
            for source in rows:
                record = service.source_record(source, self.policy["source_namespace"])
                result.append(
                    dict(
                        submission_id=source.pk,
                        revision=source.revision,
                        result_hash=source.result_hash,
                        task_id=record["task_id"],
                        assignment_id=record["assignment_id"],
                        submitted_at=record["submitted_at"],
                        review=record["review"],
                        label_config_sha256=service.digest(
                            record["result_snapshot"]["project"]["label_config"].encode("utf-8")
                        ),
                    )
                )
            service.audit(self.project, self.principal, self.actor, "preview")
            return Response({"results": result, "next_cursor": cursor})
        if action == "list":
            rows, cursor = self.page(
                Release.objects.filter(project=self.project, organization_id_frozen=self.project.organization_id),
                {"kind": "releases"},
            )
            return Response({"results": [service.summary(row, self.actor) for row in rows], "next_cursor": cursor})
        release = self.receipt()
        if action == "detail":
            value = service.summary(release, self.actor)
            # Links convey no authority; endpoint rechecks creator, source and private file.
            value["artifacts"] = (
                [
                    dict(
                        service.artifact_metadata(a),
                        download_url=f'{value["status_url"]}artifacts/{a.pk}/download/',
                    )
                    for a in self.sealed_artifacts(release).values()
                ]
                if release.state == "ready"
                else []
            )
            return Response(value)
        artifacts = self.content_gate(release)
        if action == "items":
            rows, cursor = self.page(release.items.all(), {"kind": "items", "release": str(release.pk)})
            return Response({"results": [i.record for i in rows], "next_cursor": cursor})
        if release.state != "ready":
            service.fail("RELEASE_NOT_READY")
        if action == "manifest":
            blob = bytes(release.manifest)
            response = HttpResponse(blob, content_type="application/json")
            response["X-Manifest-SHA256"] = service.digest(blob)
            response["Cache-Control"] = "private, no-store"
            return response
        if action == "download":
            artifact = artifacts.get(kwargs["artifact_id"])
            if artifact is None:
                service.fail("NOT_FOUND", 404)
            try:
                file = service.artifact_path(artifact.storage_key).open("rb")
            except OSError:
                self.quarantine(release)
            if file.seek(0, 2) != artifact.size_bytes:
                file.close()
                self.quarantine(release)
            file.seek(0)
            if hashlib.file_digest(file, "sha256").hexdigest() != artifact.sha256:
                file.close()
                self.quarantine(release)
            file.seek(0)
            etag = f'"{artifact.sha256}"'
            # RangedFileResponse handles one range. Authenticate first, including HEAD.
            if request.META.get("HTTP_IF_RANGE") not in (None, etag):
                request.META.pop("HTTP_RANGE", None)
            if request.META.get("HTTP_RANGE"):
                try:
                    ranges = BoundedRangeReader(file).parse_range_header(
                        request.META["HTTP_RANGE"], artifact.size_bytes
                    )
                except ValueError:
                    ranges = None
                if (
                    not ranges
                    or len(ranges) != 1
                    or not (0 <= ranges[0][0] < artifact.size_bytes and ranges[0][0] < ranges[0][1])
                ):
                    file.close()
                    response = HttpResponse(status=416)
                    response["Content-Range"] = f"bytes */{artifact.size_bytes}"
                    response["Cache-Control"] = "private, no-store"
                    return response
            response = PrivateRangeResponse(
                request,
                file,
                content_type={
                    "JSON": "application/json",
                    "JSONL": "application/x-ndjson",
                    "CONLL2003": "application/zip",
                }[artifact.format],
            )
            if response.status_code == 200:
                response["Content-Length"] = artifact.size_bytes
            response["ETag"], response["Accept-Ranges"] = etag, "bytes"
            response["Content-Disposition"] = f'attachment; filename="{artifact.name}"'
            response["Cache-Control"], response["X-Content-Type-Options"] = "private, no-store", "nosniff"
            return response
        service.fail("NOT_FOUND", 404)

    def post(self, request, *args, **kwargs):
        action = kwargs.get("action", "list")
        if action == "preview":
            normalized = service.normalize(self.body())
            rows, xml = service.selected_records(self.project, normalized, self.policy["source_namespace"])
            service.audit(self.project, self.principal, self.actor, "preview")
            return Response(
                {
                    "item_count": len(rows),
                    "task_count": len({r["task_id"] for r in rows}),
                    "label_config_sha256": service.digest(xml.encode("utf-8")),
                    "formats": service.formats(xml),
                }
            )
        if action == "list":
            release = service.freeze(
                self.project, self.principal, self.actor, self.body(), request.headers.get("Idempotency-Key")
            )
            return Response(service.summary(release, self.actor), status=202)
        if action == "retry":
            if self.body() != {}:
                service.fail("INVALID_REQUEST", 400)
            with transaction.atomic():
                release = self.receipt()
                release = Release.objects.select_for_update().get(pk=release.pk)
                service.creator(release, self.project)
                if release.actor_id_frozen != self.actor.pk:
                    service.fail("RELEASE_FORBIDDEN", 403)
                if release.state in ("running", "queued"):
                    attempt = Attempt.objects.get(release=release, number=release.attempt_number)
                    if attempt.heartbeat > timezone.now() - timedelta(seconds=service.limits()["timeout"] + 60):
                        service.fail("RELEASE_NOT_READY")
                    attempt.state = "expired"
                    attempt.save(update_fields=["state"])
                    release.state = "failed_retryable"
                    release.save(update_fields=["state", "updated_at"])
                if release.state not in ("dispatch_pending", "failed_retryable"):
                    service.fail("RELEASE_NOT_READY")
                service.integrity(release)
                service.audit(self.project, self.principal, self.actor, "retry", release=release)
                transaction.on_commit(lambda: service.dispatch(release.pk))
            release.refresh_from_db()
            return Response(service.summary(release, self.actor), status=202)
        service.fail("NOT_FOUND", 404)
