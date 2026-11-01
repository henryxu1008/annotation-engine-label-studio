import copy
import io
import json
from unittest.mock import patch
from uuid import uuid4

from access_control.project_operations import ORGANIZE_REVIEW, PROVIDE_REVIEW_CONFIDENCE, READ_WORK, REVIEW
from django.db import DatabaseError, IntegrityError, connection, transaction
from rest_framework.exceptions import APIException
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APITestCase
from review_batches import confidence_contract as c
from review_batches import confidence_priority as p
from review_batches.confidence_api import ConfidenceJSONParser
from review_batches.models import ReviewBatch
from review_batches.models import ReviewConfidenceObservation as Fact
from tasks.models import ReviewDecision, Submission, TaskAssignment
from tasks.submissions import _snapshot_hash

from .confidence_fixtures import PROFILE, ConfidenceFixtures


class TestConfidenceBoundaries(ConfidenceFixtures, APITestCase):
    def setUp(self):
        self.confidence_setup()
        self.source = self.make_source()

    def reason(self, result, expected, status=409):
        assert result.status_code == status, result.data
        assert result.data['display_context']['reason'] == expected, result.data

    def test_flags_modes_and_unsupported_backend_fail_before_sources(self):
        body = self.fact_body(self.source)
        for setting in (
            {'ANNOTATION_REVIEW_CONFIDENCE_ENABLED': False},
            {'ANNOTATION_REVIEW_BATCHES_ENABLED': False},
            {'ANNOTATION_REVIEW_BATCH_MODES': {}},
            {'ANNOTATION_REVIEW_BATCH_MODES': {f'{self.org.pk}:{self.project.pk}': 'core_only'}},
            {'ANNOTATION_REVIEW_CONFIDENCE_INSTANCE_ID': ''},
        ):
            with self.settings(**setting), patch.object(c, 'load', side_effect=AssertionError('source decoder')):
                assert self.publish(body=body).status_code in (403, 404)
                assert self.preview().status_code in (403, 404)
        with patch.object(connection, 'vendor', 'unsupported'), patch.object(
            c, 'load', side_effect=AssertionError('source decoder')
        ):
            assert self.publish(body=body).status_code == 503
        assert Fact.objects.count() == ReviewBatch.objects.count() == 0

    def test_producer_operations_are_independent_and_revocation_applies_to_recovery(self):
        body, key = self.fact_body(self.source), uuid4()
        assert self.publish(body=body, key=key).status_code == 201
        for operations in ([READ_WORK, ORGANIZE_REVIEW, REVIEW], [PROVIDE_REVIEW_CONFIDENCE], []):
            with self.settings(CONFIDENCE_TEST_OPERATIONS=operations):
                assert self.publish(body=body, key=key).status_code == 403
                assert self.source_input(self.source).status_code == 403
        with self.settings(CONFIDENCE_TEST_OPERATIONS=[READ_WORK, PROVIDE_REVIEW_CONFIDENCE]):
            assert self.source_input(self.source).status_code == 200
            assert self.preview(actor=self.producer).status_code == 403

    def test_current_grant_membership_account_and_project_role_revoke_old_event(self):
        body, key = self.fact_body(self.source), uuid4()
        assert self.publish(body=body, key=key).status_code == 201
        for grants in (
            [],
            [{**self.grants[0], 'source': 'another-provider'}],
            [{**self.grants[0], 'principal_id': 'other'}],
        ):
            with self.settings(ANNOTATION_REVIEW_CONFIDENCE_GRANTS=grants):
                assert self.publish(body=body, key=key).status_code == 403
        self.producer.is_active = False
        from users.session_security import reactivate_accounts
        from users.tests.factories import UserFactory

        administrator = UserFactory(is_staff=True, is_superuser=True)
        self.producer.save(update_fields=['is_active'], session_actor=administrator)
        assert self.publish(body=body, key=key).status_code in (401, 403, 404)
        reactivate_accounts(type(self.producer).objects.filter(pk=self.producer.pk), actor=administrator)
        self.producer.refresh_from_db()
        self.project.members.filter(user=self.producer).update(role='reviewer')
        assert self.publish(body=body, key=key).status_code in (403, 404)

    def test_non_producers_and_non_planners_never_load_sources(self):
        body = self.fact_body(self.source)
        for actor in (self.worker, self.reviewer, self.manager):
            with patch.object(c, 'load', side_effect=AssertionError('source decoder')):
                assert self.publish(body=body, actor=actor).status_code in (403, 404)
                assert self.source_input(self.source, actor).status_code in (403, 404)
        for actor in (self.worker, self.reviewer):
            assert self.preview(actor=actor).status_code in (403, 404)

    def test_self_producer_cannot_get_or_append(self):
        source = self.make_source(self.producer)
        assert self.source_input(source).status_code == 403
        body = {**self.fact_body(self.source), 'submission_id': source.pk}
        assert self.publish(body=body).status_code == 403

    def test_all_extra_identity_and_model_fields_rejected(self):
        body = self.fact_body(self.source)
        for name in (
            'actor_id',
            'producer_namespace',
            'producer',
            'model_id',
            'profile',
            'subject',
            'status',
            'fact_hash',
            'provider',
        ):
            assert self.publish(body={**body, name: 'forged'}).status_code == 400
        assert not Fact.objects.exists()

    def test_strict_numbers_uuids_dates_and_selection_union(self):
        body = self.fact_body(self.source)
        for value in (True, 1.0, '1', 0, -1, 2**53):
            assert self.publish(body={**body, 'submission_id': value}).status_code == 400
        for value in (PROFILE.upper(), PROFILE.replace('-', ''), '{' + PROFILE + '}', 'not-uuid', None):
            # This synthetic UUID contains no letters; use an alphabetic UUID to test case normalization separately.
            if value == PROFILE:
                continue
            assert self.publish(body={**body, 'profile_id': value}).status_code == 400
        for value in ('2026-11-01', '2026-11-01T00:00:00+08:00', '2026-99-01T00:00:00Z', 123):
            assert self.publish(body={**body, 'evaluated_at': value}).status_code == 400
        for field, value in (
            ('rate', True),
            ('rate', '50'),
            ('per_submitter', 'false'),
            ('submitter_ids', [True]),
            ('package_ids', [2**53]),
        ):
            assert self.preview({**self.args, field: value}).status_code == 400
        for extra in ({'batch_id': 1}, {'score_units': 0}, {'profiles': []}):
            assert self.preview({**self.args, 'priority': {**self.args['priority'], **extra}}).status_code == 400

    def test_raw_body_limit_before_json_decode_and_duplicate_keys(self):
        with patch('review_batches.confidence_api.json.load', side_effect=AssertionError('JSON decoder')):
            with self.assertRaises(APIException) as raised:
                ConfidenceJSONParser().parse(io.BytesIO(b' ' * (c.MAX_BODY + 1)))
            assert raised.exception.status_code == 422
        for payload in (
            b'{"score_units":1,"score_units":2}',
            b'{"score_units":NaN}',
            b'{"score_units":Infinity}',
            b'"\xff"',
        ):
            with self.assertRaises(APIException) as raised:
                ConfidenceJSONParser().parse(io.BytesIO(payload))
            assert raised.exception.status_code == 400

    def test_cross_project_and_unknown_source_cannot_return_answers(self):
        body = self.fact_body(self.source)
        body['submission_id'] = self.source.pk + 99999
        assert self.publish(body=body).status_code == 409
        self.client.force_authenticate(self.producer)
        result = self.client.get(
            self.input_base.replace(f'/{self.project.pk}/', f'/{self.project.pk + 9999}/') + f'{self.source.pk}/',
            {'profile_id': PROFILE, 'profile_digest': self.profile_digest},
        )
        assert result.status_code == 404 and 'Frozen text' not in str(result.data)

    def test_snapshot_binding_ignores_live_task_and_config(self):
        original = self.source_input(self.source).data
        self.source.assignment.task.data = {'text': 'changed live data'}
        self.source.assignment.task.save(update_fields=['data'])
        self.project.label_config = '<View><Text name="other" value="$else"/></View>'
        self.project.save(update_fields=['label_config'])
        current = self.source_input(self.source)
        assert current.status_code == 200 and current.data == original
        assert (
            self.publish(
                body={
                    'submission_id': self.source.pk,
                    'profile_id': PROFILE,
                    'profile_digest': self.profile_digest,
                    'evaluated_input_hash': original['input_hash'],
                    'score_units': 0,
                    'evaluated_at': '2026-11-01T00:00:00Z',
                }
            ).status_code
            == 201
        )

    def test_source_tamper_without_hash_change_fails(self):
        body = self.fact_body(self.source)
        snapshot = copy.deepcopy(self.source.result_snapshot)
        snapshot['task']['data']['text'] = 'tampered'
        Submission.objects.filter(pk=self.source.pk).update(result_snapshot=snapshot)
        self.reason(self.publish(body=body), 'REVIEW_CONFIDENCE_PROOF_INVALID')
        self.reason(self.preview(), 'REVIEW_CONFIDENCE_PROOF_INVALID')

    def test_new_revision_and_review_and_reassignment_reject_stale_input(self):
        body = self.fact_body(self.source)
        successor = self.make_source(revision=2, assignment=self.source.assignment)
        assert self.publish(body=body).status_code == 409
        assert self.publish(successor).status_code == 201
        preview = self.preview()
        assert [c['submission_id'] for c in preview.data['plan']['candidates']] == [successor.pk]
        TaskAssignment.objects.filter(pk=successor.assignment_id).update(assignee=self.second_worker)
        assert self.source_input(successor).status_code == 409
        assert self.preview().data['plan']['priority']['evidence'][0]['state'] == 'unknown'

    def test_registry_bad_digest_duplicates_and_more_than_twenty_deny_fresh_keep_recovery(self):
        first = self.create_batch()
        assert first.status_code == 201
        configurations = [[{**self.entries[0], 'digest': 'b' * 64}], self.entries * 2, self.entries * 21, []]
        for entries in configurations:
            with self.settings(ANNOTATION_REVIEW_CONFIDENCE_PROFILES=entries):
                assert self.preview().status_code in (403, 409, 422)
                self.client.force_authenticate(self.manager)
                cap = self.client.get(self.base + 'capability/')
                assert cap.status_code == 200, cap.data
                assert cap.data['can_recover_confidence'] and not cap.data['can_prioritize_confidence']
        self.client.force_authenticate(self.reviewer)
        cap = self.client.get(self.base + 'capability/')
        assert cap.status_code == 200 and cap.data['can_read_confidence_priority'] is True, cap.data
        assert 'confidence_profiles' not in cap.data and 'confidence_owner_hash' not in cap.data

    def test_new_profile_digest_does_not_reuse_or_mix_old_score(self):
        assert self.publish(self.source).status_code == 201
        descriptor = {**self.descriptor, 'calibration_version': 'next'}
        digest = c.digest(descriptor)
        entries = [*self.entries, {**self.entries[0], 'descriptor': descriptor, 'digest': digest}]
        args = copy.deepcopy(self.args)
        args['priority']['profile_digest'] = digest
        with self.settings(ANNOTATION_REVIEW_CONFIDENCE_PROFILES=entries):
            result = self.preview(args)
            assert result.status_code == 200 and result.data['plan']['priority']['summary']['known_candidates'] == 0
            body = {**self.fact_body(self.source), 'profile_digest': digest}
            assert self.publish(body=body).status_code == 201
        assert Fact.objects.count() == 2

    def test_same_event_other_profile_body_conflicts_not_profile_scoped(self):
        body, key = self.fact_body(self.source), uuid4()
        assert self.publish(body=body, key=key).status_code == 201
        for change in ({'profile_digest': 'b' * 64}, {'profile_id': str(uuid4())}, {'score_units': 5}):
            self.reason(
                self.publish(body={**body, **change}, key=key),
                'REVIEW_CONFIDENCE_FORBIDDEN' if 'profile_id' in change else 'REVIEW_CONFIDENCE_KEY_CONFLICT',
                403 if 'profile_id' in change else 409,
            )

    def test_frozen_fact_all_update_paths_and_nullable_fk_cannot_rebind(self):
        assert self.publish(self.source).status_code == 201
        fact = Fact.objects.get()
        for field, value in (
            ('fact_hash', 'b' * 64),
            ('proof', {}),
            ('receipt', {}),
            ('submission_id_frozen', 9999),
            ('actor_id_frozen', 9999),
        ):
            with self.assertRaises(DatabaseError), transaction.atomic():
                Fact.objects.filter(pk=fact.pk).update(**{field: value})
        Fact.objects.filter(pk=fact.pk).update(submission=None, created_by=None)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Fact.objects.filter(pk=fact.pk).update(submission=self.source)
        result = self.preview()
        assert (
            result.status_code == 200
            and result.data['plan']['priority']['evidence'][0]['reason'] == 'source_unavailable'
        )

    def test_batch_key_from_ordinary_strategy_is_conflict_not_server_error(self):
        ordinary = {k: v for k, v in self.args.items() if k != 'priority'}
        first = self.preview(ordinary)
        key = uuid4()
        assert (
            self.create_batch({**ordinary, 'expected_plan_hash': first.data['plan_hash']}, key=key).status_code == 201
        )
        preview = self.preview()
        self.reason(
            self.create_batch({**self.args, 'expected_plan_hash': preview.data['plan_hash']}, key=key),
            'REVIEW_BATCH_KEY_CONFLICT',
        )

    def test_confidence_off_preserves_ordinary_list_and_hides_new_plan(self):
        assert self.create_batch().status_code == 201
        ordinary = {k: v for k, v in self.args.items() if k != 'priority'}
        result = self.preview(ordinary)
        old = self.create_batch({**ordinary, 'expected_plan_hash': result.data['plan_hash']})
        with self.settings(ANNOTATION_REVIEW_CONFIDENCE_ENABLED=False):
            self.client.force_authenticate(self.manager)
            result = self.client.get(self.base)
            assert result.status_code == 200, result.data
            assert [r['id'] for r in result.data['results']['batches']] == [old.data['id']]
            cap = self.client.get(self.base + 'capability/')
            assert not any('confidence' in k for k in cap.data)

    def test_batch_recovery_rejects_instance_clone_and_keeps_frozen_history(self):
        preview = self.preview()
        body, key = {**self.args, 'expected_plan_hash': preview.data['plan_hash']}, uuid4()
        result = self.create_batch(body, key=key)
        with self.settings(ANNOTATION_REVIEW_CONFIDENCE_INSTANCE_ID=str(uuid4())):
            assert self.create_batch(body, key=key).status_code == 403
        assert ReviewBatch.objects.get().plan_hash == result.data['plan_hash']

    def test_post_render_actual_snapshot_changes_rollback_both_writes(self):
        body = self.fact_body(self.source)
        original = JSONRenderer.render
        fired = []

        def render(renderer, data, *args, **kwargs):
            value = original(renderer, data, *args, **kwargs)
            if not fired and isinstance(data, dict) and data.get('schema') in (c.RECEIPT, c.SCHEMA):
                fired.append(True)
                snapshot = copy.deepcopy(self.source.result_snapshot)
                snapshot['task']['data']['text'] = snapshot['task']['data']['text'].replace('Frozen', 'Broken')
                assert len(json.dumps(snapshot, ensure_ascii=False).encode()) == len(
                    json.dumps(self.source.result_snapshot, ensure_ascii=False).encode()
                )
                measure = (
                    'OCTET_LENGTH(CAST(result_snapshot AS TEXT))'
                    if connection.vendor == 'postgresql'
                    else 'LENGTH(CAST(result_snapshot AS BLOB))'
                )

                def stored():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            f'SELECT {measure}, result_hash FROM tasks_submission WHERE id=%s', [self.source.pk]
                        )
                        return cursor.fetchone()

                before = stored()
                Submission.objects.filter(pk=self.source.pk).update(result_snapshot=snapshot)
                assert stored() == before
            return value

        with patch.object(JSONRenderer, 'render', render):
            assert self.publish(body=body).status_code == 409
        assert not Fact.objects.exists()
        fired.clear()
        preview = self.preview()
        with patch.object(JSONRenderer, 'render', render):
            assert self.create_batch({**self.args, 'expected_plan_hash': preview.data['plan_hash']}).status_code == 409
        assert not ReviewBatch.objects.exists()

    def test_post_render_grant_revocation_rollback(self):
        body = self.fact_body(self.source)
        original = JSONRenderer.render

        def render(renderer, data, *args, **kwargs):
            value = original(renderer, data, *args, **kwargs)
            if isinstance(data, dict) and data.get('schema') == c.RECEIPT:
                self.grants.clear()
            return value

        with patch.object(JSONRenderer, 'render', render):
            assert self.publish(body=body).status_code == 403
        assert not Fact.objects.exists()

    def test_unrelated_integrity_error_is_not_idempotent_success(self):
        body = self.fact_body(self.source)
        with patch.object(Fact.objects, 'create', side_effect=IntegrityError('unrelated')):
            assert self.publish(body=body).status_code == 500
        assert not Fact.objects.exists()

    def test_sql_snapshot_row_and_union_preflight_reject_without_json_decoder(self):
        from django.db.models import JSONField

        snapshot = copy.deepcopy(self.source.result_snapshot)
        snapshot['meta_large'] = 'x' * (c.MAX_SINGLE_STORED + 1)
        Submission.objects.filter(pk=self.source.pk).update(result_snapshot=snapshot)
        original = JSONField.from_db_value
        decoded = []

        def decode(field, value, expression, db):
            if value and 'meta_large' in str(value):
                decoded.append(value)
            return original(field, value, expression, db)

        with patch.object(JSONField, 'from_db_value', decode):
            self.reason(self.preview(), 'REVIEW_CONFIDENCE_TOO_LARGE', 422)
        assert not decoded

    def test_canonical_snapshot_limit_and_no_model_calls(self):
        snapshot = copy.deepcopy(self.source.result_snapshot)
        snapshot['meta_large'] = 'x' * c.MAX_SINGLE
        Submission.objects.filter(pk=self.source.pk).update(
            result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
        )
        self.reason(self.preview(), 'REVIEW_CONFIDENCE_TOO_LARGE', 422)
        assert not Fact.objects.exists() and not ReviewBatch.objects.exists()

    def test_oversized_legacy_review_reason_sql_gate_before_snapshot_decode(self):
        result = self.create_batch()
        ReviewDecision.objects.create(
            submission=self.source, reviewer=self.reviewer, decision='rejected', reason='r' * (c.MAX_OUTPUT + 1)
        )
        Submission.objects.filter(pk=self.source.pk).update(status='rejected')
        from django.db.models import JSONField

        original, decoded = JSONField.from_db_value, []

        def decode(field, value, expression, db):
            if value and 'Frozen text' in str(value):
                decoded.append(value)
            return original(field, value, expression, db)

        self.client.force_authenticate(self.reviewer)
        with patch.object(JSONField, 'from_db_value', decode):
            response = self.client.get(self.base + f'{result.data["id"]}/')
        self.reason(response, 'REVIEW_CONFIDENCE_TOO_LARGE', 422)
        assert not decoded

    def test_reviewer_self_rows_excluded_before_source_reads(self):
        self.make_source(self.reviewer)
        result = self.create_batch()
        assert result.status_code == 201
        self.client.force_authenticate(self.reviewer)
        original, seen = p.load, []

        def load(query, **kwargs):
            seen.extend(query.values_list('submitted_by_id', flat=True))
            return original(query, **kwargs)

        with patch.object(p, 'load', load):
            response = self.client.get(self.base + f'{result.data["id"]}/')
        assert response.status_code == 200, response.data
        assert self.reviewer.pk not in seen
        assert response.data['candidate_count'] == 1

    def test_confidence_queue_and_review_do_not_decode_other_users_hotkeys(self):
        result = self.create_batch()
        type(self.worker).objects.filter(pk=self.worker.pk).update(
            custom_hotkeys={'unbounded-other-user': 'x' * 1000000}
        )
        from django.db.models import JSONField

        original = JSONField.from_db_value

        def decode(field, value, expression, db):
            assert 'unbounded-other-user' not in str(value)
            return original(field, value, expression, db)

        self.client.force_authenticate(self.reviewer)
        with patch.object(JSONField, 'from_db_value', decode):
            queue = self.client.get(self.base + f'{result.data["id"]}/queue/')
            assert queue.status_code == 200, queue.data
            reviewed = self.client.post(
                f'/api/submissions/{self.source.pk}/review/?review_batch={result.data["id"]}',
                {'decision': 'approved'},
                format='json',
            )
            assert reviewed.status_code == 200, reviewed.data
            assert 'custom_hotkeys' not in str(queue.data) and 'custom_hotkeys' not in str(reviewed.data)

    def test_rework_original_and_successor_use_minimal_users_without_lazy_hotkeys(self):
        result = self.create_batch()
        ReviewDecision.objects.create(
            submission=self.source, reviewer=self.reviewer, decision='rejected', reason='rework'
        )
        Submission.objects.filter(pk=self.source.pk).update(status='rejected')
        self.make_source(revision=2, assignment=self.source.assignment)
        type(self.worker).objects.filter(pk__in=[self.worker.pk, self.reviewer.pk]).update(
            custom_hotkeys={'unbounded-other-user': 'x' * 1000000}
        )
        from django.db.models import JSONField

        original = JSONField.from_db_value

        def decode(field, value, expression, db):
            assert 'unbounded-other-user' not in str(value)
            return original(field, value, expression, db)

        self.client.force_authenticate(self.manager)
        with patch.object(JSONField, 'from_db_value', decode):
            response = self.client.get(self.base + f'{result.data["id"]}/rework/{self.source.pk}/')
        assert response.status_code == 200, response.data
        assert 'custom_hotkeys' not in str(response.data) and 'active_organization_meta' not in str(response.data)
