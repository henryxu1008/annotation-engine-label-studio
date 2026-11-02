import copy
from unittest.mock import patch
from uuid import uuid4

from access_control.project_operations import ORGANIZE_CLASSIFICATION
from django.db import connection
from django.db.models import JSONField
from projects.models import ProjectMember
from rest_framework.test import APITestCase
from review_batches import service
from review_batches.models import SecondReviewTask
from tasks.models import Submission, TaskAssignment
from tasks.submissions import _snapshot_hash, review_submission
from users.tests.factories import UserFactory

from .second_review_fixtures import OPERATIONS, SecondReviewFixtures


class TestSecondReviewIntegration(SecondReviewFixtures, APITestCase):
    def setUp(self):
        self.second_setup()

    def test_A02_B03_classification_cohort_is_excluded_without_hydrating_others(self):
        mode = self.settings(
            ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=True,
            ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=True,
            ANNOTATION_CLASSIFICATION_DISAGREEMENT_MODES={f'{self.org.pk}:{self.project.pk}': 'fork_review'},
            CONFIDENCE_TEST_OPERATIONS=[*OPERATIONS, ORGANIZE_CLASSIFICATION],
        )
        with mode:
            first = self.make_source()
            other = self.make_source(self.second_worker)
            TaskAssignment.objects.filter(pk=other.assignment_id).update(task_id=first.assignment.task_id)
            snap = copy.deepcopy(other.result_snapshot)
            snap['task'] = first.result_snapshot['task']
            snap['annotation']['result'][0]['value']['choices'] = ['B']
            Submission.objects.filter(pk=other.pk).update(result_snapshot=snap, result_hash=_snapshot_hash(snap))
            self.client.force_authenticate(self.manager)
            url = f'/api/projects/{self.project.pk}/classification-disagreements/batches/'
            result = self.client.post(
                url + 'preview/',
                dict(name='frozen cohort', scope=dict(kind='tasks', task_ids=[first.assignment.task_id])),
                format='json',
            )
            assert result.status_code == 200, result.data
            result = self.client.post(
                url,
                dict(preview_token=result.data['preview_token'], expected_plan_hash=result.data['plan_hash']),
                format='json',
                HTTP_IDEMPOTENCY_KEY=str(uuid4()),
            )
            assert result.status_code == 201, result.data
            args = dict(
                name='classification',
                rate=100,
                seed='fixed',
                priority=dict(
                    kind='classification_conflict',
                    batch_id=result.data['id'],
                    manifest_hash=result.data['manifest_hash'],
                ),
            )
            preview = self.preview(args)
            assert preview.status_code == 200, preview.data
            batch = self.create_batch({**args, 'expected_plan_hash': preview.data['plan_hash']})
            assert batch.status_code == 201, batch.data
            review_submission(submission=first, reviewer=self.reviewer, decision='approved', reason='full initial')
            # Freeze selected cohort membership, then make another answer far
            # too large. The new endpoint must not load it or recalculate metrics.
            poisoned = {**snap, 'other-cohort-never-read': 'x' * (3 * 1024 * 1024)}
            Submission.objects.filter(pk=other.pk).update(
                result_snapshot=poisoned, result_hash=_snapshot_hash(poisoned)
            )
            original, decoded = JSONField.from_db_value, []

            def decode(field, value, expression, db):
                if value and 'other-cohort-never-read' in str(value):
                    decoded.append(value)
                return original(field, value, expression, db)

            with patch.object(JSONField, 'from_db_value', decode), patch.object(
                service, 'read_sources', side_effect=AssertionError('no whole batch')
            ), patch.object(service, 'checked_sources', side_effect=AssertionError('no whole batch')):
                result, _ = self.dispatch(first, batch.data['id'])
            assert result.status_code == 201 and not decoded, result.data
            ProjectMember.objects.filter(project=self.project, user=self.second_worker).update(role='reviewer')
            self.client.force_authenticate(self.manager)
            candidates = self.client.get(self.base + f'{batch.data["id"]}/sources/{first.pk}/second-review-assignees/')
            assert candidates.status_code == 200, candidates.data
            assert self.second_worker.pk not in [r['id'] for r in candidates.data['results']]

    def test_E07_large_roster_is_filtered_paged_and_name_only(self):
        source, batch = self.prepared()
        for i in range(101):
            user = UserFactory(active_organization=self.org, first_name=f'Independent-{i}', last_name='候选')
            self.org.add_user(user)
            ProjectMember.objects.create(project=self.project, user=user, role='reviewer')
        self.client.force_authenticate(self.manager)
        url = self.base + f'{batch}/sources/{source.pk}/second-review-assignees/'
        queries = []

        def trace(execute, sql, params, many, context):
            queries.append(sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(trace):
            first = self.client.get(url, {'page_size': 100})
        assert (
            first.status_code == 200 and first.data['count'] == 102 and len(first.data['results']) == 100
        ), first.data
        page = self.client.get(url, {'page_size': 100, 'cursor': first.data['next']})
        assert page.status_code == 200 and len(page.data['results']) == 2 and page.data['next'] is None
        assert not ({v['id'] for v in first.data['results']} & {v['id'] for v in page.data['results']})
        projected = [q for q in queries if '_sr_value_first_name' in q]
        assert projected and all(
            '"password"' not in q and '"avatar"' not in q and '"email"' not in q for q in projected
        )

    def test_E07_multibyte_combined_display_name_is_bounded(self):
        from users.models import User

        User.objects.filter(pk=self.supervisor.pk).update(first_name='中' * 200, last_name='文' * 200)
        source, batch = self.prepared()
        self.client.force_authenticate(self.manager)
        result = self.client.get(self.base + f'{batch}/sources/{source.pk}/second-review-assignees/')
        assert result.status_code == 422

    def test_F01_approved_concern_and_rejected_confirmed_keep_legacy_release_rules(self):
        for initial, outcome, expected in (('approved', 'concern', 200), ('rejected', 'confirmed', 400)):
            source, batch = self.prepared(decision=initial)
            result, _ = self.dispatch(source, batch)
            assert result.status_code == 201
            pk = result.data['receipt']['task_id']
            body = self.opinion_body(pk, outcome, 'Supervision only')
            assert self.decide(pk, body).status_code == 201
            self.client.force_authenticate(self.manager)
            result = self.client.get(f'/api/submissions/{source.pk}/release/')
            assert result.status_code == expected, result.data
            source.refresh_from_db()
            assert source.status == initial

    def test_F02_unchecked_and_pending_never_become_approved(self):
        source = self.make_source()
        self.client.force_authenticate(self.manager)
        before = source.status
        assert self.client.get(f'/api/submissions/{source.pk}/release/').status_code == 400
        source.refresh_from_db()
        assert source.status == before == 'pending' and not SecondReviewTask.objects.exists()

    def test_F06_old_review_and_handoff_do_not_reopen_initial_decision(self):
        source, batch = self.prepared()
        result, _ = self.dispatch(source, batch)
        assert result.status_code == 201
        self.client.force_authenticate(self.reviewer)
        reviewed = self.client.post(
            f'/api/submissions/{source.pk}/review/', dict(decision='rejected', reason='reopen'), format='json'
        )
        assert reviewed.status_code in (400, 403, 409), reviewed.data
        self.client.force_authenticate(self.manager)
        result = self.client.get(self.base + f'{batch}/sources/{source.pk}/arbitration-context/')
        assert result.status_code in (400, 403, 404, 409), result.data
        source.refresh_from_db()
        assert source.status == 'approved'

    def test_F03_F04_ready_release_and_snapshot_bytes_survive_supervisory_concern(self):
        import sys
        import tempfile

        from data_export import approved, snapshots
        from data_export.approved_models import ApprovedDatasetRelease as Release
        from data_export.approved_models import ApprovedDatasetReleaseAttempt as Attempt
        from data_export.snapshot_models import ReleaseSnapshot

        if connection.vendor != 'postgresql' or sys.version_info[:2] != (3, 11):
            self.skipTest('Real #79/#80 PostgreSQL and Python 3.11 runtime required')
        source, batch = self.prepared()
        root = tempfile.TemporaryDirectory(prefix='second-review-release-')
        self.addCleanup(root.cleanup)
        policy = dict(
            source_namespace='00000000-0000-4000-8000-000000000079',
            projects={
                f'{self.org.pk}:{self.project.pk}': dict(mode='fork_approved', version='supervision-regression/v1')
            },
        )
        body = dict(
            items=[dict(submission_id=source.pk, revision=source.revision, result_hash=source.result_hash)],
            output=dict(format='JSONL', resource_mode='references_only'),
            filters={},
        )
        base = f'/api/projects/{self.project.pk}/approved-dataset-releases/'
        with self.settings(APPROVED_EXPORT_ROOT=root.name, APPROVED_EXPORT_POLICY=policy), patch.object(
            approved, 'queue_available', return_value=True
        ):
            self.client.force_authenticate(self.manager)
            created = self.client.post(base, body, format='json', HTTP_IDEMPOTENCY_KEY='supervision-release')
            assert created.status_code == 202, created.data
            release = Release.objects.get(pk=created.data['release_id'])
            release.state, release.attempt_number = 'queued', 1
            release.save(update_fields=['state', 'attempt_number'])
            attempt = Attempt.objects.create(release=release, number=1)
            approved.generate(str(release.pk), str(attempt.token))
            release.refresh_from_db()
            assert release.state == 'ready', release.error_code
            snap_base = base + str(release.pk) + '/snapshots/'
            captured = self.client.post(snap_base, {}, format='json', HTTP_IDEMPOTENCY_KEY='supervision-snapshot')
            assert captured.status_code == 202, captured.data
            snap = ReleaseSnapshot.objects.get(pk=captured.data['snapshot_id'])
            ReleaseSnapshot.objects.filter(pk=snap.pk).update(state='queued')
            snapshots.generate(str(snap.pk), str(snap.token))
            snap.refresh_from_db()
            assert snap.state == 'ready', snap.error_code
            manifest_url = snap_base + str(snap.pk) + '/manifest/'
            before = self.client.get(manifest_url)
            assert before.status_code == 200
            artifact = release.artifacts.first()
            artifact_url = base + str(release.pk) + f'/artifacts/{artifact.pk}/download/'
            content = self.client.get(artifact_url)
            try:
                assert content.status_code == 200
                original_bytes = b''.join(content.streaming_content)
            finally:
                for closer in content._resource_closers:
                    closer()
            dispatched, _ = self.dispatch(source, batch)
            pk = dispatched.data['receipt']['task_id']
            assert self.decide(pk, self.opinion_body(pk, 'concern', 'Independent concern')).status_code == 201
            self.client.force_authenticate(self.manager)
            after = self.client.get(manifest_url)
            assert after.status_code == 200 and after.content == before.content
            content = self.client.get(artifact_url)
            try:
                assert content.status_code == 200 and b''.join(content.streaming_content) == original_bytes
            finally:
                for closer in content._resource_closers:
                    closer()
            assert b'supervisory_only' not in after.content and b'second_review' not in after.content
            assert self.client.get(base + str(release.pk) + '/').status_code == 200
