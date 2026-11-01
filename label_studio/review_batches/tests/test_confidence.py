import copy
from unittest.mock import patch
from uuid import uuid4

from django.test import override_settings
from rest_framework.test import APITestCase
from review_batches import confidence_contract as c
from review_batches import confidence_priority as p
from review_batches.models import ReviewBatch, ReviewConfidenceObservation
from tasks.models import Prediction, ReviewDecision, Submission, Task

from .confidence_fixtures import ConfidenceFixtures


class TestConfidence(ConfidenceFixtures, APITestCase):
    def setUp(self):
        self.confidence_setup()
        self.source = self.make_source()

    def test_producer_exact_input_fact_and_no_business_writes(self):
        before = list(Submission.objects.values())
        tasks_before = list(Task.objects.values())
        predictions_before = list(Prediction.objects.values())
        result = self.publish(self.source)
        assert result.status_code == 201, result.data
        row = ReviewConfidenceObservation.objects.get()
        assert row.proof['subject']['submission_id'] == self.source.pk
        assert row.proof['input_hash'] == self.source_input(self.source).data['input_hash']
        assert list(Submission.objects.values()) == before and not ReviewDecision.objects.exists()
        assert list(Task.objects.values()) == tasks_before
        assert list(Prediction.objects.values()) == predictions_before

    def test_preview_two_sorts_three_materializations_no_private_publisher_projection(self):
        self.make_source(self.second_worker)
        assert self.publish(self.source).status_code == 201
        with patch.object(p, 'assemble', wraps=p.assemble) as sorts, patch.object(
            p, 'materialize', wraps=p.materialize
        ) as passes:
            result = self.preview()
        assert result.status_code == 200, result.data
        assert sorts.call_count == 2 and passes.call_count == 3
        assert result.data['processing_cost']['materialization_passes'] == 3
        assert result.data['plan']['priority']['summary']['low_confidence_candidates'] == 1
        assert result.data['plan']['priority']['summary']['unknown_candidates'] == 1
        assert sum(x['selected'] for x in result.data['plan']['candidates']) == 1
        assert 'event_uuid' not in str(result.data) and 'producer_namespace' not in str(result.data)
        assert not ReviewBatch.objects.exists()

    def test_all_unknown_is_explicit_deterministic_fallback(self):
        self.make_source()
        first, second = self.preview(), self.preview()
        assert first.status_code == 200, first.data
        assert first.data == second.data
        assert first.data['plan']['priority']['summary']['unknown_candidates'] == 2
        assert self.create_batch().status_code == 201

    def test_manager_cannot_publish_or_get_producer_input(self):
        assert self.source_input(self.source, self.manager).status_code == 403
        body = self.fact_body(self.source)
        assert self.publish(body=body, actor=self.manager).status_code == 403
        assert not ReviewConfidenceObservation.objects.exists()

    def test_score_and_opaque_identity_are_strict(self):
        body = self.fact_body(self.source)
        for value in (True, 0.5, '123', -1, 1000001):
            response = self.publish(body={**body, 'score_units': value})
            assert response.status_code == 400, (value, response.data)
        assert self.publish(body={**body, 'actor_id': self.producer.pk}).status_code == 400
        assert not ReviewConfidenceObservation.objects.exists()

    def test_producer_original_key_recovers_without_live_snapshot_or_registry(self):
        key = uuid4()
        body = self.fact_body(self.source)
        first = self.publish(body=body, key=key)
        assert first.status_code == 201, first.data
        with override_settings(ANNOTATION_REVIEW_CONFIDENCE_PROFILES=[]), patch.object(
            c, 'load', side_effect=AssertionError('No source load')
        ):
            result = self.publish(body=body, key=key)
        assert result.status_code == 200 and result.data['replayed'], result.data
        assert result.data['observation_id'] == first.data['observation_id']
        assert self.publish(body={**body, 'score_units': 0}, key=key).status_code == 409

    def test_new_event_cannot_correct_same_revision(self):
        body = self.fact_body(self.source)
        assert self.publish(body=body).status_code == 201
        result = self.publish(body={**body, 'score_units': 0})
        assert result.status_code == 409 and ReviewConfidenceObservation.objects.count() == 1

    def test_batch_original_key_without_live_sources_or_descriptor(self):
        preview = self.preview()
        body = {**self.args, 'expected_plan_hash': preview.data['plan_hash']}
        key = uuid4()
        first = self.create_batch(body, key=key)
        assert first.status_code == 201, first.data
        with override_settings(ANNOTATION_REVIEW_CONFIDENCE_PROFILES=[]), patch.object(
            p, 'materialize', side_effect=AssertionError('No source load')
        ):
            result = self.create_batch(body, key=key)
        assert result.status_code == 200 and result.data['replayed'], result.data
        assert result.data['id'] == first.data['id'] and result.data['processing_cost']['materialization_passes'] == 0

    def test_detail_reviewer_redacts_score_and_can_review_selected_exact_revision(self):
        assert self.publish(self.source).status_code == 201
        result = self.create_batch()
        assert result.status_code == 201, result.data
        self.client.force_authenticate(self.reviewer)
        detail = self.client.get(self.base + str(result.data['id']) + '/')
        assert detail.status_code == 200, detail.data
        assert 'confidence_score_units' not in str(detail.data) and 'priority_source' not in detail.data
        queue = self.client.get(self.base + str(result.data['id']) + '/queue/')
        assert queue.status_code == 200, queue.data
        reviewed = self.client.post(
            f'/api/submissions/{self.source.pk}/review/?review_batch={result.data["id"]}',
            {'decision': 'approved'},
            format='json',
        )
        assert reviewed.status_code == 200 and reviewed.data['status'] == 'approved', reviewed.data

    def test_threshold_boundaries_and_selected_prefix_local_validation(self):
        assert self.publish(self.source, 1000000).status_code == 201
        for threshold in (0, 1000000):
            args = copy.deepcopy(self.args)
            args['priority']['threshold_units'] = threshold
            result = self.preview(args)
            assert result.status_code == 200, result.data
            assert result.data['plan']['priority']['summary']['low_confidence_candidates'] == 0
        assert self.create_batch().status_code == 201
        plan = copy.deepcopy(ReviewBatch.objects.get().plan)
        plan['candidates'][0]['selected'] = False
        with self.assertRaises(Exception):
            p.validate(plan)
