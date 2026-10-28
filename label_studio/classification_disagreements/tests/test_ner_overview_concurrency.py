"""Independent PostgreSQL writers at the overview's actual render boundary."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from classification_disagreements import ner_overview as overview
from classification_disagreements.tests import test_ner_overview as f
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import Project
from rest_framework.response import Response
from rest_framework.test import APIClient
from tasks.models import Submission, TaskAssignment
from tasks.submissions import _snapshot_hash


@override_settings(ANNOTATION_NER_DISAGREEMENTS_ENABLED=True, ANNOTATION_NER_BATCHES_ENABLED=True,
                   ANNOTATION_NER_QUALITY_OVERVIEW_ENABLED=True)
class TestNerOverviewConcurrency(TransactionTestCase):
    client_class = APIClient
    make_source = f.TestNerOverview.make_source
    write = f.TestNerOverview.write
    extra_task = f.TestNerOverview.extra_task
    preview = f.TestNerOverview.preview
    create = f.TestNerOverview.create
    freeze = f.TestNerOverview.freeze
    read = f.TestNerOverview.read

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Requires real PostgreSQL independent connections')
        f.TestNerOverview.setUpTestData.__func__(self)
        f.TestNerOverview.setUp(self)

    def interleave(self, change, expected):
        render = Response.render
        entered = False
        def writer():
            close_old_connections()
            try:
                change()
            finally:
                connection.close()
        def after_render(response):
            nonlocal entered
            result = render(response)
            if not entered and isinstance(response.data, dict) and response.data.get('schema') == overview.SCHEMA:
                entered = True
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(writer).result(20)
            return result
        with patch.object(Response, 'render', after_render):
            response = self.read()
        self.assertTrue(entered)
        self.assertIn(response.status_code, expected, response.data)
        if response.status_code != 200:
            self.assertNotIn('summary', response.data)
            self.assertNotIn('annotators', response.data)
        return response

    def test_organization_revocation_after_render_discards_overview(self):
        self.interleave(lambda: OrganizationMember.objects.filter(user=self.manager, organization=self.org).update(
            deleted_at=timezone.now()), (401, 403, 404))

    @override_settings(ANNOTATION_PROJECT_OPERATION_PROVIDER='classification_disagreements.tests.test_ner_batch_concurrency.ReadPolicy')
    def test_read_work_withdrawal_committed_after_render_discards_overview(self):
        self.interleave(lambda: Project.objects.filter(pk=self.project.pk).update(description='deny-read'), (403,))

    def test_source_deletion_after_render_discards_overview(self):
        self.interleave(lambda: Submission.objects.filter(pk=self.first.pk).delete(), (409,))

    def test_source_corruption_after_render_discards_overview(self):
        self.interleave(lambda: Submission.objects.filter(pk=self.first.pk).update(result_hash='0'*64), (409,))

    def test_source_snapshot_change_after_render_discards_overview(self):
        snapshot = self.first.result_snapshot.copy()
        snapshot['unexpected'] = '合成'
        self.interleave(lambda: Submission.objects.filter(pk=self.first.pk).update(
            result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)), (409,))

    def test_assignment_rebinding_after_render_discards_overview(self):
        self.interleave(lambda: TaskAssignment.objects.filter(pk=self.first.assignment_id).update(
            assignee_id=self.manager.pk), (409,))

    def test_new_revision_committed_during_read_keeps_original_frozen_result(self):
        before = self.read().data
        def append():
            row = self.make_source(assignment=self.first.assignment, revision=2)
            self.write(row, [])
            Submission.objects.filter(pk=self.first.pk).update(status=Submission.Status.SUPERSEDED)
        response = self.interleave(append, (200,))
        for key in ('scope', 'source_set_hash', 'scope_counts', 'summary', 'annotators'):
            self.assertEqual(response.data[key], before[key])
