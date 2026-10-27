"""Real PostgreSQL independent connections at hydration/render response boundaries."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from classification_disagreements import ner, ner_api
from classification_disagreements.tests import test_ner_api as fixtures
from django.db import close_old_connections, connection
from django.db.models.query import QuerySet
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from organizations.models import OrganizationMember
from rest_framework.response import Response
from rest_framework.test import APIClient
from tasks.models import Submission, Task
from tasks.submissions import _snapshot_hash


@override_settings(ANNOTATION_NER_DISAGREEMENTS_ENABLED=True)
class TestNerConcurrency(TransactionTestCase):
    client_class = APIClient
    make_source = fixtures.TestNerAPI.make_source
    write = fixtures.TestNerAPI.write

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Requires real PostgreSQL independent connections')
        fixtures.TestNerAPI.setUpTestData.__func__(self)
        fixtures.TestNerAPI.setUp(self)

    def interleave(self, change, expected, remap=False, stage='render'):
        entered, release = threading.Event(), threading.Event()
        original_render, resolve = Response.render, ner_api.resolve_actor
        original_iterator = QuerySet.iterator

        def render(response):
            result = original_render(response)
            if stage == 'render' and response.status_code == 200 and not entered.is_set():
                entered.set()
                assert release.wait(20)
            return result

        def iterate(query, *args, **kwargs):
            if stage == 'hydrate' and query.model is Submission and not entered.is_set():
                entered.set()
                assert release.wait(20)
            return original_iterator(query, *args, **kwargs)

        def actor(request):
            principal, current = resolve(request)
            return (principal, self.reviewer) if remap and release.is_set() else (principal, current)

        def read():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(self.manager)
                return client.get(self.url, {'submissions': f'{self.first.pk},{self.second.pk}'})
            finally:
                connection.close()

        with (ThreadPoolExecutor(max_workers=1) as pool, patch.object(Response, 'render', render),
              patch.object(ner_api, 'resolve_actor', actor), patch.object(QuerySet, 'iterator', iterate)):
            pending = pool.submit(read)
            try:
                assert entered.wait(20)
                change()
            finally:
                release.set()
            result = pending.result(25)
        assert result.status_code in expected, result.data
        if result.status_code != 200:
            assert 'Alice' not in result.content.decode()
        return result

    def test_revocation_commits_before_final_read_authority(self):
        self.interleave(lambda: OrganizationMember.objects.filter(user=self.manager, organization=self.org).update(
            deleted_at=timezone.now()), (401, 403, 404))

    def test_source_hash_change_never_returns_rendered_answers(self):
        self.interleave(lambda: Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64), (409,))

    def test_source_deletion_never_returns_rendered_answers(self):
        self.interleave(lambda: Submission.objects.filter(pk=self.first.pk).delete(), (409,))

    def test_source_deletion_after_authorization_rejects_before_hydration(self):
        with patch.object(ner, 'Evaluator') as engine:
            self.interleave(lambda: Submission.objects.filter(pk=self.first.pk).delete(), (409,), stage='hydrate')
        engine.assert_not_called()

    def test_source_growth_after_sql_preflight_never_reaches_scoring(self):
        maximum = 8192
        def grow():
            value = copy.deepcopy(self.first.result_snapshot)
            value['padding'] = 'x' * maximum
            Submission.objects.filter(pk=self.first.pk).update(result_snapshot=value, result_hash=_snapshot_hash(value))
        with patch.object(ner, 'MAX_STORED_BYTES', maximum), patch.object(ner, 'Evaluator') as engine:
            self.interleave(grow, (422,), stage='hydrate')
        engine.assert_not_called()

    def test_actor_remapping_discards_previous_viewer_projection(self):
        self.interleave(lambda: None, (403, 404, 409), remap=True)

    def test_assignment_task_rebinding_discards_previous_scope(self):
        # A fresh task avoids the active-assignee uniqueness constraint masking
        # the committed scope change this response-boundary test must exercise.
        other = Task.objects.create(project=self.project, data={'text': 'Other task'})
        self.interleave(lambda: type(self.first.assignment).objects.filter(pk=self.first.assignment_id).update(
            task_id=other.pk), (404, 409))

    def test_new_revision_does_not_change_explicitly_pinned_results(self):
        def append():
            row = self.make_source(assignment=self.first.assignment, revision=2)
            self.write(row, [])
        response = self.interleave(append, (200,))
        assert response.data['scope']['input_ids'] == [self.first.pk, self.second.pk]
        assert response.data['pairs'][0]['f1'] == {'numerator': '1', 'denominator': '2'}
