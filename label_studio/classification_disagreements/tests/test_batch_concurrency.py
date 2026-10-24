"""Real PostgreSQL connections: project-first ordering and late read withdrawal."""

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from classification_disagreements import batches
from classification_disagreements.models import ClassificationBatch
from classification_disagreements.tests import test_batches as fixtures
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import Project, ProjectMember
from rest_framework.response import Response
from rest_framework.test import APIClient
from tasks.models import Submission


@override_settings(
    ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=True, ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=True
)
class TestClassificationBatchConcurrency(TransactionTestCase):
    client_class = APIClient
    make_source = fixtures.TestClassificationBatches.make_source
    set_answer = fixtures.TestClassificationBatches.set_answer
    preview = fixtures.TestClassificationBatches.preview
    body = fixtures.TestClassificationBatches.body
    create = fixtures.TestClassificationBatches.create
    new_batch = fixtures.TestClassificationBatches.new_batch
    item = fixtures.TestClassificationBatches.item

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Independent connections and row locks require PostgreSQL")
        fixtures.TestClassificationBatches.setUpTestData.__func__(self)
        fixtures.TestClassificationBatches.setUp(self)

    def client_for(self, actor=None):
        client = APIClient()
        client.force_authenticate(actor or self.manager)
        client.handler.load_middleware()
        return client

    def post(self, client, body, key, attempted=None):
        close_old_connections()
        try:

            def observe(execute, sql, params, many, context):
                if attempted is not None and "FOR UPDATE" in sql and '"project"' in sql:
                    attempted.set()
                return execute(sql, params, many, context)

            with connection.execute_wrapper(observe):
                return client.post(self.base, body, format="json", HTTP_IDEMPOTENCY_KEY=str(key))
        finally:
            connection.close()

    def test_same_key_waits_then_recovers_exact_single_row(self):
        body, key = self.body(), uuid4()
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
        original = batches.build_plan

        def hold(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get("lock"):
                entered.set()
                assert release.wait(20)
            return result

        clients = [self.client_for(), self.client_for()]
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(batches, "build_plan", hold):
            first = pool.submit(self.post, clients[0], body, key)
            try:
                assert entered.wait(20)
                second = pool.submit(self.post, clients[1], body, key, attempted)
                assert attempted.wait(20)
            finally:
                release.set()
            responses = [first.result(25), second.result(25)]
        assert sorted(r.status_code for r in responses) == [200, 201]
        assert responses[0].data["id"] == responses[1].data["id"]
        assert ClassificationBatch.objects.count() == 1

    def test_different_request_on_waiting_same_key_conflicts(self):
        body, key = self.body(), uuid4()
        changed = self.body(self.preview({**self.args, "name": "Other"}))
        client, attempted = self.client_for(), threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                future = pool.submit(self.post, client, changed, key, attempted)
                assert attempted.wait(20)
                assert self.create(body, key).status_code == 201
            response = future.result(25)
        assert response.status_code == 409 and ClassificationBatch.objects.count() == 1

    def test_revocation_commits_before_waiting_creation(self):
        body, client, attempted = self.body(), self.client_for(), threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                future = pool.submit(self.post, client, body, uuid4(), attempted)
                assert attempted.wait(20)
                OrganizationMember.objects.filter(organization=self.org, user=self.manager).update(
                    deleted_at=timezone.now()
                )
            response = future.result(25)
        assert response.status_code in (403, 404) and not ClassificationBatch.objects.exists()

    def test_other_actor_waiting_on_same_key_cannot_recover_original_batch(self):
        body, key, attempted = self.body(), uuid4(), threading.Event()
        ProjectMember.objects.filter(pk=self.review_member.pk).update(role="manager")
        client = self.client_for(self.reviewer)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                future = pool.submit(self.post, client, body, key, attempted)
                assert attempted.wait(20)
                assert self.create(body, key).status_code == 201
            response = future.result(25)
        assert response.status_code == 409 and "replayed" not in response.data and "manifest_hash" not in response.data
        assert response.data["display_context"]["reason"] == "CLASSIFICATION_BATCH_KEY_CONFLICT"
        assert ClassificationBatch.objects.count() == 1

    def test_new_revision_commits_before_waiting_creation(self):
        body, client, attempted = self.body(), self.client_for(), threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                future = pool.submit(self.post, client, body, uuid4(), attempted)
                assert attempted.wait(20)
                newer = self.make_source(assignment=self.first.assignment, revision=2)
                self.set_answer(newer, "Negative")
            response = future.result(25)
        assert response.status_code == 409 and not ClassificationBatch.objects.exists()

    def test_project_fence_held_through_response_render(self):
        body, client = self.body(), self.client_for()
        entered, release = threading.Event(), threading.Event()
        render = Response.render

        def hold(response):
            result = render(response)
            if response.status_code == 201:
                entered.set()
                assert release.wait(20)
            return result

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(Response, "render", hold):
            future = pool.submit(self.post, client, body, uuid4())
            try:
                assert entered.wait(20)
                with self.assertRaises(DatabaseError), transaction.atomic():
                    Project.objects.select_for_update(nowait=True).get(pk=self.project.pk)
            finally:
                release.set()
            response = future.result(25)
        assert response.status_code == 201

    def interleave_read(self, change):
        batch, client = self.new_batch(), self.client_for()
        entered, release = threading.Event(), threading.Event()
        original = batches.project_items

        def hold(*args):
            result = original(*args)
            if not entered.is_set():
                entered.set()
                assert release.wait(20)
            return result

        def read():
            close_old_connections()
            try:
                return client.get(f"{self.base}{batch.pk}/items/{self.first.assignment.task_id}/")
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(batches, "project_items", hold):
            future = pool.submit(read)
            try:
                assert entered.wait(20)
                change()
            finally:
                release.set()
            response = future.result(25)
        return response

    def test_revocation_after_read_materialization_never_returns_answers(self):
        response = self.interleave_read(
            lambda: OrganizationMember.objects.filter(organization=self.org, user=self.manager).update(
                deleted_at=timezone.now()
            )
        )
        assert response.status_code in (403, 404) and "item" not in response.data

    def test_source_corruption_after_read_materialization_discards_payload(self):
        response = self.interleave_read(
            lambda: Submission.objects.filter(pk=self.first.pk).update(result_hash="0" * 64)
        )
        assert response.status_code == 409 and "item" not in response.data

    def test_source_deletion_after_read_materialization_discards_payload(self):
        response = self.interleave_read(lambda: Submission.objects.filter(pk=self.first.pk).delete())
        assert response.status_code == 409 and "item" not in response.data

    def test_new_revision_after_freeze_does_not_replace_inputs(self):
        def change():
            newer = self.make_source(assignment=self.first.assignment, revision=2)
            self.set_answer(newer, "Negative")

        response = self.interleave_read(change)
        assert response.status_code == 200 and response.data["item"]["agreement"] == 1
        assert {pin["submission_id"] for pin in response.data["item"]["inputs"]} == {self.first.pk, self.second.pk}
