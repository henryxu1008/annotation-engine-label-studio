from unittest.mock import patch
from uuid import uuid4

from rest_framework.test import APITestCase
from review_batches.models import SecondReviewDecision, SecondReviewTask
from tasks.models import ReviewDecision, Submission

from .second_review_fixtures import SecondReviewFixtures


class TestSecondReview(SecondReviewFixtures, APITestCase):
    def setUp(self):
        self.second_setup()

    def test_A01_ordinary_dispatch_and_independent_confirmed(self):
        source, batch = self.prepared()
        before = list(Submission.objects.values()), list(ReviewDecision.objects.values())
        result, _ = self.dispatch(source, batch)
        assert result.status_code == 201, result.data
        task_id = result.data['receipt']['task_id']
        self.client.force_authenticate(self.supervisor)
        queue = self.client.get(self.second_base)
        assert queue.status_code == 200 and queue.data['count'] == 1, queue.data
        result = self.decide(task_id)
        assert result.status_code == 201, result.data
        assert result.data['receipt']['outcome'] == 'confirmed'
        assert (list(Submission.objects.values()), list(ReviewDecision.objects.values())) == before
        assert SecondReviewTask.objects.get().status == 'completed'

    def test_A03_rejected_concern_is_supervision_only(self):
        source, batch = self.prepared(decision='rejected')
        result, _ = self.dispatch(source, batch)
        assert result.status_code == 201, result.data
        pk = result.data['receipt']['task_id']
        result = self.decide(pk, self.opinion_body(pk, 'concern', '理由 Concern'))
        assert result.status_code == 201, result.data
        source.refresh_from_db()
        assert source.status == 'rejected' and not source.is_releasable
        assert SecondReviewDecision.objects.get().reason == '理由 Concern'

    def test_D03_recover_dispatch_without_live_origin(self):
        source, batch = self.prepared()
        key = uuid4()
        result, body = self.dispatch(source, batch, key=key)
        assert result.status_code == 201, result.data
        with patch(
            'review_batches.second_review_origin.load', side_effect=AssertionError('origin must not load')
        ), patch('review_batches.second_review_origin.hints', side_effect=AssertionError('no live lookup')):
            again, _ = self.dispatch(source, batch, body=body, key=key)
        assert again.status_code == 200 and again.data['replayed'], again.data
        assert again.data['receipt'] == result.data['receipt']

    def test_C09_cancel_has_separate_receipt_and_no_replacement(self):
        source, batch = self.prepared()
        result, body = self.dispatch(source, batch)
        assert result.status_code == 201, result.data
        pk = result.data['receipt']['task_id']
        create_ack = result.data['receipt']
        cancel = dict(expected_version=1, reason='错派 Cancel', expected_viewer_hash=body['expected_viewer_hash'])
        key = uuid4()
        result = self.client.post(
            self.second_base + f'{pk}/cancel/', cancel, format='json', HTTP_IDEMPOTENCY_KEY=str(key)
        )
        assert result.status_code == 201, result.data
        again = self.client.post(
            self.second_base + f'{pk}/cancel/', cancel, format='json', HTTP_IDEMPOTENCY_KEY=str(key)
        )
        assert again.status_code == 200 and again.data['receipt'] == result.data['receipt'], again.data
        assert SecondReviewTask.objects.get().receipt == create_ack
        replacement, _ = self.dispatch(source, batch, body=body)
        assert replacement.status_code == 409, replacement.data
