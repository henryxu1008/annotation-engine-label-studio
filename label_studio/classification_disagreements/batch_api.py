from uuid import UUID

from access_control.project_operations import (
    ORGANIZE_CLASSIFICATION,
    lock_project_operation,
    require_project_operation,
)
from django.conf import settings
from django.core import signing
from django.db import connection, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound
from rest_framework.generics import get_object_or_404
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from review_batches.service import digest, fail

from . import batches, service
from .api import ComparisonAPI
from .models import ClassificationBatch


class Pagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 100


class BatchAPI(ComparisonAPI):
    pagination_class = Pagination

    def context(self, request, pk, task_id=None, *, create=False):
        principal, actor, project, role = super().context(request, pk, task_id)
        if not settings.ANNOTATION_CLASSIFICATION_BATCHES_ENABLED:
            raise NotFound()
        if connection.vendor not in ("postgresql", "sqlite"):
            fail("CLASSIFICATION_BATCH_BACKEND_UNSUPPORTED", 503)
        if create:
            if role != "manager":
                raise NotFound()
            require_project_operation(project, ORGANIZE_CLASSIFICATION)
        return principal, actor, project, role

    def query(self, request, allowed=()):
        query = request.query_params
        if set(query) - set(allowed) or any(len(query.getlist(key)) != 1 for key in query):
            fail("CLASSIFICATION_BATCH_INVALID_INPUT", 400)
        for name in ("page", "page_size"):
            if name in query and (
                not query[name].isascii()
                or not query[name].isdigit()
                or len(query[name]) > 9
                or int(query[name]) < 1
                or (name == "page_size" and int(query[name]) > 100)
            ):
                fail("CLASSIFICATION_BATCH_INVALID_INPUT", 400)

    def recheck(self, request, pk, context, *, create=False):
        current = self.context(request, pk, create=create)
        if current[0] != context[0] or current[1].pk != context[1].pk or current[3] != context[3]:
            fail("CLASSIFICATION_AUTHORITY_CHANGED")
        return current

    def respond(self, request, data, verify, *, status=200):
        before = digest(data)
        response = self.finalize_response(request, Response(data, status=status))
        response.render()
        verify()
        if digest(response.data) != before:
            fail("CLASSIFICATION_BATCH_REPRESENTATION_CHANGED")
        return response

    def read(self, request, pk, materialize):
        context = self.context(request, pk)
        data = materialize(context)

        def verify():
            current = self.recheck(request, pk, context)
            if data != materialize(current):
                fail("CLASSIFICATION_BATCH_READ_CHANGED")
            self.recheck(request, pk, context)

        return self.respond(request, {**data, "observed_at": timezone.now().isoformat()}, verify)

    def batch(self, context, batch_id):
        _, actor, project, role = context
        batch = batches.checked(get_object_or_404(ClassificationBatch, project=project, pk=batch_id), project)
        batches.metadata(batch, actor, role)
        return batch


class CapabilityAPI(BatchAPI):
    def get(self, request, pk):
        self.query(request)

        def materialize(context):
            _, _, project, role = context
            from access_control.project_operations import project_operation_policy

            return {
                "enabled": True,
                "can_create": role == "manager" and project_operation_policy(project).allows(ORGANIZE_CLASSIFICATION),
                "can_view_quality_overview": (settings.ANNOTATION_CLASSIFICATION_QUALITY_OVERVIEW_ENABLED
                                              and settings.ANNOTATION_CLASSIFICATION_PARTIAL_MATCH_ENABLED),
                "schema": batches.SCHEMA,
                "rule": service.RULE,
                "selection": batches.SELECTION,
                "limits": {
                    "tasks": batches.MAX_TASKS,
                    "participants": batches.MAX_PARTICIPANTS,
                    "inputs": batches.MAX_INPUTS,
                    "bytes": batches.MAX_BYTES,
                },
            }

        return self.read(request, pk, materialize)


