from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from classification_disagreements.models import ClassificationGoldEvidence
from classification_disagreements.tests import test_comparison as fixtures
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import override_settings
from projects.models import ProjectMember
from rest_framework.test import APITestCase, APIClient, APITransactionTestCase
from tasks.models import Submission
from users.tests.factories import UserFactory


@override_settings(ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=True, ANNOTATION_CLASSIFICATION_GOLD_ENABLED=True)
class TestGoldAPI(APITestCase):
    setUpTestData = classmethod(fixtures.TestClassification.setUpTestData.__func__)
    make_source = fixtures.TestClassification.make_source
    set_answer = fixtures.TestClassification.set_answer

    def setUp(self):
        fixtures.TestClassification.setUp(self)
        self.gold_mode = self.settings(ANNOTATION_CLASSIFICATION_GOLD_PROJECTS={f'{self.org.pk}:{self.project.pk}': True})
        self.gold_mode.enable()
        self.addCleanup(self.gold_mode.disable)
        self.base = f'/api/projects/{self.project.pk}/classification-disagreements/gold/'

    def create(self, body, key=None):
        body = dict(body)
        source = Submission.objects.filter(pk=body.get('submission_id')).first()
        if source:
            body.setdefault('expected_result_hash', source.result_hash)
        if body.get('kind') == 'score':
            ref = ClassificationGoldEvidence.objects.filter(pk=body.get('reference_id')).first()
            if ref:
                body.setdefault('expected_reference_hash', ref.manifest_hash)
        return self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=str(key or uuid4()))

    def reference(self):
        result = self.create({'kind': 'reference', 'submission_id': self.first.pk})
        assert result.status_code == 201, result.data
        return result.data['id']

    def test_reference_and_score_freeze_exact_inputs_without_review_mutation(self):
        self.set_answer(self.second, 'Negative')
        before = list(Submission.objects.order_by('pk').values())
        ref = self.reference()
        result = self.create({'kind': 'score', 'reference_id': ref, 'submission_id': self.second.pk})
        assert result.status_code == 201, result.data
        scored = result.data['manifest']['contents']
        assert scored['source']['revision'] == self.second.revision
        assert scored['result']['matched_controls'] == 0 and scored['result']['total_controls'] == 1
        assert list(Submission.objects.order_by('pk').values()) == before
        assert self.client.get(self.base + str(result.data['id']) + '/').data['manifest'] == result.data['manifest']
        assert 'answer' not in str(self.client.get(self.base).data)

    def test_original_key_replay_and_conflict(self):
        key = uuid4()
        body = {'kind': 'reference', 'submission_id': self.first.pk}
        result = self.create(body, key)
        assert result.status_code == 201
        replay = self.create(body, key)
        assert replay.status_code == 200 and replay.data['id'] == result.data['id'] and replay.data['replayed']
        assert self.create({'kind': 'reference', 'submission_id': self.second.pk}, key).status_code == 409
        assert ClassificationGoldEvidence.objects.count() == 1

    def test_default_disabled_nonmanager_cross_project_and_revocation(self):
        with self.settings(ANNOTATION_CLASSIFICATION_GOLD_ENABLED=False):
            assert self.client.get(self.base).status_code == 404
        ref = self.reference()
        for user in (self.worker, self.reviewer):
            self.client.force_authenticate(user)
            assert self.client.get(self.base).status_code == 404
            assert self.client.get(self.base + str(ref) + '/').status_code == 404
            assert self.create({'kind': 'reference', 'submission_id': self.first.pk}).status_code == 404
        self.client.force_authenticate(self.manager)
        other_base = f'/api/projects/{self.other.pk}/classification-disagreements/gold/{ref}/'
        assert self.client.get(other_base).status_code == 404
        with self.settings(ANNOTATION_CLASSIFICATION_GOLD_PROJECTS={}):
            assert self.client.get(self.base + str(ref) + '/').status_code == 404

    def test_rejects_invalid_source_cancelled_assignment_and_forged_fields(self):
        assert self.create({'kind': 'reference', 'submission_id': self.first.pk, 'actor_id': self.manager.pk}).status_code == 400
        self.first.assignment.cancel()
        assert self.create({'kind': 'reference', 'submission_id': self.first.pk}).status_code == 409
        assert not ClassificationGoldEvidence.objects.exists()

    def test_reference_and_candidate_must_be_independent_and_identical_task(self):
        ref = self.reference()
        assert self.create({'kind': 'score', 'reference_id': ref, 'submission_id': self.first.pk}).status_code == 409
        unrelated = self.make_source()
        assert self.create({'kind': 'score', 'reference_id': ref, 'submission_id': unrelated.pk}).status_code == 409

    def test_version_and_exact_revision_remain_frozen_after_new_submission(self):
        ref = self.reference()
        prior = self.create({'kind': 'score', 'reference_id': ref, 'submission_id': self.second.pk})
        assert prior.status_code == 201
        newer = self.make_source(worker=self.reviewer, assignment=self.second.assignment, revision=2)
        self.set_answer(newer, 'Negative')
        current = self.create({'kind': 'score', 'reference_id': ref, 'submission_id': newer.pk})
        assert current.status_code == 201
        assert current.data['manifest']['contents']['source']['revision'] == 2
        assert current.data['manifest']['contents']['result']['matched_controls'] == 0
        assert self.client.get(self.base + str(prior.data['id']) + '/').data['manifest'] == prior.data['manifest']
        next_reference = self.create({'kind': 'reference', 'submission_id': newer.pk})
        assert next_reference.status_code == 201 and next_reference.data['id'] != ref
        assert self.client.get(self.base + str(ref) + '/').data['manifest']['contents']['source']['revision'] == 1

    def test_changed_scope_or_corrupt_snapshot_rejected(self):
        ref = self.reference()
        assert self.create({'kind': 'reference', 'submission_id': self.second.pk,
                            'expected_result_hash': 'a' * 64}).status_code == 409
        assert self.create({'kind': 'score', 'reference_id': ref, 'submission_id': self.second.pk,
                            'expected_reference_hash': 'a' * 64}).status_code == 409
        self.set_answer(self.second, 'Negative', lambda snapshot: snapshot['task']['data'].update(text='changed'))
        assert self.create({'kind': 'score', 'reference_id': ref, 'submission_id': self.second.pk}).status_code == 409
        Submission.objects.filter(pk=self.second.pk).update(result_hash='0' * 64)
        assert self.create({'kind': 'reference', 'submission_id': self.second.pk}).status_code == 409

    def test_oversized_snapshot_is_rejected_before_hydration(self):
        self.set_answer(self.first, 'Positive', lambda snapshot: snapshot['task'].update(meta={'padding': 'x' * 300000}))
        response = self.create({'kind': 'reference', 'submission_id': self.first.pk})
        assert response.status_code == 409 and not ClassificationGoldEvidence.objects.exists()

    def test_malformed_annotation_is_rejected_without_server_error(self):
        self.set_answer(self.first, 'Positive', lambda snapshot: snapshot.update(annotation=[]))
        assert self.create({'kind': 'reference', 'submission_id': self.first.pk}).status_code == 409

    def test_sources_endpoint_is_manager_only(self):
        url = self.base + f'tasks/{self.first.assignment.task_id}/sources/'
        result = self.client.get(url)
        assert result.status_code == 200 and len(result.data['sources']) == 2
        assert self.client.post(url, {}, format='json').status_code == 405
        self.client.force_authenticate(self.worker)
        assert self.client.get(url).status_code == 404

    def test_membership_revocation_blocks_existing_reference_and_replay(self):
        manager = UserFactory(active_organization=self.org)
        self.org.add_user(manager)
        member = ProjectMember.objects.create(project=self.project, user=manager, role='manager')
        self.client.force_authenticate(manager)
        key = uuid4()
        body = {'kind': 'reference', 'submission_id': self.first.pk}
        response = self.create(body, key)
        assert response.status_code == 201
        member.enabled = False
        member.save(update_fields=['enabled'])
        assert self.client.get(self.base + str(response.data['id']) + '/').status_code == 404
        assert self.create(body, key).status_code == 404

    def test_database_rejects_modifying_evidence(self):
        ref = self.reference()
        with self.assertRaises(DatabaseError), transaction.atomic():
            ClassificationGoldEvidence.objects.filter(pk=ref).update(manifest_hash='0' * 64)


