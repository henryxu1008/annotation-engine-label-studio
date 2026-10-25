import copy
from unittest.mock import patch
from uuid import uuid4

from classification_disagreements import batches
from classification_disagreements.models import ClassificationBatch
from classification_disagreements.tests import test_comparison as fixtures
from django.db import DatabaseError, transaction
from django.test import override_settings
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APITestCase
from review_batches import priority, service
from review_batches.models import ReviewBatch
from tasks.models import ReviewDecision, Submission
from tasks.submissions import _snapshot_hash


@override_settings(
    ANNOTATION_REVIEW_BATCHES_ENABLED=True,
    ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=True,
    ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=True,
)
class TestPriority(APITestCase):
    setUpTestData = classmethod(fixtures.TestClassification.setUpTestData.__func__)
    make_source = fixtures.TestClassification.make_source
    set_answer = fixtures.TestClassification.set_answer

    def setUp(self):
        fixtures.TestClassification.setUp(self)
        mode = self.settings(ANNOTATION_REVIEW_BATCH_MODES={f'{self.org.pk}:{self.project.pk}': 'fork_review'})
        mode.enable()
        self.addCleanup(mode.disable)
        self.set_answer(self.second, 'Negative')
        self.analysis = self.freeze_analysis()
        self.base = f'/api/projects/{self.project.pk}/review-batches/'
        self.args = {
            'name': 'Priority 优先',
            'rate': 50,
            'seed': 'fixed',
            'priority': {
                'kind': 'classification_conflict',
                'batch_id': self.analysis['id'],
                'manifest_hash': self.analysis['manifest_hash'],
            },
        }

    def freeze_analysis(self):
        base = f'/api/projects/{self.project.pk}/classification-disagreements/batches/'
        preview = self.client.post(
            base + 'preview/',
            {'name': 'Frozen', 'scope': {'kind': 'tasks', 'task_ids': [self.first.assignment.task_id]}},
            format='json',
        )
        assert preview.status_code == 200, preview.data
        created = self.client.post(
            base,
            {k: preview.data[k] for k in ('preview_token',)} | {'expected_plan_hash': preview.data['plan_hash']},
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        )
        assert created.status_code == 201, created.data
        return created.data

    def preview(self, args=None):
        return self.client.post(self.base + 'preview/', args or self.args, format='json')

    def create(self, body=None, key=None):
        if body is None:
            preview = self.preview()
            assert preview.status_code == 200, preview.data
            body = {**self.args, 'expected_plan_hash': preview.data['plan_hash']}
        return self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=str(key or uuid4()))

    def test_conflicts_rank_before_unknown_with_seeded_ties_and_fixed_quota(self):
        self.make_source()
        self.args['rate'] = 33
        before = list(Submission.objects.order_by('pk').values())
        first, repeated = self.preview(), self.preview()
        assert first.status_code == 200, first.data
        assert first.data == repeated.data
        plan = first.data['plan']
        assert plan['algorithm'] == priority.ALGORITHM
        candidates = plan['candidates']
        expected = min(
            [service.identity(self.first), service.identity(self.second)],
            key=lambda c: (service.digest([priority.ALGORITHM, self.project.pk, 'fixed', c]), c['submission_id']),
        )
        assert [c['submission_id'] for c in candidates if c['selected']] == [
            expected['submission_id'],
        ]
        assert priority.summary(plan, candidates) == {
            'conflict_candidates': 2,
            'selected_conflicts': 1,
            'unknown_candidates': 1,
        }
        created = self.create()
        assert created.status_code == 201, created.data
        assert list(Submission.objects.order_by('pk').values()) == before
        assert not ReviewDecision.objects.exists()
        assert 'choices' not in str(plan['priority']) and 'Synthetic' not in str(plan['priority'])

    def test_per_submitter_rounding_retains_quota_and_frozen_evidence(self):
        self.args.update(rate=1, per_submitter=True)
        plan = self.preview().data['plan']
        assert sum(c['selected'] for c in plan['candidates']) == 2
        batch = self.create().data['id']
        old = ReviewBatch.objects.get(pk=batch).plan
        self.make_source(assignment=self.first.assignment, revision=2)
        assert ReviewBatch.objects.get(pk=batch).plan == old

    def test_new_revision_is_unknown_and_never_borrows_old_conflict(self):
        newer = self.make_source(assignment=self.first.assignment, revision=2)
        plan = self.preview().data['plan']
        evidence = {e['submission_id']: e for e in plan['priority']['evidence']}
        assert evidence[newer.pk]['reason'] == 'revision_mismatch'
        assert evidence[newer.pk]['state'] == 'unknown'
        assert [c['submission_id'] for c in plan['candidates'] if c['selected']] == [self.second.pk]

    def test_changed_preview_rejects_without_partial_batch(self):
        preview = self.preview()
        body = {**self.args, 'expected_plan_hash': preview.data['plan_hash']}
        self.make_source(assignment=self.first.assignment, revision=2)
        assert self.create(body).status_code == 409
        assert not ReviewBatch.objects.exists()

    def test_bad_manifest_foreign_batch_corruption_and_deleted_source_reject(self):
        for change in ({'manifest_hash': '0' * 64}, {'batch_id': 999999}):
            response = self.preview({**self.args, 'priority': {**self.args['priority'], **change}})
            assert response.status_code in (404, 409)
        Submission.objects.filter(pk=self.second.pk).update(result_hash='0' * 64)
        assert self.preview().status_code == 409
        self.second.delete()
        ClassificationBatch.objects.filter(pk=self.analysis['id']).delete()
        assert self.preview().status_code == 409
        assert not ReviewBatch.objects.exists()

    def test_receipt_recovers_original_after_source_loss_and_key_rotation(self):
        preview = self.preview()
        body, key = {**self.args, 'expected_plan_hash': preview.data['plan_hash']}, uuid4()
        created = self.create(body, key)
        assert created.status_code == 201, created.data
        self.second.delete()
        ClassificationBatch.objects.filter(pk=self.analysis['id']).delete()
        with override_settings(SECRET_KEY='rotated-synthetic-only'):
            replay = self.create(body, key)
        assert replay.status_code == 200 and replay.data['id'] == created.data['id']
        assert replay.data['replayed'] and replay.data['idempotency_key'] == str(key)
        assert self.create({**body, 'seed': 'other'}, key).status_code == 409
        assert ReviewBatch.objects.count() == 1

    def test_feature_modes_roles_policy_and_nested_actor_cannot_expand_authority(self):
        for settings in (
            {'ANNOTATION_CLASSIFICATION_BATCHES_ENABLED': False},
            {'ANNOTATION_CLASSIFICATION_DISAGREEMENT_MODES': {}},
            {'ANNOTATION_REVIEW_BATCH_MODES': {}},
            {'ANNOTATION_PROJECT_OPERATION_PROVIDER': 'review_batches.tests.test_batches.ReadOnlyPolicy'},
        ):
            with override_settings(**settings):
                assert self.preview().status_code in (403, 404)
        invalid = {**self.args, 'priority': {**self.args['priority'], 'actor_id': self.manager.pk}}
        assert self.preview(invalid).status_code == 400
        for actor in (self.worker, self.reviewer):
            self.client.force_authenticate(actor)
            assert self.preview().status_code in (403, 404)

    def test_reviewer_projection_excludes_self_and_does_not_expose_analysis_manifest(self):
        self.args['rate'] = 100
        created = self.create()
        self.client.force_authenticate(self.reviewer)
        detail = self.client.get(f'{self.base}{created.data["id"]}/')
        assert detail.status_code == 200, detail.data
        assert 'priority_source' not in detail.data
        assert detail.data['priority_summary']['conflict_candidates'] == 0
        assert self.analysis['manifest_hash'] not in str(detail.data)
        assert detail.data['items'] == []
        queue = self.client.get(f'{self.base}{created.data["id"]}/queue/')
        assert queue.status_code == 200 and queue.data['count'] == 0
        result = self.client.post(
            f'/api/submissions/{self.first.pk}/review/?review_batch={created.data["id"]}',
            {'decision': 'approved'},
            format='json',
        )
        assert result.status_code == 403 and not ReviewDecision.objects.exists()
        assert self.client.get(f'{self.base}{created.data["id"]}/rework/{self.first.pk}/').status_code == 404
        assert self.client.get(f'/api/submissions/{self.first.pk}/').status_code == 200

    def test_independent_reviewer_keeps_visible_priority_without_full_origin(self):
        from projects.models import ProjectMember
        from users.tests.factories import UserFactory

        reviewer = UserFactory(active_organization=self.org)
        ProjectMember.objects.create(project=self.project, user=reviewer, role='reviewer')
        created = self.create()
        self.client.force_authenticate(reviewer)
        detail = self.client.get(f'{self.base}{created.data["id"]}/')
        assert detail.status_code == 200 and detail.data['candidate_count'] == 2
        assert 'priority_source' not in detail.data
        assert detail.data['priority_summary']['conflict_candidates'] == 2

    def test_legacy_plan_request_and_projection_stay_compatible(self):
        args = {k: v for k, v in self.args.items() if k != 'priority'}
        preview = self.preview(args)
        assert preview.data['plan']['algorithm'] == service.ALGORITHM
        assert 'priority' not in preview.data['plan']
        body = {**args, 'expected_plan_hash': preview.data['plan_hash']}
        key = uuid4()
        created = self.create(body, key)
        old_plan = ReviewBatch.objects.get(pk=created.data['id']).plan
        self.create()
        assert self.create(body, key).data['id'] == created.data['id']
        assert ReviewBatch.objects.get(pk=created.data['id']).plan == old_plan
        assert 'priority_summary' not in self.client.get(f'{self.base}{created.data["id"]}/').data

    def test_inclusive_byte_budget_and_one_byte_excess_reject_before_partial_write(self):
        used = sum(len(batches.canonical(s.result_snapshot)) for s in (self.first, self.second))
        with patch.object(batches, 'MAX_BYTES', used):
            assert self.preview().status_code == 200
        with patch.object(batches, 'MAX_BYTES', used - 1):
            assert self.preview().status_code == 422
        with patch.object(service, 'MAX_CANDIDATES', 2):
            assert self.preview().status_code == 200
            self.make_source()
            assert self.preview().status_code == 422
        assert not ReviewBatch.objects.exists()

    def test_render_time_revocation_rolls_back_creation_and_preserves_key_recovery(self):
        body = {**self.args, 'expected_plan_hash': self.preview().data['plan_hash']}
        original = JSONRenderer.render

        def revoke(renderer, data, *args, **kwargs):
            result = original(renderer, data, *args, **kwargs)
            if isinstance(data, dict) and 'replayed' in data:
                from projects.models import Project

                Project.objects.filter(pk=self.project.pk).update(created_by=self.worker)
            return result

        key = uuid4()
        with patch.object(JSONRenderer, 'render', revoke):
            assert self.create(body, key).status_code in (403, 404)
        assert not ReviewBatch.objects.exists()
        assert self.create(body, key).status_code == 201

    def test_actual_eight_mib_and_one_byte_excess(self):
        snapshot = copy.deepcopy(self.first.result_snapshot)
        snapshot['task']['meta'] = {'padding': ''}
        used = len(batches.canonical(snapshot)) + len(batches.canonical(self.second.result_snapshot))
        snapshot['task']['meta']['padding'] = 'x' * (batches.MAX_BYTES - used)
        Submission.objects.filter(pk=self.first.pk).update(
            result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
        )
        self.first.refresh_from_db()
        analysis = self.freeze_analysis()
        self.args['priority'].update(batch_id=analysis['id'], manifest_hash=analysis['manifest_hash'])
        assert self.preview().status_code == 200
        snapshot['task']['meta']['padding'] += 'x'
        Submission.objects.filter(pk=self.first.pk).update(
            result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
        )
        assert self.preview().status_code == 422
        assert not ReviewBatch.objects.exists()

    def test_actual_thousand_candidates_and_one_excess(self):
        for _ in range(998):
            self.make_source()
        result = self.preview()
        assert result.status_code == 200, result.data
        assert len(result.data['plan']['candidates']) == 1000
        self.make_source()
        assert self.preview().status_code == 422
        assert not ReviewBatch.objects.exists()

    def test_rendered_evidence_tampering_rolls_back_new_batch(self):
        body = {**self.args, 'expected_plan_hash': self.preview().data['plan_hash']}
        original = JSONRenderer.render

        def tamper(renderer, data, *args, **kwargs):
            if isinstance(data, dict) and 'replayed' in data:
                data['plan_hash'] = '0' * 64
            return original(renderer, data, *args, **kwargs)

        with patch.object(JSONRenderer, 'render', tamper):
            assert self.create(body).status_code == 409
        assert not ReviewBatch.objects.exists()

    def test_new_frozen_evidence_is_guarded_against_direct_orm_rewrite(self):
        created = self.create()
        batch = ReviewBatch.objects.get(pk=created.data['id'])
        changed = copy.deepcopy(batch.plan)
        changed['priority']['evidence'][0]['reason'] = 'not_covered'
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                ReviewBatch.objects.filter(pk=batch.pk).update(plan=changed, plan_hash=service.digest(changed))
        assert ReviewBatch.objects.get(pk=batch.pk).plan == batch.plan

    def test_classification_gate_withdrawal_denies_new_reads_and_recovery_but_not_legacy(self):
        preview = self.preview()
        body, key = {**self.args, 'expected_plan_hash': preview.data['plan_hash']}, uuid4()
        created = self.create(body, key)
        args = {k: v for k, v in self.args.items() if k != 'priority'}
        legacy = self.create({**args, 'expected_plan_hash': self.preview(args).data['plan_hash']})
        with override_settings(
            ANNOTATION_CLASSIFICATION_DISAGREEMENT_MODES={f'{self.org.pk}:{self.project.pk}': 'core_only_internal'}
        ):
            assert self.client.get(f'{self.base}{created.data["id"]}/').status_code in (403, 404)
            assert self.create(body, key).status_code in (403, 404)
            assert self.client.get(f'{self.base}{legacy.data["id"]}/').status_code == 200

    def test_same_role_mapped_actor_drift_discards_new_read_and_viewer_pins_differ(self):
        from access_control.project_operations import local_principal
        from projects.models import ProjectMember
        from users.tests.factories import UserFactory

        reviewers = [UserFactory(active_organization=self.org) for _ in range(2)]
        for actor in reviewers:
            ProjectMember.objects.create(project=self.project, user=actor, role='reviewer')
        created = self.create()
        pins = []
        for actor in reviewers:
            self.client.force_authenticate(actor)
            capability = self.client.get(self.base + 'capability/').data
            detail = self.client.get(f'{self.base}{created.data["id"]}/').data
            queue = self.client.get(f'{self.base}{created.data["id"]}/queue/').data
            assert capability['viewer_hash'] == detail['viewer_hash'] == queue['viewer_hash']
            pins.append(detail['viewer_hash'])
        assert pins[0] != pins[1]
        self.client.force_authenticate(reviewers[0])
        principal = local_principal(reviewers[0])
        with patch.object(priority, 'resolve_actor', return_value=(principal, reviewers[1])):
            assert self.client.get(f'{self.base}{created.data["id"]}/').status_code == 403
            assert self.client.get(f'{self.base}{created.data["id"]}/queue/').status_code == 403
