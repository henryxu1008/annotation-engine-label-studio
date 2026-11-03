"""Independent disposition, never a second approval or release command."""

from unittest.mock import patch
from uuid import uuid4

from django.db import DatabaseError, connection, transaction
from projects.models import ProjectMember
from rest_framework.test import APITestCase
from review_batches import followup_origin, followup_snapshots, second_review_service
from review_batches.models import ReviewBatch, SecondReviewConcernFollowup, SecondReviewTask
from tasks.models import ReviewDecision, Submission
from tasks.submissions import _snapshot_hash, create_submission, review_submission

from .second_review_fixtures import OPERATIONS, SecondReviewFixtures


class ConcernFixtures(SecondReviewFixtures):
    def followup_setup(self):
        self.second_setup()
        mode = self.settings(
            ANNOTATION_SECOND_REVIEW_FOLLOWUP_ENABLED=True,
            CONFIDENCE_TEST_OPERATIONS=[*OPERATIONS, 'record_second_review_followup'],
        )
        mode.enable()
        self.addCleanup(mode.disable)
        self.followup_base = f'/api/projects/{self.project.pk}/second-review-followups/'

    def concerned(self, **kwargs):
        source, batch = self.prepared(**kwargs)
        result, _ = self.dispatch(source, batch)
        assert result.status_code == 201, result.data
        task_id = result.data['receipt']['task_id']
        result = self.decide(task_id, self.opinion_body(task_id, 'concern', 'Inspect exact revision 复查'))
        assert result.status_code == 201, result.data
        return source, batch, task_id

    def followup_context(self, task_id, target=None, actor=None):
        self.client.force_authenticate(actor or self.manager)
        return self.client.get(
            self.followup_base + f'context/{task_id}/',
            {} if target is None else {'related_submission_id': target.pk},
        )

    def followup_body(self, task_id, target=None, actor=None):
        response = self.followup_context(task_id, target, actor=actor)
        assert response.status_code == 200, getattr(response, 'data', response.content[:500])
        return dict(
            second_review_task_id=task_id,
            expected_concern_hash=response.data['concern_hash'],
            expected_context_hash=response.data['context_hash'],
            expected_viewer_hash=response.data['viewer_hash'],
            expected_version=0,
            disposition='revision_linked' if target else 'no_change',
            reason='Independent disposition 独立处置',
            related_submission_id=target.pk if target else None,
        )

    def record_followup(self, body, *, key=None, actor=None):
        self.client.force_authenticate(actor or self.manager)
        return self.client.post(self.followup_base, body, format='json', HTTP_IDEMPOTENCY_KEY=str(key or uuid4()))

    def successor(self, source, *, decision=None, reviewer=None):
        target = create_submission(annotation=source.annotation, assignment=source.assignment, actor=self.worker)
        if decision:
            review_submission(
                submission=target, reviewer=reviewer or self.reviewer, decision=decision, reason='Next review 新审核'
            )
        target.refresh_from_db()
        return target