@override_settings(ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=True, ANNOTATION_CLASSIFICATION_GOLD_ENABLED=True)
class TestGoldConcurrency(APITransactionTestCase):
    make_source = fixtures.TestClassification.make_source
    set_answer = fixtures.TestClassification.set_answer

    def setUp(self):
        fixtures.TestClassification.setUpTestData.__func__(self)
        fixtures.TestClassification.setUp(self)
        self.gold_mode = self.settings(ANNOTATION_CLASSIFICATION_GOLD_PROJECTS={f'{self.org.pk}:{self.project.pk}': True})
        self.gold_mode.enable()
        self.addCleanup(self.gold_mode.disable)

    def test_concurrent_replay_creates_one_reference(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL row-lock semantics required')
        barrier, key = Barrier(2), uuid4()
        url = f'/api/projects/{self.project.pk}/classification-disagreements/gold/'
        body = {'kind': 'reference', 'submission_id': self.first.pk, 'expected_result_hash': self.first.result_hash}

        def call():
            close_old_connections()
            client = APIClient()
            client.force_authenticate(self.manager)
            barrier.wait(timeout=15)
            response = client.post(url, body, format='json', HTTP_IDEMPOTENCY_KEY=str(key))
            close_old_connections()
            return response.status_code, response.data['id']

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: call(), range(2)))
        assert sorted(status for status, _ in outcomes) == [200, 201]
        assert outcomes[0][1] == outcomes[1][1] and ClassificationGoldEvidence.objects.count() == 1
