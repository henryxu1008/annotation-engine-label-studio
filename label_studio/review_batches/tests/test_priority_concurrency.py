import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from projects.models import Project
from rest_framework.test import APIClient
from review_batches import service
from review_batches.models import ReviewBatch
from review_batches.tests import test_concurrency as existing
from review_batches.tests import test_priority as fixtures


@override_settings(
    ANNOTATION_REVIEW_BATCHES_ENABLED=True,
    ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=True,
    ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=True,
)
class TestPriorityConcurrency(TransactionTestCase):
    client_class = APIClient
    make_source = fixtures.TestPriority.make_source
    set_answer = fixtures.TestPriority.set_answer
    freeze_analysis = fixtures.TestPriority.freeze_analysis
    preview = fixtures.TestPriority.preview
    run_request = existing.TestBatchConcurrency.run_request

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Independent PostgreSQL row-lock interleavings required')
        fixtures.TestPriority.setUpTestData.__func__(type(self))
        fixtures.TestPriority.setUp(self)
        self.body = {**self.args, 'expected_plan_hash': self.preview().data['plan_hash']}

    def test_same_key_waits_and_recovers_one_frozen_plan(self):
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
        original = service.build_plan

        def hold(*args):
            entered.set()
            assert release.wait(15)
            return original(*args)

        key = uuid4()
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(service, 'build_plan', side_effect=hold):
            first = pool.submit(self.run_request, self.manager, self.base, self.body, key=key)
            try:
                assert entered.wait(15)
                second = pool.submit(
                    self.run_request, self.manager, self.base, self.body, key=key, attempted=attempted
                )
                assert attempted.wait(15)
            finally:
                release.set()
            responses = [first.result(20), second.result(20)]
        assert sorted(r.status_code for r in responses) == [200, 201]
        assert responses[0].data['id'] == responses[1].data['id']
        assert ReviewBatch.objects.count() == 1

    def test_same_key_different_body_has_one_winner(self):
        key = uuid4()
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [
                pool.submit(self.run_request, self.manager, self.base, {**self.body, 'name': name}, key=key)
                for name in ('First', 'Second')
            ]
            assert sorted(job.result(20).status_code for job in jobs) == [201, 409]
        assert ReviewBatch.objects.count() == 1

    def test_revocation_first_denies_waiting_creation(self):
        attempted = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                pending = pool.submit(
                    self.run_request, self.manager, self.base, self.body, key=uuid4(), attempted=attempted
                )
                assert attempted.wait(15)
                Project.objects.filter(pk=self.project.pk).update(created_by=self.worker)
            response = pending.result(20)
        assert response.status_code in (403, 404)
        assert not ReviewBatch.objects.exists()

    def test_new_revision_first_rejects_waiting_stale_preview(self):
        attempted = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                pending = pool.submit(
                    self.run_request, self.manager, self.base, self.body, key=uuid4(), attempted=attempted
                )
                assert attempted.wait(15)
                self.make_source(assignment=self.first.assignment, revision=2)
            response = pending.result(20)
        assert response.status_code == 409
        assert not ReviewBatch.objects.exists()