class TestConcernFollowup(ConcernFixtures, APITestCase):
    def setUp(self):
        self.followup_setup()

    def test_A01_no_change_preserves_initial_and_opinion(self):
        source, batch, task_id = self.concerned()
        before = source.result_hash
        body = self.followup_body(task_id)
        result = self.record_followup(body)
        assert result.status_code == 201, result.data
        source.refresh_from_db()
        assert source.status == 'approved' and source.result_hash == before
        assert result.data['receipt']['effect'] == 'supervisory_only'
        assert result.data['receipt']['disposition'] == 'no_change'

    def test_B01_legacy_operations_cannot_record_or_preview(self):
        source, batch, task_id = self.concerned()
        body = self.followup_body(task_id)
        with self.settings(CONFIDENCE_TEST_OPERATIONS=OPERATIONS):
            assert self.followup_context(task_id).status_code == 403
            assert self.record_followup(body).status_code == 403

    def test_B04_reviewer_direct_collection_denied(self):
        self.client.force_authenticate(self.supervisor)
        assert self.client.get(self.followup_base).status_code in (403, 404)

    def test_A02_rejected_no_change_keeps_rework(self):
        source, batch, task = self.concerned(decision='rejected')
        assert self.record_followup(self.followup_body(task)).status_code == 201
        source.refresh_from_db()
        assert source.status == 'rejected'
        assert self.client.get(f'/api/submissions/{source.pk}/release/').status_code == 400

    def test_A03_C04_existing_successor_each_review_state(self):
        for decision in (None, 'approved', 'rejected'):
            source, batch, task = self.concerned()
            target = self.successor(source, decision=decision)
            context = self.followup_context(task, target)
            assert context.status_code == 200, context.data
            assert context.data['original']['historical_revision']
            assert context.data['target']['subject']['submission_id'] == target.pk
            result = self.record_followup(self.followup_body(task, target))
            assert result.status_code == 201, result.data
            saved = SecondReviewConcernFollowup.objects.get(pk=result.data['receipt']['followup_id'])
            assert saved.target_ref['status_at_record'] == (decision or 'pending')
            source.refresh_from_db()
            assert source.status == 'approved'

    def test_A04_confirmed_pending_cancelled_are_not_concerns(self):
        source, batch = self.prepared()
        result, _ = self.dispatch(source, batch)
        task = result.data['receipt']['task_id']
        assert self.followup_context(task).status_code == 409
        assert self.decide(task, self.opinion_body(task, 'confirmed')).status_code == 201
        assert self.followup_context(task).status_code == 409

    def test_A04_cancelled_second_task_cannot_enter_followup(self):
        source, batch = self.prepared()
        response, _ = self.dispatch(source, batch)
        task = response.data['receipt']['task_id']
        self.client.force_authenticate(self.manager)
        state = self.client.get(self.second_base + task + '/').data
        response = self.client.post(
            self.second_base + task + '/cancel/',
            dict(expected_version=1, expected_viewer_hash=state['viewer_hash'], reason='Cancelled synthetic dispatch'),
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        )
        assert response.status_code == 201, response.data
        assert self.followup_context(task).status_code == 409

    def test_A07_no_second_initial_approval_and_no_concern_arbitration_link(self):
        from rest_framework.exceptions import ValidationError

        source, batch, task = self.concerned()
        assert self.record_followup(self.followup_body(task)).status_code == 201
        with self.assertRaises(ValidationError):
            review_submission(submission=source, reviewer=self.reviewer, decision='approved')
        self.client.force_authenticate(self.manager)
        # There is deliberately no new automatic arbitration command.
        response = self.client.post(self.followup_base + task + '/arbitration/', {}, format='json')
        assert response.status_code == 404

    def test_B01_read_work_is_required_even_with_new_write_operation(self):
        from access_control.project_operations import READ_WORK

        source, batch, task = self.concerned()
        body = self.followup_body(task)
        with self.settings(
            CONFIDENCE_TEST_OPERATIONS=[o for o in OPERATIONS if o != READ_WORK] + ['record_second_review_followup']
        ):
            assert self.followup_context(task).status_code == 403
            assert self.record_followup(body).status_code == 403

    def test_B04_all_new_surfaces_reject_nonmanagers(self):
        source, batch, task = self.concerned()
        body = self.followup_body(task)
        saved = self.record_followup(body)
        pk = saved.data['receipt']['followup_id']
        for actor in (self.worker, self.reviewer, self.supervisor):
            self.client.force_authenticate(actor)
            for path in (
                self.followup_base,
                self.followup_base + pk + '/',
                self.followup_base + 'context/' + task + '/',
            ):
                assert self.client.get(path).status_code in (403, 404)
            assert self.record_followup(body, actor=actor).status_code in (403, 404)

    def test_B10_other_manager_reads_frozen_metadata_without_sources(self):
        source, batch, task = self.concerned()
        saved = self.record_followup(self.followup_body(task))
        ReviewBatch.objects.filter(pk=batch).delete()
        source.delete()
        self.client.force_authenticate(self.producer)
        with patch.object(followup_origin, 'load', side_effect=AssertionError('no origin hydrate')):
            response = self.client.get(self.followup_base + saved.data['receipt']['followup_id'] + '/')
            assert response.status_code == 200, response.data
            assert response.data['source_integrity'] == 'not_checked'
            assert response.data['receipt'] == saved.data['receipt']

    def test_B02_each_original_human_cannot_handle_as_manager(self):
        source, batch, task = self.concerned()
        body = self.followup_body(task)
        for user in (self.worker, self.reviewer, self.supervisor):
            with transaction.atomic():
                ProjectMember.objects.filter(project=self.project, user=user).update(role='manager')
                assert self.followup_context(task, actor=user).status_code == 403
                assert self.record_followup(body, actor=user).status_code == 403
                transaction.set_rollback(True)

    def test_B05_cross_scope_and_bad_cursor_are_rejected(self):
        source, batch, task = self.concerned()
        body = self.followup_body(task)
        body['second_review_task_id'] = str(uuid4())
        assert self.record_followup(body).status_code == 404
        self.client.force_authenticate(self.manager)
        assert self.client.get(self.followup_base, {'cursor': 'forged'}).status_code == 400
        assert self.client.get(self.followup_base, {'actor_id': self.manager.pk}).status_code == 400

    def test_B06_revoke_role_denies_read_write_and_replay(self):
        source, batch, task = self.concerned()
        body, key = self.followup_body(task), uuid4()
        result = self.record_followup(body, key=key)
        assert result.status_code == 201
        # The owner is also the project's creator; disabled current membership
        # must override that historical convenience identity.
        from organizations.models import OrganizationMember

        OrganizationMember.objects.filter(organization=self.org, user=self.manager).update(
            deleted_at=__import__('django.utils.timezone', fromlist=['now']).now()
        )
        assert self.client.get(self.followup_base).status_code in (403, 404)
        assert self.record_followup(body, key=key).status_code in (403, 404)

    def test_B08_off_core_mode_instance_denies_even_recovery(self):
        source, batch, task = self.concerned()
        body, key = self.followup_body(task), uuid4()
        assert self.record_followup(body, key=key).status_code == 201
        for options in (
            {'ANNOTATION_SECOND_REVIEW_FOLLOWUP_ENABLED': False},
            {'ANNOTATION_SECOND_REVIEW_ENABLED': False},
            {'ANNOTATION_SECOND_REVIEW_MODES': {f'{self.org.pk}:{self.project.pk}': 'core_only'}},
            {'ANNOTATION_SECOND_REVIEW_INSTANCE_ID': ''},
        ):
            with self.settings(**options):
                assert self.client.get(self.followup_base).status_code == 403
                assert self.record_followup(body, key=key).status_code == 403

    def test_C05_wrong_chain_and_old_target_do_not_link(self):
        source, batch, task = self.concerned()
        other = self.make_source(self.second_worker)
        for target in (source, other):
            assert self.followup_context(task, target).status_code in (403, 409)

    def test_C06_C07_stale_target_or_inactive_assignment_rejects(self):
        source, batch, task = self.concerned()
        target = self.successor(source)
        body = self.followup_body(task, target)
        newest = self.successor(source)
        assert self.record_followup(body).status_code == 409
        assert self.followup_context(task, target).status_code == 409
        source.assignment.cancel()
        assert self.followup_context(task, newest).status_code == 409

    def test_C08_corrupt_snapshot_or_drifted_config_does_not_return_half_answers(self):
        source, batch, task = self.concerned()
        target = self.successor(source)
        for changed_hash in (False, True):
            snap = {**target.result_snapshot, 'task': {**target.result_snapshot['task'], 'data': {'text': 'changed'}}}
            Submission.objects.filter(pk=target.pk).update(
                result_snapshot=snap, result_hash=_snapshot_hash(snap) if changed_hash else '0' * 64
            )
            response = self.followup_context(task, target)
            assert response.status_code == 409
            assert 'original' not in response.data and 'target' not in response.data

    def test_C09_inconsistent_or_self_reviewed_target_rejected(self):
        source, batch, task = self.concerned()
        target = self.successor(source)
        Submission.objects.filter(pk=target.pk).update(status='approved')
        assert self.followup_context(task, target).status_code == 409
        ReviewDecision.objects.create(submission=target, reviewer=self.worker, decision='approved')
        assert self.followup_context(task, target).status_code == 409

    def test_C10_D04_missing_origin_target_recovery_is_receipt_only(self):
        source, batch, task = self.concerned()
        target = self.successor(source)
        body, key = self.followup_body(task, target), uuid4()
        saved = self.record_followup(body, key=key)
        assert saved.status_code == 201
        ReviewBatch.objects.filter(pk=batch).delete()
        target.delete()
        source.delete()
        with (
            patch.object(followup_origin, 'load', side_effect=AssertionError('no live source')),
            patch.object(followup_origin, 'lock', side_effect=AssertionError('no origin lock')),
            patch.object(second_review_service, 'task', side_effect=AssertionError('no second task')),
            patch.object(followup_snapshots, 'load', side_effect=AssertionError('no snapshot')),
        ):
            replay = self.record_followup(body, key=key)
            assert replay.status_code == 200, replay.data
            assert replay.data['receipt'] == saved.data['receipt'] and replay.data['replayed']
        detail = self.client.get(self.followup_base + saved.data['receipt']['followup_id'] + '/')
        assert detail.status_code == 200 and detail.data['source_integrity'] == 'not_checked'
        history = self.client.get(self.followup_base)
        assert history.status_code == 200 and history.data['results'][0]['recorded'] == saved.data['receipt']

    def test_D01_D02_D03_exact_key_owner_body_subject_and_single_slot(self):
        source, batch, task = self.concerned()
        body, key = self.followup_body(task), uuid4()
        saved = self.record_followup(body, key=key)
        assert saved.status_code == 201
        assert self.record_followup(body, key=key).data['receipt'] == saved.data['receipt']
        assert self.record_followup({**body, 'reason': 'different'}, key=key).status_code == 409
        assert self.record_followup(body, key=key, actor=self.producer).status_code == 409
        assert self.record_followup(body).status_code == 409
        assert self.followup_context(task).status_code == 409
        assert SecondReviewConcernFollowup.objects.count() == 1

    def test_D04_same_owner_rotated_viewer_uses_original_command(self):
        source, batch, task = self.concerned()
        body, key = self.followup_body(task), uuid4()
        saved = self.record_followup(body, key=key)
        from review_batches import followup_contract

        with patch.object(followup_contract, 'viewer', return_value='f' * 64):
            replay = self.record_followup(body, key=key)
        assert replay.status_code == 200 and replay.data['receipt'] == saved.data['receipt']

    def test_D09_final_render_failure_rolls_back_disposition(self):
        source, batch, task = self.concerned()
        body = self.followup_body(task)
        from review_batches import second_review_contract

        render = second_review_contract.JSONRenderer.render

        def corrupted(renderer, data, *args, **kwargs):
            result = render(renderer, data, *args, **kwargs)
            if isinstance(data, dict) and 'receipt' in data:
                return result.replace(b'supervisory_only', b'changed_effect')
            return result

        with patch.object(second_review_contract.JSONRenderer, 'render', corrupted):
            assert self.record_followup(body).status_code == 409
        assert not SecondReviewConcernFollowup.objects.exists()

    def test_D10_direct_delete_is_blocked_project_cascade_works(self):
        source, batch, task = self.concerned()
        assert self.record_followup(self.followup_body(task)).status_code == 201
        with self.assertRaises(DatabaseError), transaction.atomic():
            SecondReviewConcernFollowup.objects.all().delete()
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('DELETE FROM review_batches_secondreviewconcernfollowup')
        self.project.delete()
        assert not SecondReviewConcernFollowup.objects.exists()

    def test_E01_E02_B09_strict_input_and_reason(self):
        source, batch, task = self.concerned()
        body = self.followup_body(task)
        for change in (
            {'actor_id': self.manager.pk},
            {'effect': 'approved'},
            {'expected_version': False},
            {'reason': ''},
            {'reason': '  '},
            {'reason': '\x00'},
            {'reason': 'a' * 2001},
            {'related_submission_id': source.pk},
            {'disposition': 'revision_linked'},
            {'expected_concern_hash': 'bad'},
        ):
            response = self.record_followup({**body, **change})
            assert response.status_code == 400, (change, response.data)
        self.client.force_authenticate(self.manager)
        for payload in ('{"reason":"a","reason":"b"}', '{"value":NaN}', 'x' * (16 * 1024 + 1)):
            response = self.client.post(
                self.followup_base, payload, content_type='application/json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
            )
            assert response.status_code in (400, 422), response.data
        assert not SecondReviewConcernFollowup.objects.exists()

    def test_E07_metadata_list_never_loads_snapshots(self):
        source, batch, task = self.concerned()
        self.client.force_authenticate(self.manager)
        with patch.object(followup_origin, 'load', side_effect=AssertionError('metadata only')):
            response = self.client.get(self.followup_base, {'page_size': 1})
        assert response.status_code == 200, response.data
        assert response.data['count'] == 1 and response.data['results'][0]['task_id'] == task
        assert 'result_snapshot' not in str(response.data)

    def test_F06_old_release_and_review_status_stay_unchanged(self):
        for initial, expected in (('approved', 200), ('rejected', 400)):
            source, batch, task = self.concerned(decision=initial)
            target = self.successor(source)
            assert self.record_followup(self.followup_body(task, target)).status_code == 201
            self.client.force_authenticate(self.manager)
            assert self.client.get(f'/api/submissions/{source.pk}/release/').status_code == expected
            assert self.client.get(f'/api/submissions/{target.pk}/release/').status_code == 400
            source.refresh_from_db()
            assert source.status == initial and SecondReviewTask.objects.get(pk=task).status == 'completed'

    def test_B06_nonowner_manager_revocation_denies_stale_body_and_receipt(self):
        source, batch, task = self.concerned()
        body, key = self.followup_body(task, actor=self.producer), uuid4()
        assert self.record_followup(body, key=key, actor=self.producer).status_code == 201
        ProjectMember.objects.filter(project=self.project, user=self.producer).update(enabled=False)
        assert self.record_followup(body, key=key, actor=self.producer).status_code in (403, 404)
        assert self.followup_context(task, actor=self.producer).status_code in (403, 404)

    def test_B09_trusted_mapping_controls_actor_not_the_session(self):
        from access_control.identity import Principal

        source, batch, task = self.concerned()

        class Provider:
            def resolve(inner, request):
                return Principal('followup-test:handler', 'trusted-followup-test', 'handler', self.manager.pk)

        with patch('access_control.identity.get_identity_provider', return_value=Provider()):
            body = self.followup_body(task, actor=self.worker)
            response = self.record_followup(body, actor=self.worker)
        assert response.status_code == 201, response.data
        assert response.data['receipt']['owner'] == dict(
            principal='followup-test:handler', source='trusted-followup-test', actor_id=self.manager.pk
        )

    def test_E07_large_cursor_and_decimal_are_bounded_before_conversion(self):
        self.client.force_authenticate(self.manager)
        for query in ({'page_size': '1' * 5000}, {'cursor': 'x' * 4097}, {'page_size': '101'}):
            assert self.client.get(self.followup_base, query).status_code == 400

    def test_E07_101_concerns_are_scoped_paged_without_snapshot_hydration(self):
        for _ in range(101):
            self.concerned()
        self.client.force_authenticate(self.manager)
        with patch.object(followup_origin, 'load', side_effect=AssertionError('no answers in history')):
            first = self.client.get(self.followup_base, {'page_size': 100})
            assert first.status_code == 200, first.data
            assert first.data['count'] == 101 and len(first.data['results']) == 100
            last = self.client.get(self.followup_base, {'page_size': 100, 'cursor': first.data['next']})
            assert last.status_code == 200 and len(last.data['results']) == 1 and last.data['next'] is None
        assert not {r['task_id'] for r in first.data['results']} & {r['task_id'] for r in last.data['results']}
