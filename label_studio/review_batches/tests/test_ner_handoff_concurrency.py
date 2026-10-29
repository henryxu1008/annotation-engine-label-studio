"""Independent PostgreSQL connections over the NER handoff project fence."""
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from classification_disagreements.models import NerArbitrationTask
from classification_disagreements.tests import test_ner_arbitration_concurrency as concurrent
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import Project
from rest_framework.test import APIClient
from review_batches.models import NerReviewArbitrationHandoff
from tasks.models import ReviewDecision, Submission

from . import test_ner_priority as fixtures
from .test_ner_handoff import NerHandoffFixtures


@override_settings(
    ANNOTATION_REVIEW_BATCHES_ENABLED=True,
    ANNOTATION_NER_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_NER_BATCHES_ENABLED=True,
    ANNOTATION_NER_REVIEW_PRIORITY_ENABLED=True,
    ANNOTATION_NER_ARBITRATION_ENABLED=True,
    ANNOTATION_NER_REVIEW_ARBITRATION_HANDOFF_ENABLED=True,
)
class TestNerHandoffConcurrency(NerHandoffFixtures, TransactionTestCase):
    client_class = APIClient
    client_for = concurrent.TestNerArbitrationConcurrency.client_for
    post = concurrent.TestNerArbitrationConcurrency.post
    competing = concurrent.TestNerArbitrationConcurrency.competing

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Independent PostgreSQL connections required')
        fixtures.TestNerPriority.setUpTestData.__func__(self)
        NerHandoffFixtures.setUp(self)

    def pending(self):
        result = self.standalone()
        assert result.status_code == 201, result.data
        return NerArbitrationTask.objects.get(pk=result.data['id'])

    def command_body(self, row, cancel=False):
        value = {
            'expected_version': row.version,
            'expected_input_set_hash': row.input_set_hash,
            'reason': 'Frozen NER',
        }
        if not cancel:
            value['winner_submission_id'] = self.first.pk
        return value

    def test_same_parent_key_recovers_one_pair(self):
        value, key = self.handoff_body(), uuid4()
        results = self.competing(
            self.manager, self.manager, self.handoff_url, self.handoff_url, value, value, key, key
        )
        assert [r.status_code for r in results] == [201, 200]
        assert results[0].data['id'] == results[1].data['id']
        assert NerReviewArbitrationHandoff.objects.count() == NerArbitrationTask.objects.count() == 1

    def test_different_parent_keys_same_input_cannot_duplicate_target(self):
        value = self.handoff_body()
        results = self.competing(
            self.manager, self.manager, self.handoff_url, self.handoff_url, value, value, uuid4(), uuid4()
        )
        assert [r.status_code for r in results] == [201, 409]
        assert NerReviewArbitrationHandoff.objects.count() == NerArbitrationTask.objects.count() == 1

    def test_same_key_different_body_cannot_replace_intent(self):
        value, key = self.handoff_body(), uuid4()
        results = self.competing(
            self.manager,
            self.manager,
            self.handoff_url,
            self.handoff_url,
            value,
            {**value, 'expected_input_set_hash': '0' * 64},
            key,
            key,
        )
        assert [r.status_code for r in results] == [201, 409]

    def test_standalone_wins_even_when_public_keys_are_identical(self):
        value, key = self.handoff_body(), uuid4()
        results = self.competing(
            self.manager,
            self.manager,
            f'/api/projects/{self.project.pk}/ner-arbitrations/',
            self.handoff_url,
            self.standalone_body(),
            value,
            key,
            key,
        )
        assert [r.status_code for r in results] == [201, 409]
        assert NerArbitrationTask.objects.count() == 1 and not NerReviewArbitrationHandoff.objects.exists()

    def test_handoff_wins_and_child_key_is_separate(self):
        value, key = self.handoff_body(), uuid4()
        results = self.competing(
            self.manager,
            self.manager,
            self.handoff_url,
            f'/api/projects/{self.project.pk}/ner-arbitrations/',
            value,
            self.standalone_body(),
            key,
            key,
        )
        assert [r.status_code for r in results] == [201, 409]
        assert NerArbitrationTask.objects.get().idempotency_key != key

    def test_duplicate_link_keys_cannot_duplicate_binding(self):
        target = self.pending()
        value = self.handoff_body(target)
        results = self.competing(
            self.manager, self.manager, self.handoff_url, self.handoff_url, value, value, uuid4(), uuid4()
        )
        assert [r.status_code for r in results] == [201, 409]
        assert NerReviewArbitrationHandoff.objects.count() == 1

    def transition_race(self, action, first):
        target = self.pending()
        value = self.handoff_body(target)
        path = f'/api/projects/{self.project.pk}/ner-arbitrations/{target.pk}/{action}/'
        actor = self.manager if action == 'cancel' else self.independent
        if first:
            results = self.competing(
                actor,
                self.manager,
                path,
                self.handoff_url,
                self.command_body(target, cancel=action == 'cancel'),
                value,
                uuid4(),
                uuid4(),
                cancel_first=action == 'cancel',
            )
            assert [r.status_code for r in results] == [201, 409]
            assert not NerReviewArbitrationHandoff.objects.exists()
        else:
            results = self.competing(
                self.manager,
                actor,
                self.handoff_url,
                path,
                value,
                self.command_body(target, cancel=action == 'cancel'),
                uuid4(),
                uuid4(),
            )
            assert [r.status_code for r in results] == [201, 201]
            assert NerReviewArbitrationHandoff.objects.get().origin['target']['version_at_link'] == 1

    def test_decision_before_link_cas(self):
        self.transition_race('decision', True)

    def test_link_before_decision_preserves_version(self):
        self.transition_race('decision', False)

    def test_cancel_before_link_cas(self):
        self.transition_race('cancel', True)

    def test_link_before_cancel_preserves_version(self):
        self.transition_race('cancel', False)

    def test_revocation_while_original_parent_recovery_waits(self):
        value, key = self.handoff_body(), uuid4()
        r = self.handoff(value, key)
        assert r.status_code == 201, r.data
        attempted = threading.Event()
        client = self.client_for(self.manager)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                future = pool.submit(self.post, client, self.handoff_url, value, key, attempted)
                assert attempted.wait(20)
                OrganizationMember.objects.filter(organization=self.org, user=self.manager).update(
                    deleted_at=timezone.now()
                )
            result = future.result(25)
        assert result.status_code in (403, 404) and 'receipt' not in result.data
        assert NerArbitrationTask.objects.count() == NerReviewArbitrationHandoff.objects.count() == 1

    def source_transition_before_handoff(self, revision=False):
        value, attempted = self.handoff_body(), threading.Event()
        client = self.client_for(self.manager)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                future = pool.submit(self.post, client, self.handoff_url, value, uuid4(), attempted)
                assert attempted.wait(20)
                if revision:
                    newer = self.make_source(worker=self.worker, assignment=self.first.assignment, revision=2)
                    self.write(newer, [])
                    Submission.objects.filter(pk=self.first.pk).update(status='superseded')
                else:
                    result = self.client_for(self.independent).post(
                        f'/api/submissions/{self.first.pk}/review/?review_batch={self.batch.pk}',
                        {'decision': 'approved'},
                        format='json',
                    )
                    assert result.status_code in (200, 201), result.data
            result = future.result(25)
        assert result.status_code == 409, result.data
        assert not NerArbitrationTask.objects.exists() and not NerReviewArbitrationHandoff.objects.exists()

    def test_normal_review_committed_before_waiting_handoff_denies_it(self):
        self.source_transition_before_handoff()
        assert ReviewDecision.objects.count() == 1

    def test_new_revision_committed_before_waiting_handoff_is_not_substituted(self):
        self.source_transition_before_handoff(revision=True)
        assert not ReviewDecision.objects.exists()

    def test_handoff_before_normal_review_does_not_block_or_perform_review(self):
        value = self.handoff_body()
        path = f'/api/submissions/{self.first.pk}/review/?review_batch={self.batch.pk}'
        results = self.competing(
            self.manager, self.independent, self.handoff_url, path, value, {'decision': 'approved'}, uuid4(), uuid4()
        )
        assert results[0].status_code == 201 and results[1].status_code in (200, 201)
        assert ReviewDecision.objects.count() == 1
        assert NerArbitrationTask.objects.get().status == 'pending'