class PreviewAPI(BatchAPI):
    def post(self, request, pk):
        self.query(request)
        context = self.context(request, pk, create=True)
        principal, actor, project, role = context
        name, ids = batches.preview_args(request.data, project)
        plan = batches.build_plan(project, ids)
        enumerated = timezone.now().isoformat()
        payload = {
            "schema": batches.SCHEMA,
            "project_id": project.pk,
            "organization_id": project.organization_id,
            "creator_principal": principal.principal_id,
            "actor_id_frozen": actor.pk,
            "role": role,
            "name": name,
            "task_ids": ids,
            "scope_enumerated_at": enumerated,
            "plan_hash": digest(plan),
            "selection": batches.SELECTION,
            "rule": service.RULE,
        }
        token = signing.dumps(payload, salt=batches.PREVIEW_SALT, compress=True)
        if len(token) > batches.MAX_TOKEN:
            batches.too_large()

        def verify():
            current = self.recheck(request, pk, context, create=True)
            if batches.build_plan(current[2], ids) != plan:
                fail("CLASSIFICATION_BATCH_PREVIEW_CHANGED")
            self.recheck(request, pk, context, create=True)

        return self.respond(
            request,
            {
                "schema": batches.SCHEMA,
                "preview_token": token,
                "plan_hash": payload["plan_hash"],
                "task_ids": ids,
                "display_counts": batches.counts([item["result"] for item in plan["tasks"]]),
                "scope_enumerated_at": enumerated,
            },
            verify,
        )


class ListAPI(BatchAPI):
    def get(self, request, pk):
        self.query(request, ("page", "page_size"))

        def materialize(context):
            _, actor, project, role = context
            page = self.paginate_queryset(batches.visible_batches(project, actor, role))
            rows = [batches.metadata(batches.checked(batch, project), actor, role) for batch in page]
            return self.get_paginated_response(rows).data

        return self.read(request, pk, materialize)

    def post(self, request, pk):
        self.query(request)
        context = self.context(request, pk, create=True)
        body = request.data
        if (
            not isinstance(body, dict)
            or set(body) != {"preview_token", "expected_plan_hash"}
            or not isinstance(body["preview_token"], str)
            or not 1 <= len(body["preview_token"]) <= batches.MAX_TOKEN
            or not isinstance(body["expected_plan_hash"], str)
            or len(body["expected_plan_hash"]) != 64
        ):
            fail("CLASSIFICATION_BATCH_INVALID_INPUT", 400)
        try:
            key = UUID(request.headers.get("Idempotency-Key", ""))
        except (ValueError, AttributeError):
            fail("CLASSIFICATION_BATCH_INVALID_KEY", 400)
        request_hash = digest(body)
        principal = context[0]
        with transaction.atomic():
            project, actor = lock_project_operation(pk, principal, ORGANIZE_CLASSIFICATION)
            context = self.recheck(request, pk, context, create=True)
            if actor.pk != context[1].pk:
                fail("CLASSIFICATION_AUTHORITY_CHANGED")
            previous = ClassificationBatch.objects.filter(project=project, idempotency_key=key).first()
            if previous:
                if (
                    previous.request_hash != request_hash
                    or previous.creator_principal != principal.principal_id
                    or previous.actor_id_frozen != actor.pk
                ):
                    fail("CLASSIFICATION_BATCH_KEY_CONFLICT")
                batch = batches.checked(previous, project)
                plan = None
            else:
                try:
                    payload = signing.loads(
                        body["preview_token"], salt=batches.PREVIEW_SALT, max_age=batches.PREVIEW_AGE
                    )
                except signing.BadSignature:
                    fail("CLASSIFICATION_BATCH_PREVIEW_INVALID", 400)
                if (
                    not isinstance(payload, dict)
                    or payload.get("schema") != batches.SCHEMA
                    or payload.get("project_id") != pk
                    or payload.get("organization_id") != project.organization_id
                    or payload.get("creator_principal") != principal.principal_id
                    or payload.get("actor_id_frozen") != actor.pk
                    or payload.get("role") != context[3]
                    or payload.get("selection") != batches.SELECTION
                    or payload.get("rule") != service.RULE
                    or payload.get("plan_hash") != body["expected_plan_hash"]
                ):
                    fail("CLASSIFICATION_BATCH_PREVIEW_INVALID", 400)
                plan = batches.build_plan(project, payload["task_ids"], lock=True)
                if digest(plan) != payload["plan_hash"]:
                    fail("CLASSIFICATION_BATCH_PREVIEW_CHANGED")
                manifest = {
                    **plan,
                    "schema": batches.SCHEMA,
                    "project_id": pk,
                    "organization_id": project.organization_id,
                    "creator_principal": principal.principal_id,
                    "actor_id_frozen": actor.pk,
                    "name": payload["name"],
                    "scope_enumerated_at": payload["scope_enumerated_at"],
                    "calculated_at": timezone.now().isoformat(),
                }
                batch = ClassificationBatch.objects.create(
                    project=project,
                    created_by=actor,
                    creator_principal=principal.principal_id,
                    actor_id_frozen=actor.pk,
                    name=payload["name"],
                    idempotency_key=key,
                    request_hash=request_hash,
                    manifest=manifest,
                    manifest_hash=digest(manifest),
                )

            def verify():
                current = self.recheck(request, pk, context, create=True)
                batches.checked(ClassificationBatch.objects.get(pk=batch.pk), current[2])
                if plan is not None and batches.build_plan(current[2], plan["task_ids"]) != plan:
                    fail("CLASSIFICATION_BATCH_PREVIEW_CHANGED")
                self.recheck(request, pk, context, create=True)

            return self.respond(
                request,
                {
                    **batches.metadata(batch, actor, context[3]),
                    "replayed": previous is not None,
                    "idempotency_key": str(key),
                },
                verify,
                status=200 if previous else 201,
            )


