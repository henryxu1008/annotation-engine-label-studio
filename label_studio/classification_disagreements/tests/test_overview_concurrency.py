"""Real PostgreSQL connections; reuse the existing frozen-read race harness."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from classification_disagreements import partial
from classification_disagreements.tests import test_concurrency as fixtures
from classification_disagreements.tests import test_batches as batch_fixtures
from django.db import close_old_connections, connection
from django.test import override_settings
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import ProjectMember
from rest_framework.response import Response
from rest_framework.test import APIClient


@override_settings(ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=True,
                   ANNOTATION_CLASSIFICATION_PARTIAL_MATCH_ENABLED=True,
                   ANNOTATION_CLASSIFICATION_QUALITY_OVERVIEW_ENABLED=True)
class TestOverviewConcurrency(fixtures.TestComparisonConcurrency):
    preview = batch_fixtures.TestClassificationBatches.preview
    body = batch_fixtures.TestClassificationBatches.body
    create = batch_fixtures.TestClassificationBatches.create
    new_batch = batch_fixtures.TestClassificationBatches.new_batch

    def setUp(self):
        super().setUp()
        self.base = f'/api/projects/{self.project.pk}/classification-disagreements/batches/'
        self.args = {'name': 'Concurrent overview', 'scope': {'kind': 'tasks', 'task_ids': [self.first.assignment.task_id]}}
        self.batch = self.new_batch()
        self.url = f'{self.base}{self.batch.pk}/quality-overview/'

    def interleave(self, change, expected):
        get = APIClient.get
        def adapted(client, path, data=None, **kwargs):
            return get(client, path, {} if path == self.url else data, **kwargs)
        with patch.object(APIClient, 'get', adapted):
            return super().interleave(change, expected)

    def test_newer_revision_does_not_replace_pinned_input(self):
        def append():
            row = self.make_source(assignment=self.first.assignment, revision=2)
            self.set_answer(row, 'Negative')
        response = self.interleave(append, (200,))
        assert response.data['summary']['exact_agreement'] == partial.fraction(1)

    def after_render_change(self, change, expected):
        render = Response.render
        def mutate():
            close_old_connections()
            try:
                change()
            finally:
                connection.close()
        def after_render(response):
            result = render(response)
            if isinstance(response.data, dict) and response.data.get('schema') == 'classification-quality-overview/v1':
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(mutate).result(20)
            return result
        with patch.object(Response, 'render', after_render):
            response = self.client.get(self.url)
        assert response.status_code in expected, response.data
        assert 'summary' not in response.data

    def test_revocation_commits_after_actual_json_render(self):
        self.after_render_change(lambda: OrganizationMember.objects.filter(
            organization=self.org, user=self.manager).update(deleted_at=timezone.now()), (403, 404))

    def test_role_change_commits_after_actual_json_render(self):
        self.member.role = 'manager'
        self.member.save()
        self.client.force_authenticate(self.worker)
        self.after_render_change(lambda: ProjectMember.objects.filter(pk=self.member.pk).update(role='reviewer'), (409,))

    def test_source_hash_change_commits_after_actual_json_render(self):
        from tasks.models import Submission
        self.after_render_change(lambda: Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64), (409,))
