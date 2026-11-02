"""Independent PostgreSQL connections observe real Project locks and commits."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import Project
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APIClient
from review_batches.models import SecondReviewDecision, SecondReviewTask
from tasks.models import Annotation, Submission, TaskAssignment
from tasks.submissions import create_submission

from .second_review_fixtures import SecondReviewFixtures


class TestSecondReviewConcurrency(SecondReviewFixtures, TransactionTestCase):
    client_class = APIClient

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Requires independent PostgreSQL connections')
        SecondReviewFixtures.setUpTestData.__func__(type(self))
        self.second_setup()
        self.source, self.batch = self.prepared()
        self.body = self.dispatch_body(self.source, self.batch)

    def run_request(self, action='organize', body=None, key=None, pk=None, started=None, pid=None):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_backend_pid()')
                if pid is not None:
                    pid.append(cursor.fetchone()[0])
            client = APIClient()
            client.force_authenticate(self.supervisor if action == 'decide' else self.manager)
            if started:
                started.set()
            suffix = '' if action == 'organize' else f'{pk}/' + ('decision/' if action == 'decide' else 'cancel/')
            return client.post(
                self.second_base + suffix, body or self.body, format='json', HTTP_IDEMPOTENCY_KEY=str(key or uuid4())
            )
        finally:
            connection.close()

    def blocked(self, pid):
        until = time.monotonic() + 10
        while time.monotonic() < until:
            with connection.cursor() as cursor:
                cursor.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s', [pid])
                row = cursor.fetchone()
            if row and row[0] == 'Lock':
                return True
            time.sleep(0.02)
        return False

    def initialized(self):
        result, _ = self.dispatch(self.source, self.batch, self.body)
        assert result.status_code == 201, result.data
        pk = result.data['receipt']['task_id']
        body = self.opinion_body(pk)
        return pk, body

    def test_D04_same_key_same_body_has_one_task_original_receipt_and_two_total_passes(self):
        from review_batches import second_review_origin as origin

        key = uuid4()
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(origin, 'load', wraps=origin.load) as passes:
            futures = [pool.submit(self.run_request, key=key) for _ in range(2)]
            results = [f.result(30) for f in futures]
        assert sorted(r.status_code for r in results) == [200, 201], [r.data for r in results]
        assert results[0].data['receipt'] == results[1].data['receipt']
        assert SecondReviewTask.objects.count() == 1 and passes.call_count == 2

    def test_D04_same_subject_different_keys_has_one_winner(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [f.result(30) for f in [pool.submit(self.run_request) for _ in range(2)]]
        assert sorted(r.status_code for r in results) == [201, 409], [r.data for r in results]
        assert SecondReviewTask.objects.count() == 1

    def test_D05_two_opinions_only_one_terminal(self):
        pk, body = self.initialized()
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [
                pool.submit(self.run_request, 'decide', {**body, 'outcome': outcome, 'reason': 'reason 理由'}, pk=pk)
                for outcome in ('confirmed', 'concern')
            ]
            results = [job.result(30) for job in jobs]
        assert sorted(r.status_code for r in results) == [201, 409], [r.data for r in results]
        assert SecondReviewTask.objects.get().status == 'completed' and SecondReviewDecision.objects.count() == 1

    def test_D05_opinion_and_cancel_only_one_terminal(self):
        pk, body = self.initialized()
        cancel = dict(expected_version=1, reason='stop', expected_viewer_hash=self.body['expected_viewer_hash'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [
                pool.submit(self.run_request, 'decide', body, pk=pk),
                pool.submit(self.run_request, 'cancel', cancel, pk=pk),
            ]
            results = [j.result(30) for j in jobs]
        assert sorted(r.status_code for r in results) == [201, 409], [r.data for r in results]
        row = SecondReviewTask.objects.get()
        assert row.status in ('completed', 'cancelled') and row.version == 2
        assert SecondReviewDecision.objects.count() == (1 if row.status == 'completed' else 0)

    def submit_new(self, started=None, pid=None):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_backend_pid()')
                if pid is not None:
                    pid.append(cursor.fetchone()[0])
            if started:
                started.set()
            assignment = TaskAssignment.objects.get(pk=self.source.assignment_id)
            annotation = Annotation.objects.get(pk=self.source.annotation_id)
            return create_submission(assignment=assignment, annotation=annotation, actor=self.worker).pk
        finally:
            connection.close()

    def test_D06_submit_commits_first_stale_dispatch_rejected(self):
        pid, attempted = [], threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                job = pool.submit(self.run_request, started=attempted, pid=pid)
                assert attempted.wait(10) and self.blocked(pid[0])
                newer = create_submission(
                    assignment=self.source.assignment, annotation=self.source.annotation, actor=self.worker
                )
            result = job.result(30)
        assert result.status_code == 409 and not SecondReviewTask.objects.exists(), result.data
        assert newer.revision == 2

    def test_D06_dispatch_commits_first_waiting_submit_preserves_historical_review(self):
        entered, release, attempted, pid = threading.Event(), threading.Event(), threading.Event(), []
        original = JSONRenderer.render

        def render(renderer, data, *args, **kwargs):
            result = original(renderer, data, *args, **kwargs)
            if isinstance(data, dict) and data.get('receipt', {}).get('command') == 'dispatch':
                entered.set()
                assert release.wait(15)
            return result

        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(JSONRenderer, 'render', render):
            first = pool.submit(self.run_request)
            try:
                assert entered.wait(15)
                second = pool.submit(self.submit_new, attempted, pid)
                assert attempted.wait(10) and self.blocked(pid[0])
            finally:
                release.set()
            result, newer = first.result(30), second.result(30)
        assert result.status_code == 201 and Submission.objects.get(pk=newer).revision == 2
        self.client.force_authenticate(self.supervisor)
        read = self.client.get(self.second_base + result.data['receipt']['task_id'] + '/source/')
        assert read.status_code == 200 and read.data['historical_revision'], read.data

    def test_D07_revocation_commits_first_waiting_command_rejected(self):
        pid, attempted = [], threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                job = pool.submit(self.run_request, started=attempted, pid=pid)
                assert attempted.wait(10) and self.blocked(pid[0])
                OrganizationMember.objects.filter(organization=self.org, user=self.manager).update(
                    deleted_at=timezone.now()
                )
            result = job.result(30)
        assert result.status_code in (403, 404) and not SecondReviewTask.objects.exists()

    def test_D07_command_holds_project_and_membership_fences_through_render(self):
        entered, release = threading.Event(), threading.Event()
        original = JSONRenderer.render

        def render(renderer, data, *args, **kwargs):
            result = original(renderer, data, *args, **kwargs)
            if isinstance(data, dict) and data.get('receipt', {}).get('command') == 'dispatch':
                entered.set()
                assert release.wait(15)
            return result

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(JSONRenderer, 'render', render):
            job = pool.submit(self.run_request)
            try:
                assert entered.wait(15)
                for query in (
                    Project.objects.filter(pk=self.project.pk),
                    OrganizationMember.objects.filter(organization=self.org, user=self.manager),
                ):
                    with self.assertRaises(DatabaseError), transaction.atomic():
                        list(query.select_for_update(nowait=True).values_list('pk', flat=True))
            finally:
                release.set()
            assert job.result(30).status_code == 201

    def test_D08_late_operation_revocation_rolls_back_and_original_key_is_reusable(self):
        key, original, touched = uuid4(), JSONRenderer.render, []
        mode = self.settings(CONFIDENCE_TEST_OPERATIONS=[])

        def render(renderer, data, *args, **kwargs):
            result = original(renderer, data, *args, **kwargs)
            if isinstance(data, dict) and data.get('receipt', {}).get('command') == 'dispatch' and not touched:
                touched.append(True)
                mode.enable()
            return result

        try:
            with patch.object(JSONRenderer, 'render', render):
                result = self.run_request(key=key)
        finally:
            mode.disable()
        assert result.status_code == 403 and not SecondReviewTask.objects.exists()
        assert self.run_request(key=key).status_code == 201
