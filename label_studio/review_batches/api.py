from access_control.identity import resolve_actor
from access_control.project_operations import (
    ORGANIZE_REVIEW,
    REVIEW,
    lock_project_operation,
    require_project_operation,
)
from core.permissions import all_permissions
from django.db import transaction
from rest_framework import generics
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from tasks.models import Submission
from tasks.serializers import SubmissionSerializer

from . import service
from .models import ReviewBatch


class BatchPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 100

    def paginate_queryset(self, queryset, request, view=None):
        try:
            return super().paginate_queryset(queryset, request, view)
        except NotFound as exc:
            exc.display_context = {"reason": "SUBMISSION_PAGE_OUT_OF_RANGE"}
            raise


class BatchAPI(generics.GenericAPIView):
    permission_required = all_permissions.annotations_view
    pagination_class = BatchPagination

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response

    def context(self, request, pk, manager=False):
        if set(request.query_params) - {"page", "page_size"}:
            service.fail("REVIEW_BATCH_INVALID_INPUT", 400)
        principal, actor = resolve_actor(request)
        project, role, policy = service.guard(pk, principal, actor, manager=manager)
        return principal, actor, project, role, policy


class CapabilityAPI(BatchAPI):
    def get(self, request, pk):
        principal, actor = resolve_actor(request)
        project, role, policy = service.guard(pk, principal, actor)
        return Response({"enabled": True, "can_create": role == "manager" and policy.allows(ORGANIZE_REVIEW)})


class PreviewAPI(BatchAPI):
    def post(self, request, pk):
        principal, actor, project, _, _ = self.context(request, pk, manager=True)
        require_project_operation(project, ORGANIZE_REVIEW)
        args = service.payload(request)
        plan = service.build_plan(project, args)
        service.guard(pk, principal, actor, manager=True)
        require_project_operation(project, ORGANIZE_REVIEW)
        return Response({"plan": plan, "plan_hash": service.digest(plan)})


class ListAPI(BatchAPI):
    def get(self, request, pk):
        principal, actor, project, role, _ = self.context(request, pk)
        page = self.paginate_queryset(ReviewBatch.objects.filter(project=project).order_by("-pk"))
        data = [service.metadata(batch) for batch in page]
        _, current_role, _ = service.guard(pk, principal, actor)
        if current_role != role:
            raise PermissionDenied()
        return self.get_paginated_response({"role": role, "batches": data})

    def post(self, request, pk):
        principal, _, _, _, _ = self.context(request, pk, manager=True)
        args = service.payload(request)
        expected = args.pop("expected_plan_hash", None)
        if expected is None:
            service.fail("REVIEW_BATCH_PREVIEW_REQUIRED", 400)
        key = service.key_from_request(request)
        request_hash = service.digest({**args, "expected_plan_hash": expected})
        with transaction.atomic():
            project, actor = lock_project_operation(pk, principal, ORGANIZE_REVIEW)
            service.guard(pk, principal, actor, manager=True)
            previous = ReviewBatch.objects.filter(project=project, idempotency_key=key).first()
            if previous:
                if (
                    previous.request_hash != request_hash
                    or previous.creator_principal != principal.principal_id
                    or previous.actor_id_frozen != actor.pk
                ):
                    service.fail("REVIEW_BATCH_KEY_CONFLICT")
                service.checked_batch(project, previous.pk)
                return Response({**service.metadata(previous), "replayed": True})
            try:
                plan = service.build_plan(project, args)
            except APIException as exc:
                if getattr(exc, "display_context", {}).get("reason") == "REVIEW_BATCH_EMPTY":
                    service.fail("REVIEW_BATCH_PREVIEW_CHANGED")
                raise
            if service.digest(plan) != expected:
                service.fail("REVIEW_BATCH_PREVIEW_CHANGED")
            batch = ReviewBatch.objects.create(
                project=project,
                created_by=actor,
                actor_id_frozen=actor.pk,
                creator_principal=principal.principal_id,
                name=args["name"],
                idempotency_key=key,
                request_hash=request_hash,
                plan=plan,
                plan_hash=expected,
            )
        return Response({**service.metadata(batch), "replayed": False}, status=201)


