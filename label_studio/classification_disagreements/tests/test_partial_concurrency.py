"""Separate PostgreSQL connections, including response materialization races."""
from unittest.mock import patch

from classification_disagreements import partial
from classification_disagreements.tests import test_concurrency as fixtures
from django.test import override_settings


@override_settings(ANNOTATION_CLASSIFICATION_PARTIAL_MATCH_ENABLED=True)
class TestPartialConcurrency(fixtures.TestComparisonConcurrency):
    def setUp(self):
        super().setUp()
        response = self.client.get(self.url, {'submissions': f'{self.first.pk},{self.second.pk}'})
        self.receipt = response.data['frozen']
        self.url += 'partial-match/'

    def interleave(self, change, expected):
        # Reuse the existing real connection/barrier harness. Its request query
        # is adapted only for this new read endpoint; old tests stay unchanged.
        from rest_framework.test import APIClient
        original_get = APIClient.get
        def get(client, path, data=None, **kwargs):
            if path == self.url:
                data = {'source_frozen': self.receipt}
            return original_get(client, path, data, **kwargs)
        with patch.object(APIClient, 'get', get):
            return super().interleave(change, expected)

    def test_newer_revision_does_not_replace_pinned_input(self):
        def append():
            row = self.make_source(assignment=self.first.assignment, revision=2)
            self.set_answer(row, 'Negative')
        response = self.interleave(append, (200,))
        assert response.data['partial']['score'] == partial.fraction(1)
        assert response.data['base']['agreement'] == 1
