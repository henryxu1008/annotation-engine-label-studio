import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from classification_disagreements import service
from classification_disagreements.tests import test_comparison as fixtures81
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from organizations.models import OrganizationMember
from rest_framework.test import APIClient
from review_batches.tests import test_batches as fixtures84
from tasks.models import Submission


@override_settings(ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=True)
class TestComparisonConcurrency(TransactionTestCase):
    client_class = APIClient
    make_source = fixtures81.TestClassification.make_source
    set_answer = fixtures81.TestClassification.set_answer

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Independent connections require PostgreSQL')
        fixtures84.TestReviewBatches.setUpTestData.__func__(self)
        fixtures81.TestClassification.setUp(self)

    def interleave(self, change, expected):
        entered, release = threading.Event(), threading.Event()
        calculate = service.calculate

        def hold(rows, project_id):
            result = calculate(rows, project_id)
            if not entered.is_set():
                entered.set()
                assert release.wait(20)
            return result

        def read():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(self.manager)
                return client.get(self.url, {'submissions': f'{self.first.pk},{self.second.pk}'})
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(service, 'calculate', hold):
            pending = pool.submit(read)
            try:
                assert entered.wait(20)
                change()
            finally:
                release.set()
            result = pending.result(25)
        assert result.status_code in expected, result.data
        if result.status_code != 200:
            assert 'locations' not in result.data
        return result

    def test_revocation_commits_after_materialization(self):
        self.interleave(
            lambda: OrganizationMember.objects.filter(user=self.manager, organization=self.org).update(
                deleted_at=timezone.now()
            ),
            (401, 403, 404),
        )

    def test_source_hash_change_commits_after_materialization(self):
        self.interleave(lambda: Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64), (409,))

    def test_source_deletion_commits_after_materialization(self):
        self.interleave(lambda: Submission.objects.filter(pk=self.first.pk).delete(), (409,))

    def test_newer_revision_does_not_replace_pinned_input(self):
        def append():
            newer = self.make_source(assignment=self.first.assignment, revision=2)
            self.set_answer(newer, 'Negative')

        result = self.interleave(append, (200,))
        assert result.data['agreement'] == 1
        assert [item['submission_id'] for item in result.data['manifest']['inputs']] == [self.first.pk, self.second.pk]