class DetailAPI(BatchAPI):
    def get(self, request, pk, batch_id):
        self.query(request)

        def materialize(context):
            batch = self.batch(context, batch_id)
            return {
                **batches.metadata(batch, context[1], context[3]),
                "display_counts": batches.counts(batches.project_items(batch, context[1], context[3])),
            }

        return self.read(request, pk, materialize)


class ItemsAPI(BatchAPI):
    def get(self, request, pk, batch_id):
        self.query(request, ("page", "page_size", "state"))
        state = request.query_params.get("state", "disagreement")
        if state not in ("all", "agreement", "disagreement", "unavailable"):
            fail("CLASSIFICATION_BATCH_INVALID_INPUT", 400)

        def materialize(context):
            batch = self.batch(context, batch_id)
            items = batches.project_items(batch, context[1], context[3])
            filtered = [item for item in items if state == "all" or item["outcome"] == state]
            page = self.paginate_queryset(filtered)
            data = self.get_paginated_response(
                [
                    {
                        key: row[key]
                        for key in ("task_id", "outcome", "reason", "agreement", "matching_pairs", "total_pairs")
                    }
                    for row in page
                ]
            ).data
            return {**data, "batch_id": batch.pk, "display_counts": batches.counts(items)}

        return self.read(request, pk, materialize)


class ItemAPI(BatchAPI):
    def get(self, request, pk, batch_id, task_id):
        self.query(request)

        def materialize(context):
            batch = self.batch(context, batch_id)
            item = next(
                (item for item in batches.project_items(batch, context[1], context[3]) if item["task_id"] == task_id),
                None,
            )
            if item is None:
                raise NotFound()
            return {**batches.metadata(batch, context[1], context[3]), "item": item,
                    "can_explain_partial": settings.ANNOTATION_CLASSIFICATION_PARTIAL_MATCH_ENABLED}

        return self.read(request, pk, materialize)
