from unittest.mock import patch

from django.test import override_settings
from rest_framework.test import APITestCase
from review_batches.tests import test_batches as fixtures
from tasks.models import Submission
from tasks.serializers import SubmissionSerializer


@override_settings(ANNOTATION_REVIEW_BATCHES_ENABLED=True, ANNOTATION_FIXED_PACKAGES_ENABLED=True)
class TestReworkComparison(APITestCase):
    setUpTestData = classmethod(fixtures.TestReviewBatches.setUpTestData.__func__)
    setUp = fixtures.TestReviewBatches.setUp
    make_source = fixtures.TestReviewBatches.make_source
    preview = fixtures.TestReviewBatches.preview
    create = fixtures.TestReviewBatches.create
    review = fixtures.TestReviewBatches.review

    def prepare(self):
        self.batch = self.create().data['id']
        assert self.review(self.source, self.batch, 'rejected').status_code == 200
        self.newer = self.make_source(assignment=self.source.assignment, revision=2)
        self.url = f'{self.base}{self.batch}/rework/{self.source.pk}/'

    def test_multiple_reworks_keep_exact_original_and_page_all_later_revisions(self):
        self.prepare()
        assert (
            self.client.post(
                f'/api/submissions/{self.newer.pk}/review/',
                {'decision': 'rejected', 'reason': 'Again 再次返工'},
                format='json',
            ).status_code
            == 200
        )
        latest = self.make_source(assignment=self.source.assignment, revision=3)
        response = self.client.get(self.url, {'page_size': 1})
        assert response.status_code == 200
        assert response['Cache-Control'] == 'private, no-store'
        assert response.data['count'] == 2
        assert response.data['results']['source']['status'] == 'rejected'
        assert response.data['results']['source']['result_hash'] == self.source.result_hash
        assert response.data['results']['revisions'][0]['id'] == latest.pk
        page = self.client.get(self.url, {'page_size': 1, 'page': 2})
        assert page.data['results']['revisions'][0]['id'] == self.newer.pk
        assert page.data['results']['revisions'][0]['review']['reason'] == 'Again 再次返工'

    def test_unchanged_answers_are_valid_distinct_revisions_and_read_has_no_writes(
        self,
    ):
        self.prepare()
        before = list(Submission.objects.values('id', 'status', 'result_hash'))
        for user in (self.manager, self.reviewer):
            self.client.force_authenticate(user)
            response = self.client.get(self.url)
            assert response.status_code == 200
            data = response.data['results']
            assert data['source']['id'] != data['revisions'][0]['id']
            assert (
                data['source']['result_snapshot']['annotation']['result']
                == data['revisions'][0]['result_snapshot']['annotation']['result']
            )
        assert before == list(Submission.objects.values('id', 'status', 'result_hash'))

    def test_no_later_revision_is_explicit_empty(self):
        self.prepare()
        self.newer.delete()
        response = self.client.get(self.url)
        assert response.status_code == 200
        assert response.data['results']['revisions'] == []

    def test_revoked_member_and_annotator_cannot_read(self):
        self.prepare()
        self.client.force_authenticate(self.worker)
        assert self.client.get(self.url).status_code in (403, 404)
        self.client.force_authenticate(self.reviewer)
        self.review_member.delete()
        assert self.client.get(self.url).status_code in (403, 404)

    def test_foreign_project_batch_source_and_injected_actor_denied(self):
        self.prepare()
        assert (
            self.client.get(self.url.replace(f'projects/{self.project.pk}', f'projects/{self.other.pk}')).status_code
            == 404
        )
        unrelated = self.make_source()
        assert self.client.get(f'{self.base}{self.batch}/rework/{unrelated.pk}/').status_code == 404
        assert self.client.get(self.url, {'actor': self.manager.pk}).status_code == 400
        assert self.client.post(self.url, {'reviewer': self.manager.pk}, format='json').status_code == 405

    def test_self_source_is_not_exposed_to_reviewer(self):
        self.source.delete()
        self.source = self.make_source(worker=self.reviewer)
        batch = self.create().data['id']
        self.client.force_authenticate(self.reviewer)
        assert self.client.get(f'{self.base}{batch}/rework/{self.source.pk}/').status_code == 404

    def test_missing_original_does_not_fallback_to_successor(self):
        self.prepare()
        self.source.delete()
        assert self.client.get(self.url).status_code == 404

    def test_invalid_original_or_successor_fails_closed(self):
        self.prepare()
        Submission.objects.filter(pk=self.newer.pk).update(result_hash='0' * 64)
        assert self.client.get(self.url).status_code == 409

    def test_changed_successor_submitter_is_not_same_rework_chain(self):
        self.prepare()
        Submission.objects.filter(pk=self.newer.pk).update(submitted_by=self.reviewer)
        assert self.client.get(self.url).status_code == 409

    def test_response_time_revocation_discards_materialized_answers(self):
        self.prepare()
        original = SubmissionSerializer.to_representation

        def revoke(serializer, instance):
            data = original(serializer, instance)
            if instance.pk == self.newer.pk:
                self.review_member.delete()
            return data

        with patch.object(SubmissionSerializer, 'to_representation', revoke):
            response = self.client.get(self.url)
        assert response.status_code in (403, 404)
        assert 'results' not in response.data

    def test_serialized_snapshot_tampering_is_rejected(self):
        self.prepare()
        original = SubmissionSerializer.to_representation

        def tamper(serializer, instance):
            data = original(serializer, instance)
            if instance.pk == self.newer.pk:
                data['result_snapshot'] = {'forged': True}
            return data

        with patch.object(SubmissionSerializer, 'to_representation', tamper):
            assert self.client.get(self.url).status_code == 409

    def test_read_without_review_permission_and_core_mode_boundary(self):
        self.prepare()
        with override_settings(
            ANNOTATION_PROJECT_OPERATION_PROVIDER='review_batches.tests.test_batches.ReadOnlyPolicy'
        ):
            assert self.client.get(self.url).status_code == 200
        with override_settings(
            ANNOTATION_REVIEW_BATCH_MODES={f'{self.org.pk}:{self.project.pk}': 'core_only_internal'}
        ):
            assert self.client.get(self.url).status_code == 403
        with override_settings(ANNOTATION_REVIEW_BATCHES_ENABLED=False):
            assert self.client.get(self.url).status_code == 404
