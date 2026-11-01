import copy
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from access_control.identity import LocalIdentityProvider
from rest_framework.test import APIClient, APITestCase
from review_batches import confidence_contract as c
from review_batches import confidence_priority as p
from review_batches import service
from review_batches.models import ReviewBatch
from review_batches.models import ReviewConfidenceObservation as Fact
from tasks.models import Prediction, ReviewDecision, Submission, TaskAssignment
from tasks.submissions import _snapshot_hash

from .confidence_fixtures import PROFILE, ConfidenceFixtures


class TestConfidenceAcceptance(ConfidenceFixtures, APITestCase):
    def setUp(self):
        self.confidence_setup()
        self.source = self.make_source()

    def test_real_tokens_producer_planner_reviewer_and_grant_revocation(self):
        self.org.jwt.legacy_api_tokens_enabled = True
        self.org.jwt.save(update_fields=['legacy_api_tokens_enabled'])
        body = self.fact_body(self.source)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {self.producer.get_token().key}')
        result = client.post(self.fact_base, body, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        assert result.status_code == 201, result.data
        client.credentials(HTTP_AUTHORIZATION=f'Token {self.manager.get_token().key}')
        assert client.post(self.fact_base, body, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())).status_code == 403
        assert client.post(self.base + 'preview/', self.args, format='json').status_code == 200
        client.credentials(HTTP_AUTHORIZATION=f'Token {self.reviewer.get_token().key}')
        assert client.post(self.base + 'preview/', self.args, format='json').status_code in (403, 404)
        assert 'confidence_profiles' not in client.get(self.base + 'capability/').data
        client.credentials(HTTP_AUTHORIZATION=f'Token {self.producer.get_token().key}')
        with self.settings(ANNOTATION_REVIEW_CONFIDENCE_GRANTS=[]):
            assert (
                client.get(
                    self.input_base + f'{self.source.pk}/',
                    {'profile_id': PROFILE, 'profile_digest': self.profile_digest},
                ).status_code
                == 403
            )

    def test_trusted_mapped_actor_is_used_instead_of_session_user(self):
        resolve = LocalIdentityProvider.resolve

        def mapped(provider, request):
            return replace(
                resolve(provider, request), principal_id=self.grants[0]['principal_id'], local_user_id=self.producer.pk
            )

        self.client.force_authenticate(self.manager)
        with patch.object(LocalIdentityProvider, 'resolve', mapped):
            result = self.client.get(
                self.input_base + f'{self.source.pk}/', {'profile_id': PROFILE, 'profile_digest': self.profile_digest}
            )
            assert result.status_code == 200, result.data
            body = {
                'submission_id': self.source.pk,
                'profile_id': PROFILE,
                'profile_digest': self.profile_digest,
                'evaluated_input_hash': result.data['input_hash'],
                'score_units': 0,
                'evaluated_at': '2026-11-01T00:00:00Z',
            }
            result = self.client.post(self.fact_base, body, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        assert result.status_code == 201 and Fact.objects.get().actor_id_frozen == self.producer.pk, result.data

    def test_same_human_principal_other_provider_cannot_borrow_batch_receipt(self):
        preview = self.preview()
        body, key = {**self.args, 'expected_plan_hash': preview.data['plan_hash']}, uuid4()
        assert self.create_batch(body, key=key).status_code == 201
        resolve = LocalIdentityProvider.resolve

        def alternate(provider, request):
            return replace(resolve(provider, request), source='another-trusted-provider')

        with patch.object(LocalIdentityProvider, 'resolve', alternate), patch.object(
            p, 'materialize', side_effect=AssertionError('local conflicting receipt only')
        ):
            assert self.create_batch(body, key=key).status_code == 409

    def test_generic_prediction_never_becomes_trusted_confidence(self):
        Prediction.objects.create(
            project=self.project,
            task=self.source.assignment.task,
            score=0.00001,
            model_version='untrusted-import',
            result=[],
        )
        result = self.preview()
        assert result.status_code == 200 and result.data['plan']['priority']['summary']['unknown_candidates'] == 1
        assert not Fact.objects.exists()

    def test_unicode_normalization_ack_hash_and_recovery_match_full_normalized_command(self):
        for space in ('\u0085', '\u001c', '\u001d', '\u001e', '\u001f', '\u3000', '\ufeff'):
            args = {
                **self.args,
                'name': space + 'Confidence' + space,
                'seed': space + 'fixed' + space,
                'package_ids': [],
                'submitter_ids': [],
                'per_submitter': False,
            }
            preview = self.preview(args)
            assert preview.status_code == 200, preview.data
            body = {**args, 'expected_plan_hash': preview.data['plan_hash']}
            normalized = {**body, 'name': body['name'].strip(), 'seed': body['seed'].strip()}
            key = uuid4()
            created = self.create_batch(body, key=key)
            assert created.status_code == 201 and created.data['request_hash'] == service.digest(
                normalized
            ), created.data
            recovered = self.create_batch(body, key=key)
            assert recovered.status_code == 200 and recovered.data['request_hash'] == service.digest(normalized)

    def test_unsupported_dynamic_url_and_mixed_configs_are_unknown_not_promoted(self):
        original = self.source.result_snapshot
        configs = [
            '<View><Text name="text" value="$text"/><Choices name="label" toName="text" value="$labels"/></View>',
            '<View><Text name="text" value="$text" valueType="url"/><Choices name="label" toName="text"><Choice value="A"/></Choices></View>',
            '<View><Text name="text" value="$text"/><TextArea name="label" toName="text"/></View>',
        ]
        for xml in configs:
            snapshot = copy.deepcopy(original)
            snapshot['project']['label_config'] = xml
            Submission.objects.filter(pk=self.source.pk).update(
                result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
            )
            result = self.preview()
            assert result.status_code == 200, result.data
            assert result.data['plan']['priority']['evidence'][0]['reason'] == 'unsupported_input'
            assert self.source_input(self.source).status_code == 400

    def test_strict_threshold_low_first_ties_and_uncovered_count(self):
        rows = [self.source] + [self.make_source(self.second_worker if i % 2 else self.worker) for i in range(5)]
        for row, score in zip(rows, (0, 1, 1, 400000, 999999, 1000000)):
            assert self.publish(row, score).status_code == 201
        result = self.preview()
        assert result.status_code == 200, result.data
        assert {r['submission_id'] for r in result.data['plan']['candidates'] if r['selected']} == {
            r.pk for r in rows[:3]
        }
        s = result.data['plan']['priority']['summary']
        assert (
            s['low_confidence_candidates'] == s['selected_low_confidence'] == 3 and s['uncovered_low_confidence'] == 0
        )
        args = {**self.args, 'rate': 1}
        result = self.preview(args)
        assert [r['submission_id'] for r in result.data['plan']['candidates'] if r['selected']] == [rows[0].pk]
        assert result.data['plan']['priority']['summary']['uncovered_low_confidence'] == 2

    def test_global_and_per_submitter_quota_all_unknown_and_rate_100(self):
        self.make_source()
        self.make_source(self.second_worker)
        self.make_source(self.second_worker)
        self.make_source(self.second_worker)
        for per_person in (True, False):
            for rate in (1, 50, 100):
                args = {**self.args, 'per_submitter': per_person, 'rate': rate}
                result = self.preview(args)
                assert result.status_code == 200, result.data
                rows = result.data['plan']['candidates']
                groups = {}
                for row in rows:
                    groups.setdefault(row['submitter_id'] if per_person else 'all', []).append(row)
                assert all(
                    sum(r['selected'] for r in group) == (len(group) * rate + 99) // 100 for group in groups.values()
                )
                assert result.data['plan']['priority']['summary']['unknown_candidates'] == 5

    def test_known_non_low_and_unknown_share_seeded_fallback_bucket(self):
        self.make_source(self.second_worker)
        assert self.publish(self.source, 900000).status_code == 201
        result = self.preview()
        assert (
            result.status_code == 200 and result.data['plan']['priority']['summary']['low_confidence_candidates'] == 0
        )
        expected = min(
            result.data['plan']['candidates'],
            key=lambda row: service.digest(
                [c.ALGORITHM, self.project.pk, self.args['seed'], {k: v for k, v in row.items() if k != 'selected'}]
            ),
        )
        assert (
            next(row for row in result.data['plan']['candidates'] if row['selected'])['submission_id']
            == expected['submission_id']
        )

    def test_new_latest_revision_does_not_inherit_old_score_or_change_frozen_selection(self):
        assert self.publish(self.source).status_code == 201
        created = self.create_batch()
        old = ReviewBatch.objects.get()
        successor = self.make_source(revision=2, assignment=self.source.assignment)
        result = self.preview()
        assert result.status_code == 200 and result.data['plan']['priority']['evidence'][0]['state'] == 'unknown'
        assert result.data['plan']['candidates'][0]['submission_id'] == successor.pk
        old.refresh_from_db()
        assert (
            old.plan_hash == created.data['plan_hash'] and old.plan['candidates'][0]['submission_id'] == self.source.pk
        )

    def test_deleted_original_fk_cannot_rebind_recreated_submission_id(self):
        assert self.publish(self.source).status_code == 201
        created = self.create_batch()
        old = {field.attname: getattr(self.source, field.attname) for field in self.source._meta.concrete_fields}
        identifier = self.source.pk
        self.source.delete()
        recreated = Submission.objects.create(**old)
        assert recreated.pk == identifier and Fact.objects.get().submission_id is None
        preview = self.preview()
        assert (
            preview.status_code == 200
            and preview.data['plan']['priority']['evidence'][0]['reason'] == 'source_unavailable'
        )
        self.client.force_authenticate(self.manager)
        detail = self.client.get(self.base + f'{created.data["id"]}/')
        assert detail.status_code == 200 and detail.data['items'][0]['status'] == 'unavailable', detail.data

    def test_history_does_not_rerank_after_fact_or_retired_profile(self):
        created = self.create_batch()
        frozen = copy.deepcopy(ReviewBatch.objects.get().plan)
        assert self.publish(self.source).status_code == 201
        with self.settings(ANNOTATION_REVIEW_CONFIDENCE_PROFILES=[]):
            self.client.force_authenticate(self.manager)
            result = self.client.get(self.base + f'{created.data["id"]}/')
        assert result.status_code == 200 and result.data['priority_summary']['unknown_candidates'] == 1, result.data
        assert ReviewBatch.objects.get().plan == frozen

    def test_null_submitter_confidence_list_matches_object_visibility(self):
        Submission.objects.filter(pk=self.source.pk).update(submitted_by=None)
        created = self.create_batch()
        self.client.force_authenticate(self.reviewer)
        listed = self.client.get(self.base)
        assert listed.status_code == 200 and listed.data['count'] == 1, listed.data
        assert listed.data['results']['batches'][0]['id'] == created.data['id']
        assert listed.data['schema'] == c.SCHEMA and listed.data['project_id'] == self.project.pk
        assert self.client.get(self.base + f'{created.data["id"]}/').status_code == 200

    def test_null_submitter_with_self_assignee_still_hidden_in_collection_and_object(self):
        Submission.objects.filter(pk=self.source.pk).update(submitted_by=None)
        TaskAssignment.objects.filter(pk=self.source.assignment_id).update(assignee=self.reviewer)
        created = self.create_batch()
        self.client.force_authenticate(self.reviewer)
        listed = self.client.get(self.base)
        assert listed.status_code == 200 and listed.data['count'] == 0, listed.data
        assert self.client.get(self.base + f'{created.data["id"]}/').status_code == 404

    def test_reassignment_to_reviewer_never_hydrates_own_source_in_queue(self):
        created = self.create_batch()
        TaskAssignment.objects.filter(pk=self.source.assignment_id).update(assignee=self.reviewer)
        self.client.force_authenticate(self.reviewer)
        original = p.load

        def load(query, **kwargs):
            assert not query.filter(pk=self.source.pk).exists()
            return original(query, **kwargs)

        with patch.object(p, 'load', load):
            result = self.client.get(self.base + f'{created.data["id"]}/queue/')
        assert result.status_code == 200 and result.data['count'] == 0, result.data

    def test_decorative_html_css_and_remote_choice_attributes_are_unsupported_unknown(self):
        body = self.fact_body(self.source)
        original = self.source.result_snapshot
        xml = original['project']['label_config']
        configs = [
            xml.replace(
                '<Choice value="A"',
                '<Choice html="&lt;img src=&quot;https://example.test/mutable.png&quot;/&gt;" value="A"',
            ),
            xml.replace(
                '<Choice value="A"', '<Choice style="background:url(https://example.test/image.png)" value="A"'
            ),
            xml.replace('<View>', '<View style="background:url(https://example.test/image.png)">'),
            xml.replace('<Text name=', '<Text encoding="base64" name='),
        ]
        for config in configs:
            snapshot = copy.deepcopy(original)
            snapshot['project']['label_config'] = config
            Submission.objects.filter(pk=self.source.pk).update(
                result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
            )
            assert self.source_input(self.source).status_code == 400
            assert self.publish(body=body).status_code == 400
            result = self.preview()
            assert (
                result.status_code == 200
                and result.data['plan']['priority']['evidence'][0]['reason'] == 'unsupported_input'
            )

    def test_fact_hash_damage_is_not_masked_as_unknown(self):
        assert self.publish(self.source).status_code == 201
        from review_batches import confidence_observations as o

        original = o.rows

        def corrupt(*args, **kwargs):
            values = original(*args, **kwargs)
            for value in values:
                value.proof['subject']['result_hash'] = 'b' * 64
            return values

        with patch.object(o, 'rows', corrupt):
            result = self.preview()
        assert result.status_code == 409, result.data

    def test_unknown_version_and_each_partial_marker_never_fall_back(self):
        plans = [
            {'algorithm': 'revision-confidence-ranked/v2'},
            {'algorithm': service.ALGORITHM, 'priority': {'kind': c.KIND}},
            {'algorithm': service.ALGORITHM, 'priority': {'schema': c.SCHEMA}},
        ]
        self.client.force_authenticate(self.manager)
        for plan in plans:
            row = ReviewBatch.objects.create(
                project=self.project,
                created_by=self.manager,
                actor_id_frozen=self.manager.pk,
                creator_principal=f'label_studio_local:{self.manager.pk}',
                name='partial',
                idempotency_key=uuid4(),
                request_hash='a' * 64,
                plan=plan,
                plan_hash=service.digest(plan),
            )
            assert self.client.get(self.base + f'{row.pk}/').status_code == 409
            with self.settings(ANNOTATION_REVIEW_CONFIDENCE_ENABLED=False):
                assert self.client.get(self.base + f'{row.pk}/').status_code in (403, 404, 409)
                assert self.client.get(self.base).data['count'] == 0

    def test_confidence_does_not_replace_ordinary_review_authority_or_decision_uniqueness(self):
        created = self.create_batch()
        self.client.force_authenticate(self.manager)
        assert (
            self.client.post(
                f'/api/submissions/{self.source.pk}/review/', {'decision': 'approved'}, format='json'
            ).status_code
            == 403
        )
        self.client.force_authenticate(self.reviewer)
        result = self.client.post(
            f'/api/submissions/{self.source.pk}/review/',
            {'decision': 'rejected', 'reason': 'ordinary review'},
            format='json',
        )
        assert result.status_code == 200, result.data
        again = self.client.post(
            f'/api/submissions/{self.source.pk}/review/?review_batch={created.data["id"]}',
            {'decision': 'approved'},
            format='json',
        )
        assert again.status_code == 400 and ReviewDecision.objects.filter(submission=self.source).count() == 1
        assert ReviewDecision.objects.get(submission=self.source).decision == 'rejected'
