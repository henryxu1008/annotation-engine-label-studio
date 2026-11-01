"""Independent PostgreSQL connections over the field handoff project fence."""
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from access_control.project_operations import local_principal
from classification_disagreements import fields
from classification_disagreements.models import FieldArbitrationTask, FieldBatch
from classification_disagreements.tests import test_field_arbitration_concurrency as concurrent
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import Project
from rest_framework.response import Response
from rest_framework.test import APIClient
from review_batches import field_handoff as h
from review_batches.models import FieldReviewArbitrationHandoff
from tasks.models import ReviewDecision, Submission

from . import test_field_priority as fixtures
from .test_field_handoff import FieldHandoffFixtures


@override_settings(
    ANNOTATION_REVIEW_BATCHES_ENABLED=True,
    ANNOTATION_FIELD_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_FIELD_BATCHES_ENABLED=True,
    ANNOTATION_FIELD_REVIEW_PRIORITY_ENABLED=True,
    ANNOTATION_FIELD_ARBITRATION_ENABLED=True,
    ANNOTATION_FIELD_REVIEW_ARBITRATION_HANDOFF_ENABLED=True,
)
class TestFieldHandoffConcurrency(FieldHandoffFixtures, TransactionTestCase):
    client_class = APIClient
    client_for = concurrent.TestFieldArbitrationConcurrency.client_for
    post = concurrent.TestFieldArbitrationConcurrency.post
    competing = concurrent.TestFieldArbitrationConcurrency.competing

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Independent PostgreSQL connections required')
        fixtures.TestFieldPriority.setUpTestData.__func__(self)
        FieldHandoffFixtures.setUp(self)

    def pending(self):
        result = self.standalone()
        assert result.status_code == 201, result.data
        return FieldArbitrationTask.objects.get(pk=result.data['id'])

    def command_body(self, row, cancel=False):
        value = {
            'expected_version': row.version,
            'expected_input_set_hash': row.input_set_hash,
            'reason': 'Frozen field',
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
        assert FieldReviewArbitrationHandoff.objects.count() == FieldArbitrationTask.objects.count() == 1

    def test_different_parent_keys_same_input_cannot_duplicate_target(self):
        value = self.handoff_body()
        results = self.competing(
            self.manager, self.manager, self.handoff_url, self.handoff_url, value, value, uuid4(), uuid4()
        )
        assert [r.status_code for r in results] == [201, 409]
        assert FieldReviewArbitrationHandoff.objects.count() == FieldArbitrationTask.objects.count() == 1

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
            f'/api/projects/{self.project.pk}/field-arbitrations/',
            self.handoff_url,
            self.standalone_body(),
            value,
            key,
            key,
        )
        assert [r.status_code for r in results] == [201, 409]
        assert FieldArbitrationTask.objects.count() == 1 and not FieldReviewArbitrationHandoff.objects.exists()

    def test_handoff_wins_and_child_key_is_separate(self):
        value, key = self.handoff_body(), uuid4()
        results = self.competing(
            self.manager,
            self.manager,
            self.handoff_url,
            f'/api/projects/{self.project.pk}/field-arbitrations/',
            value,
            self.standalone_body(),
            key,
            key,
        )
        assert [r.status_code for r in results] == [201, 409]
        assert FieldArbitrationTask.objects.get().idempotency_key != key

    def test_duplicate_link_keys_cannot_duplicate_binding(self):
        target = self.pending()
        value = self.handoff_body(target)
        results = self.competing(
            self.manager, self.manager, self.handoff_url, self.handoff_url, value, value, uuid4(), uuid4()
        )
        assert [r.status_code for r in results] == [201, 409]
        assert FieldReviewArbitrationHandoff.objects.count() == 1

    def transition_race(self, action, first):
        target = self.pending()
        value = self.handoff_body(target)
        path = f'/api/projects/{self.project.pk}/field-arbitrations/{target.pk}/{action}/'
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
            assert not FieldReviewArbitrationHandoff.objects.exists()
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
            assert FieldReviewArbitrationHandoff.objects.get().origin['target']['version_at_link'] == 1

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
        assert FieldArbitrationTask.objects.count() == FieldReviewArbitrationHandoff.objects.count() == 1

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
        assert not FieldArbitrationTask.objects.exists() and not FieldReviewArbitrationHandoff.objects.exists()

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
        assert FieldArbitrationTask.objects.get().status == 'pending'

    def test_cached_metadata_read_is_upgraded_to_a_real_lock(self):
        attempted, acquired = threading.Event(), threading.Event()

        def other_locker():
            close_old_connections()
            try:
                with transaction.atomic():
                    attempted.set()
                    FieldBatch.objects.select_for_update().values('pk').get(pk=self.analysis['id'])
                    acquired.set()
            finally:
                connection.close()

        context = (local_principal(self.manager), self.manager, self.project, 'manager')
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                budget = h.Metadata()
                h.analysis(context, self.analysis['id'], budget)
                h.analysis(context, self.analysis['id'], budget, lock=True)
                waiting = pool.submit(other_locker)
                assert attempted.wait(10)
                assert not acquired.wait(0.15), 'Cached metadata did not acquire its row lock'
            waiting.result(10)
        assert acquired.is_set()

    def test_context_rechecks_sources_after_last_equality_on_an_independent_connection(self):
        actual, calls = fields.equal, []

        def mutate():
            close_old_connections()
            try:
                Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:

            def equality(left, right):
                result = actual(left, right)
                calls.append(1)
                if len(calls) == 12:
                    pool.submit(mutate).result(10)
                return result

            with patch.object(fields, 'equal', equality):
                response = self.client.get(self.context_url)
        assert len(calls) == 12
        assert response.status_code == 409, response.data
        assert not FieldReviewArbitrationHandoff.objects.exists()

    def test_provenance_discards_a_manifest_deleted_during_render_on_an_independent_connection(self):
        saved = self.handoff()
        assert saved.status_code == 201, saved.data
        render = Response.render
        changed = []

        def mutate():
            close_old_connections()
            try:
                FieldBatch.objects.filter(pk=self.analysis['id']).delete()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:

            def rendering(response):
                value = render(response)
                if response.data.get('provenance_status') == 'verified' and not changed:
                    changed.append(1)
                    pool.submit(mutate).result(10)
                return value

            with patch.object(Response, 'render', rendering), patch.object(
                fields, 'normalize', side_effect=AssertionError('Provenance must not calculate')
            ):
                response = self.client.get(
                    f'{self.base}{self.batch.pk}/field-arbitration-handoffs/{saved.data["id"]}/provenance/'
                )
        assert changed == [1]
        assert response.status_code == 409, response.data
