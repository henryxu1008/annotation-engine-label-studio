"""Real independent PostgreSQL transactions, not SQLite lock emulation."""

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import Project
from rest_framework.test import APIClient
from review_batches import followup_origin
from review_batches.models import ReviewBatch, SecondReviewConcernFollowup

from . import test_second_review_concurrency as existing_concurrency
from .test_concern_followup import ConcernFixtures


class TestConcernFollowupConcurrency(ConcernFixtures, TransactionTestCase):
    client_class = APIClient
    blocked = existing_concurrency.TestSecondReviewConcurrency.blocked

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Independent PostgreSQL connections required')
        ConcernFixtures.setUpTestData.__func__(type(self))
        self.followup_setup()
        self.source, self.batch, self.task = self.concerned()
        self.body = self.followup_body(self.task)

    def request(self, body=None, key=None, actor=None, started=None, pids=None):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_backend_pid()')
                if pids is not None:
                    pids.append(cursor.fetchone()[0])
            client = APIClient()
            client.force_authenticate(actor or self.manager)
            if started:
                started.set()
            return client.post(
                self.followup_base, body or self.body, format='json', HTTP_IDEMPOTENCY_KEY=str(key or uuid4())
            )
        finally:
            connection.close()

    def test_D05_same_key_one_record_same_receipt_two_source_passes(self):
        key, pids = uuid4(), []
        with (
            ThreadPoolExecutor(max_workers=2) as pool,
            patch.object(followup_origin, 'load', wraps=followup_origin.load) as reads,
        ):
            jobs = [pool.submit(self.request, key=key, pids=pids) for _ in range(2)]
            responses = [j.result(30) for j in jobs]
        assert len(set(pids)) == 2
        assert sorted(r.status_code for r in responses) == [200, 201], [r.data for r in responses]
        assert responses[0].data['receipt'] == responses[1].data['receipt']
        assert SecondReviewConcernFollowup.objects.count() == 1 and reads.call_count == 2

    def test_D06_distinct_keys_and_actors_compete_for_one_concern(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = [
                j.result(30) for j in [pool.submit(self.request), pool.submit(self.request, actor=self.producer)]
            ]
        assert sorted(r.status_code for r in responses) == [201, 409], [r.data for r in responses]
        assert SecondReviewConcernFollowup.objects.count() == 1

    def test_D05_waiting_same_key_recovers_after_winner_and_origin_deletion(self):
        key, pids, started = uuid4(), [], threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                waiter = pool.submit(self.request, key=key, started=started, pids=pids)
                assert started.wait(10)
                assert self.blocked(pids[0]), 'Waiter must actually block on the Project fence'
                saved = self.record_followup(self.body, key=key)
                assert saved.status_code == 201, saved.data
                ReviewBatch.objects.filter(pk=self.batch).delete()
            with (
                patch.object(followup_origin, 'load', side_effect=AssertionError('receipt-only waiting recovery')),
                patch.object(followup_origin, 'lock', side_effect=AssertionError('no origin lock after fence')),
            ):
                response = waiter.result(30)
        assert response.status_code == 200, response.data
        assert response.data['receipt'] == saved.data['receipt']

    def test_D07_revocation_commits_before_waiting_record(self):
        started, pids = threading.Event(), []
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                pending = pool.submit(self.request, started=started, pids=pids)
                assert started.wait(10) and self.blocked(pids[0])
                OrganizationMember.objects.filter(organization=self.org, user=self.manager).update(
                    deleted_at=timezone.now()
                )
            result = pending.result(30)
        assert result.status_code in (403, 404), result.data
        assert not SecondReviewConcernFollowup.objects.exists()

    def test_D08_new_submit_commits_first_stale_target_denied(self):
        target = self.successor(self.source)
        body = self.followup_body(self.task, target)
        started, pids = threading.Event(), []
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                pending = pool.submit(self.request, body=body, started=started, pids=pids)
                assert started.wait(10) and self.blocked(pids[0])
                newest = self.successor(self.source)
            result = pending.result(30)
        assert result.status_code == 409, result.data
        assert newest.revision > target.revision and not SecondReviewConcernFollowup.objects.exists()

    def test_D08_target_review_commits_first_context_denied(self):
        from tasks.submissions import review_submission

        target = self.successor(self.source)
        body = self.followup_body(self.task, target)
        started, pids = threading.Event(), []
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                pending = pool.submit(self.request, body=body, started=started, pids=pids)
                assert started.wait(10) and self.blocked(pids[0])
                review_submission(submission=target, reviewer=self.reviewer, decision='approved')
            result = pending.result(30)
        assert result.status_code == 409, result.data
        assert not SecondReviewConcernFollowup.objects.exists()

    def test_D07_D08_record_first_then_review_submit_and_revoke_keeps_frozen_receipt(self):
        from tasks.submissions import create_submission, review_submission

        target = self.successor(self.source)
        body, key = self.followup_body(self.task, target), uuid4()
        started, pids = threading.Event(), []

        def later():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pids.append(cursor.fetchone()[0])
                started.set()
                review_submission(submission=target, reviewer=self.reviewer, decision='approved')
                create_submission(annotation=target.annotation, assignment=target.assignment, actor=self.worker)
                with transaction.atomic():
                    Project.objects.select_for_update().get(pk=self.project.pk)
                    OrganizationMember.objects.filter(organization=self.org, user=self.manager).update(
                        deleted_at=timezone.now()
                    )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                pending = pool.submit(later)
                assert started.wait(10) and self.blocked(pids[0])
                response = self.record_followup(body, key=key)
                assert response.status_code == 201, response.data
            pending.result(30)
        row = SecondReviewConcernFollowup.objects.get()
        assert row.receipt == response.data['receipt']
        assert row.target_ref['status_at_record'] == 'pending' and row.target_ref['first_review'] is None
        assert self.record_followup(body, key=key).status_code in (403, 404)
