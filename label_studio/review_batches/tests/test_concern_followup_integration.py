"""Reuse frozen strategies and actual #79/#80 generation, without new effects."""

import copy
from unittest.mock import patch
from uuid import uuid4

from access_control.project_operations import ORGANIZE_CLASSIFICATION
from django.db.models import JSONField
from projects.models import ProjectMember
from rest_framework.test import APITestCase
from review_batches import service
from review_batches.models import ReviewConfidenceObservation, SecondReviewTask
from tasks.models import Submission, TaskAssignment
from tasks.submissions import _snapshot_hash, review_submission

from . import test_second_review_integration as existing_integration
from .second_review_fixtures import OPERATIONS
from .test_concern_followup import ConcernFixtures


class TestConcernFollowupIntegration(ConcernFixtures, APITestCase):
    def setUp(self):
        self.followup_setup()

    def test_A08_existing_owner_editor_and_mywork_rework_create_only_normal_revision(self):
        from review_batches.models import ReviewBatch

        for initial in ('approved', 'rejected'):
            source, batch, task = self.concerned(decision=initial)
            frozen = ReviewBatch.objects.get(pk=batch).plan
            self.client.force_authenticate(self.worker)
            work = self.client.get(f'/api/projects/{self.project.pk}/my-work/').data
            item = next(v for v in work['results'] if v['assignment_id'] == source.assignment_id)
            assert item['state'] == ('rework' if initial == 'rejected' else 'approved')
            source.assignment.refresh_from_db()
            response = self.client.patch(
                f'/api/annotations/{source.annotation_id}/',
                dict(
                    result=source.annotation.result,
                    assignment_id=source.assignment_id,
                    assignment_version=source.assignment.version,
                    submit_for_review=True,
                ),
                format='json',
            )
            assert response.status_code == 200, response.data
            target = Submission.objects.get(assignment_id=source.assignment_id, revision=source.revision + 1)
            assert target.status == 'pending' and target.submitted_by_id == self.worker.pk
            assert self.record_followup(self.followup_body(task, target)).status_code == 201
            source.refresh_from_db()
            assert source.status == initial and ReviewBatch.objects.get(pk=batch).plan == frozen

    def test_B02_target_initial_reviewer_is_an_additional_human_exclusion(self):
        source, batch, task = self.concerned()
        ProjectMember.objects.filter(project=self.project, user=self.producer).update(role='reviewer')
        target = self.successor(source, decision='approved', reviewer=self.producer)
        ProjectMember.objects.filter(project=self.project, user=self.producer).update(role='manager')
        assert self.followup_context(task, target, actor=self.producer).status_code == 403
        assert self.record_followup(self.followup_body(task, target)).status_code == 201

    def test_E08_actual_thousand_candidate_batch_reads_only_exact_subject_and_no_replay_origin(self):
        from django.db.models import JSONField
        from review_batches import followup_origin, second_review_service
        from review_batches.models import ReviewBatch
        from review_batches.second_review_bounded import Budget

        from . import test_confidence_budgets as existing_budgets

        self.source = self.make_source()
        others = existing_budgets.TestConfidenceBudgets.bulk_sources(self, 999)
        args = dict(name='Thousand exact frozen candidates', rate=100, seed='fixed')
        preview = self.preview(args)
        assert preview.status_code == 200, preview.data
        assert len(preview.data['plan']['candidates']) == 1000
        saved = self.create_batch({**args, 'expected_plan_hash': preview.data['plan_hash']})
        assert saved.status_code == 201, saved.data
        review_submission(submission=self.source, reviewer=self.reviewer, decision='approved')
        dispatched, _ = self.dispatch(self.source, saved.data['id'])
        task = dispatched.data['receipt']['task_id']
        assert self.decide(task, self.opinion_body(task, 'concern', 'Concern on exact subject')).status_code == 201
        marker = 'NEVER_OTHER_THOUSAND_SNAPSHOT'
        Submission.objects.filter(pk__in=[v.pk for v in others]).update(result_snapshot={marker: True})
        Submission.objects.filter(pk=others[0].pk).update(result_snapshot={marker: 'x' * (2 * 1024 * 1024)})
        decoder, decoded, snapshots, rows = JSONField.from_db_value, [], [], Budget.rows

        def decode(field, value, *args):
            if value and marker in str(value):
                decoded.append(True)
            return decoder(field, value, *args)

        def read(budget, query, limits, **kwargs):
            values = rows(budget, query, limits, **kwargs)
            if query.model is Submission and 'result_snapshot' in limits:
                snapshots.extend(v.pk for v in values)
            return values

        with (
            patch.object(JSONField, 'from_db_value', decode),
            patch.object(Budget, 'rows', read),
            patch.object(service, 'read_sources', side_effect=AssertionError('no whole batch')),
        ):
            body = self.followup_body(task)
            response = self.record_followup(body, key=uuid4())
        assert response.status_code == 201, response.data
        assert len(ReviewBatch.objects.get(pk=saved.data['id']).plan['candidates']) == 1000
        assert snapshots == [self.source.pk] * 4 and not decoded
        key = response.data['receipt']['key']
        with (
            patch.object(followup_origin, 'load', side_effect=AssertionError('no source on replay')),
            patch.object(second_review_service, 'task', side_effect=AssertionError('no old task on replay')),
        ):
            recovered = self.record_followup(body, key=key)
        assert recovered.status_code == 200 and recovered.data['receipt'] == response.data['receipt']

    def test_A06_retired_confidence_is_historical_without_new_producer_or_scoring(self):
        source, batch, task = self.concerned(confidence=True)
        count = ReviewConfidenceObservation.objects.count()
        retired = [{**entry, 'fresh': False} for entry in self.entries]
        with (
            self.settings(ANNOTATION_REVIEW_CONFIDENCE_PROFILES=retired, ANNOTATION_REVIEW_CONFIDENCE_GRANTS=[]),
            patch.object(service, 'build_plan', side_effect=AssertionError('no fresh confidence')),
            patch.object(service, 'read_sources', side_effect=AssertionError('no whole batch')),
        ):
            body = self.followup_body(task)
            assert self.record_followup(body).status_code == 201
        assert ReviewConfidenceObservation.objects.count() == count

    def test_C03_legacy_float_snapshot_hash_and_browser_presentation_are_distinct(self):
        import hashlib
        import json

        import rfc8785

        make_source = self.make_source

        def with_float(*args, **kwargs):
            row = make_source(*args, **kwargs)
            row.result_snapshot['annotation']['lead_time'] = 1.0
            row.result_hash = _snapshot_hash(row.result_snapshot)
            row.save(update_fields=['result_snapshot', 'result_hash'])
            return row

        with patch.object(self, 'make_source', side_effect=with_float):
            source, batch, task = self.concerned()
        expected_legacy = source.result_hash
        response = self.followup_context(task)
        assert response.status_code == 200, response.data
        projected = response.data['original']
        assert projected['subject']['result_hash'] == expected_legacy
        displayed = json.loads(json.dumps(source.result_snapshot), parse_int=float)
        assert projected['presentation_hash'] == hashlib.sha256(rfc8785.dumps(displayed)).hexdigest()
        assert projected['presentation_hash'] != expected_legacy
        assert self.record_followup(self.followup_body(task)).status_code == 201
        source.refresh_from_db()
        assert source.result_hash == expected_legacy

    def test_A06_B03_E08_classification_cohort_exclusion_precedes_answers(self):
        with self.settings(
            ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=True,
            ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=True,
            ANNOTATION_CLASSIFICATION_DISAGREEMENT_MODES={f'{self.org.pk}:{self.project.pk}': 'fork_review'},
            CONFIDENCE_TEST_OPERATIONS=[*OPERATIONS, 'record_second_review_followup', ORGANIZE_CLASSIFICATION],
        ):
            first, other = self.make_source(), self.make_source(self.second_worker)
            TaskAssignment.objects.filter(pk=other.assignment_id).update(task_id=first.assignment.task_id)
            snap = copy.deepcopy(other.result_snapshot)
            snap['task'] = first.result_snapshot['task']
            snap['annotation']['result'][0]['value']['choices'] = ['B']
            Submission.objects.filter(pk=other.pk).update(result_snapshot=snap, result_hash=_snapshot_hash(snap))
            self.client.force_authenticate(self.manager)
            url = f'/api/projects/{self.project.pk}/classification-disagreements/batches/'
            preview = self.client.post(
                url + 'preview/',
                dict(name='frozen', scope=dict(kind='tasks', task_ids=[first.assignment.task_id])),
                format='json',
            )
            assert preview.status_code == 200, preview.data
            frozen = self.client.post(
                url,
                dict(preview_token=preview.data['preview_token'], expected_plan_hash=preview.data['plan_hash']),
                format='json',
                HTTP_IDEMPOTENCY_KEY=str(uuid4()),
            )
            assert frozen.status_code == 201, frozen.data
            args = dict(
                name='classification concern',
                rate=100,
                seed='fixed',
                priority=dict(
                    kind='classification_conflict',
                    batch_id=frozen.data['id'],
                    manifest_hash=frozen.data['manifest_hash'],
                ),
            )
            planned = self.preview(args)
            batch = self.create_batch({**args, 'expected_plan_hash': planned.data['plan_hash']})
            assert batch.status_code == 201, batch.data
            review_submission(submission=first, reviewer=self.reviewer, decision='approved')
            dispatched, _ = self.dispatch(first, batch.data['id'])
            assert dispatched.status_code == 201, dispatched.data
            task = dispatched.data['receipt']['task_id']
            assert self.decide(task, self.opinion_body(task, 'concern', 'Independent concern')).status_code == 201
            poisoned = {**snap, 'NEVER_OTHER_COHORT': 'x' * (3 * 1024 * 1024)}
            Submission.objects.filter(pk=other.pk).update(
                result_snapshot=poisoned, result_hash=_snapshot_hash(poisoned)
            )
            ProjectMember.objects.filter(project=self.project, user=self.second_worker).update(role='manager')
            decoded, original = [], JSONField.from_db_value

            def spy(field, value, *args):
                if value and 'NEVER_OTHER_COHORT' in str(value):
                    decoded.append(True)
                return original(field, value, *args)

            with (
                patch.object(JSONField, 'from_db_value', spy),
                patch.object(service, 'read_sources', side_effect=AssertionError('no batch')),
            ):
                assert self.followup_context(task, actor=self.second_worker).status_code == 403
                assert self.record_followup(self.followup_body(task)).status_code == 201
            assert not decoded

    def ready_release(self, linked):
        original = self.decide

        def decide_and_follow_up(task, body=None, key=None):
            result = original(task, body, key)
            if result.status_code == 201 and body and body['outcome'] == 'concern':
                source = Submission.objects.get(pk=SecondReviewTask.objects.get(pk=task).submission_id_frozen)
                target = self.successor(source) if linked else None
                response = self.record_followup(self.followup_body(task, target))
                assert response.status_code == 201, response.data
            return result

        with patch.object(self, 'decide', side_effect=decide_and_follow_up):
            existing_integration.TestSecondReviewIntegration.test_F03_F04_ready_release_and_snapshot_bytes_survive_supervisory_concern(
                self
            )

    def test_F07_no_change_preserves_ready_release_snapshot_and_download_bytes(self):
        self.ready_release(False)

    def test_F07_linked_revision_preserves_ready_release_snapshot_and_download_bytes(self):
        self.ready_release(True)
