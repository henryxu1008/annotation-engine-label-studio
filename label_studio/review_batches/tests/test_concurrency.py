"""Real independent PostgreSQL connections and the existing project write fence."""

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from organizations.tests.factories import OrganizationFactory
from projects.models import Project, ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.test import APIClient
from review_batches import service
from review_batches.models import ReviewBatch
from tasks.models import Annotation, ReviewDecision, Submission, TaskAssignment
from tasks.submissions import _canonical_snapshot, _snapshot_hash, create_submission
from tasks.tests.factories import TaskFactory
from users.tests.factories import UserFactory


@override_settings(ANNOTATION_REVIEW_BATCHES_ENABLED=True)
class TestBatchConcurrency(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Independent row-lock interleavings require PostgreSQL")
        self.org = OrganizationFactory()
        self.manager = self.org.created_by
        self.project = ProjectFactory(organization=self.org)
        self.worker = UserFactory(active_organization=self.org)
        self.reviewer = UserFactory(active_organization=self.org)
        ProjectMember.objects.create(project=self.project, user=self.worker, role="annotator")
        self.membership = ProjectMember.objects.create(project=self.project, user=self.reviewer, role="reviewer")
        task = TaskFactory(project=self.project)
        self.assignment = TaskAssignment.objects.create(
            task=task, project=self.project, assignee=self.worker, assigned_by=self.manager
        )
        self.annotation = Annotation.objects.create(
            task=task, project=self.project, completed_by=self.worker, result=[]
        )
        self.assignment.annotation = self.annotation
        self.assignment.save()
        snapshot = _canonical_snapshot(self.annotation, self.assignment)
        self.source = Submission.objects.create(
            assignment=self.assignment,
            annotation=self.annotation,
            revision=1,
            submitted_by=self.worker,
            result_snapshot=snapshot,
            result_hash=_snapshot_hash(snapshot),
        )
        modes = self.settings(ANNOTATION_REVIEW_BATCH_MODES={f"{self.org.pk}:{self.project.pk}": "fork_review"})
        modes.enable()
        self.addCleanup(modes.disable)
        self.client = APIClient()
        self.client.force_authenticate(self.manager)
        self.base = f"/api/projects/{self.project.pk}/review-batches/"
        self.args = {"name": "Concurrent", "rate": 100, "seed": "fixed"}
        preview = self.client.post(self.base + "preview/", self.args, format="json")
        assert preview.status_code == 200, preview.data
        self.args["expected_plan_hash"] = preview.data["plan_hash"]

    def run_request(self, actor, url, body, *, key=None, attempted=None):
        close_old_connections()
        try:
            client = APIClient()
            client.force_authenticate(actor)
            client.get("/health/")

            def observe(execute, sql, params, many, context):
                if attempted is not None and "FOR UPDATE" in sql and '"project"' in sql:
                    attempted.set()
                return execute(sql, params, many, context)

            with connection.execute_wrapper(observe):
                return client.post(url, body, format="json", **({"HTTP_IDEMPOTENCY_KEY": str(key)} if key else {}))
        finally:
            connection.close()

    def batch(self):
        result = self.client.post(self.base, self.args, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        assert result.status_code == 201, result.data
        return result.data["id"]

    def test_same_key_creates_one_plan_with_waiting_replay(self):
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
        original = service.build_plan
        calls = []

        def hold(*args):
            calls.append(True)
            entered.set()
            assert release.wait(15)
            return original(*args)

        key = uuid4()
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(service, "build_plan", side_effect=hold):
            first = pool.submit(self.run_request, self.manager, self.base, self.args, key=key)
            try:
                assert entered.wait(15)
                second = pool.submit(
                    self.run_request, self.manager, self.base, self.args, key=key, attempted=attempted
                )
                assert attempted.wait(15)
            finally:
                release.set()
            responses = [first.result(20), second.result(20)]
        assert sorted(r.status_code for r in responses) == [200, 201]
        assert responses[0].data["id"] == responses[1].data["id"]
        assert len(calls) == ReviewBatch.objects.count() == 1

    def test_two_reviewers_cannot_decide_same_revision_twice(self):
        batch = self.batch()
        other = UserFactory(active_organization=self.org)
        ProjectMember.objects.create(project=self.project, user=other, role="reviewer")
        url = f"/api/submissions/{self.source.pk}/review/?review_batch={batch}"
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [
                pool.submit(self.run_request, actor, url, {"decision": "approved"}) for actor in (self.reviewer, other)
            ]
            assert sorted(job.result(20).status_code for job in jobs) == [200, 400]
        assert ReviewDecision.objects.filter(submission=self.source).count() == 1

    def test_concurrent_different_requests_cannot_reuse_one_key(self):
        key = uuid4()
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(self.run_request, self.manager, self.base,
                {**self.args, 'name': name}, key=key) for name in ('First', 'Other')]
            assert sorted(job.result(20).status_code for job in pending) == [201, 409]
        assert ReviewBatch.objects.count() == 1

    def test_revocation_commits_while_decision_waits(self):
        batch = self.batch()
        attempted = threading.Event()
        url = f"/api/submissions/{self.source.pk}/review/?review_batch={batch}"
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                pending = pool.submit(
                    self.run_request, self.reviewer, url, {"decision": "approved"}, attempted=attempted
                )
                assert attempted.wait(15)
                self.membership.enabled = False
                self.membership.save()
            result = pending.result(20)
        assert result.status_code == 403
        assert not ReviewDecision.objects.exists()

    def test_manager_revoked_while_creation_waits(self):
        attempted = threading.Event()
        replacement = UserFactory(active_organization=self.org)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                pending = pool.submit(
                    self.run_request, self.manager, self.base, self.args, key=uuid4(), attempted=attempted
                )
                assert attempted.wait(15)
                Project.objects.filter(pk=self.project.pk).update(created_by=replacement)
            result = pending.result(20)
        assert result.status_code in (403, 404)
        assert not ReviewBatch.objects.exists()

    def test_new_revision_commits_before_waiting_review(self):
        batch = self.batch()
        attempted = threading.Event()
        url = f"/api/submissions/{self.source.pk}/review/?review_batch={batch}"
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                pending = pool.submit(
                    self.run_request, self.reviewer, url, {"decision": "approved"}, attempted=attempted
                )
                assert attempted.wait(15)
                newer = create_submission(assignment=self.assignment, annotation=self.annotation, actor=self.worker)
            result = pending.result(20)
        assert result.status_code == 400
        self.source.refresh_from_db()
        assert self.source.status == "superseded"
        assert newer.revision == 2
        assert not ReviewDecision.objects.exists()

    def test_batch_write_fence_is_held_through_response_serialization(self):
        from tasks.serializers import SubmissionSerializer

        batch = self.batch()
        entered, release = threading.Event(), threading.Event()
        original = SubmissionSerializer.to_representation

        def hold(serializer, instance):
            data = original(serializer, instance)
            entered.set()
            assert release.wait(15)
            return data

        url = f'/api/submissions/{self.source.pk}/review/?review_batch={batch}'
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(SubmissionSerializer, 'to_representation', hold):
            pending = pool.submit(self.run_request, self.reviewer, url, {'decision': 'approved'})
            try:
                assert entered.wait(15)
                with self.assertRaises(DatabaseError) as blocked, transaction.atomic():
                    Project.objects.select_for_update(nowait=True).get(pk=self.project.pk)
                cause = blocked.exception.__cause__
                assert (getattr(cause, 'pgcode', None) or getattr(cause, 'sqlstate', None)) == '55P03'
            finally:
                release.set()
            assert pending.result(20).status_code == 200
        assert ReviewDecision.objects.filter(submission=self.source).count() == 1