class DetailAPI(BatchAPI):
    def get(self, request, pk, batch_id):
        principal, actor, project, role, _ = self.context(request, pk)
        batch = service.checked_batch(project, batch_id)
        data = service.projection(batch, actor, role)
        _, current_role, policy = service.guard(pk, principal, actor)
        if current_role != role:
            raise PermissionDenied()
        for item in data["items"]:
            item["can_review"] = item["can_review"] and policy.allows(REVIEW)
        return Response({**data, "role": role})


class QueueAPI(BatchAPI):
    def get(self, request, pk, batch_id):
        principal, actor, project, role, _ = self.context(request, pk)
        if role != "reviewer":
            raise PermissionDenied()
        require_project_operation(project, REVIEW)
        batch = service.checked_batch(project, batch_id)
        sources = service.checked_sources(batch)
        ids = [item["submission_id"] for item in batch.plan["candidates"] if item["selected"]]
        rows = (
            Submission.objects.filter(
                pk__in=ids, status="pending", assignment__project=project, assignment__task__project=project
            )
            .exclude(submitted_by=actor)
            .select_related("assignment__project", "submitted_by", "review__reviewer")
        )
        page = self.paginate_queryset(rows.order_by("pk"))
        data = SubmissionSerializer(page, many=True, context={"request": request}).data
        for item in data:
            service.check_representation(item, sources.get(item['id']))
        # Recheck materialized sources and live actor authority before exposing snapshots.
        service.checked_sources(batch)
        current = (
            Submission.objects.filter(
                pk__in=[item["id"] for item in data],
                status="pending",
                assignment__project=project,
                assignment__task__project=project,
            )
            .exclude(submitted_by=actor)
            .count()
        )
        if current != len(data) or any(item["id"] not in sources for item in data):
            service.fail("REVIEW_BATCH_QUEUE_CHANGED")
        project, current_role, _ = service.guard(pk, principal, actor)
        if current_role != 'reviewer':
            raise PermissionDenied()
        require_project_operation(project, REVIEW)
        return self.get_paginated_response(data)


class ReworkAPI(BatchAPI):
    """Read-only comparison anchored to a visible, frozen rejected revision."""

    def get(self, request, pk, batch_id, source_id):
        principal, actor, project, role, _ = self.context(request, pk)
        batch = service.checked_batch(project, batch_id)
        original = service.rework_source(batch, source_id, actor, role)
        rows = Submission.objects.filter(
            assignment_id=original.assignment_id, revision__gt=original.revision
        ).select_related('assignment__task', 'review__reviewer', 'submitted_by')
        page = self.paginate_queryset(rows.order_by('-revision', '-pk'))
        for row in page:
            service.check_rework_revision(row, original, project.pk)
        source_data = SubmissionSerializer(original, context={'request': request}).data
        data = SubmissionSerializer(page, many=True, context={'request': request}).data
        # Re-read after serialization: revoked access, changed status or evidence
        # must not expose a stale comparison assembled from separate reads.
        current_batch = service.checked_batch(project, batch_id)
        if current_batch.plan_hash != batch.plan_hash:
            service.fail('REVIEW_BATCH_PLAN_INVALID')
        current = service.rework_source(current_batch, source_id, actor, role)
        service.check_representation(source_data, current)
        current_rows = {
            row.pk: row for row in rows.filter(pk__in=[item['id'] for item in data])
        }
        for item in data:
            row = current_rows.get(item['id'])
            if row is None:
                service.fail('REVIEW_BATCH_SOURCE_INVALID')
            service.check_rework_revision(row, current, project.pk)
            service.check_representation(item, row)
        _, current_role, _ = service.guard(pk, principal, actor)
        if current_role != role:
            raise PermissionDenied()
        return self.get_paginated_response({'source': source_data, 'revisions': data})
