"""Actual UTF-8 row/union budgets, decoder probes and late output rollback."""
import copy
import json
from unittest.mock import patch
from uuid import uuid4

from django.db.models import JSONField
from rest_framework.exceptions import APIException
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APITestCase
from review_batches import confidence_contract as c
from review_batches import confidence_observations as o
from review_batches import confidence_priority as p
from review_batches.models import ReviewBatch
from review_batches.models import ReviewConfidenceObservation as Fact
from tasks.models import Submission, Task, TaskAssignment
from tasks.submissions import _snapshot_hash

from .confidence_fixtures import INSTANCE, NAMESPACE, PROFILE, ConfidenceFixtures


class TestConfidenceBudgets(ConfidenceFixtures, APITestCase):
    def setUp(self):
        self.confidence_setup()
        self.source = self.make_source()

    def bulk_sources(self, number, padding=0):
        tasks = Task.objects.bulk_create(
            [Task(project=self.project, data={'text': 'synthetic'}) for _ in range(number)]
        )
        assignments = TaskAssignment.objects.bulk_create(
            [
                TaskAssignment(project=self.project, task=t, assignee=self.worker, assigned_by=self.manager)
                for t in tasks
            ]
        )
        rows = []
        for assignment in assignments:
            snapshot = copy.deepcopy(self.source.result_snapshot)
            snapshot['task']['id'] = assignment.task_id
            snapshot['budget-union-marker'] = 'x' * padding
            rows.append(
                Submission(
                    assignment=assignment,
                    revision=1,
                    submitted_by=self.worker,
                    result_snapshot=snapshot,
                    result_hash=_snapshot_hash(snapshot),
                )
            )
        return Submission.objects.bulk_create(rows)

    def decoder(self, recorded):
        original = JSONField.from_db_value

        def decode(field, value, expression, db):
            if value and 'budget-union-marker' in str(value):
                recorded.append(value)
            return original(field, value, expression, db)

        return decode

    def test_candidate_1000_and_1001_cap_before_any_snapshot_hydration(self):
        self.bulk_sources(999)
        from review_batches import service

        args = {**self.args, 'per_submitter': False, 'package_ids': [], 'submitter_ids': []}
        _, ids = service.candidate_rows(self.project, args)
        assert len(ids) == 1000
        accepted = self.preview()
        assert accepted.status_code == 200 and len(accepted.data['plan']['candidates']) == 1000, accepted.data
        self.bulk_sources(1)
        with patch.object(p, 'load', side_effect=AssertionError('no hydration above count cap')):
            result = self.preview()
        assert result.status_code == 422, result.data

    def test_real_64mib_snapshot_union_rejected_before_all_json_decoders(self):
        self.bulk_sources(33, padding=2 * c.MIB - 4096)
        decoded = []
        with patch.object(JSONField, 'from_db_value', self.decoder(decoded)):
            result = self.preview()
        assert result.status_code == 422, result.data
        assert not decoded

    def test_real_8mib_canonical_union_is_cumulative_not_reset_per_row(self):
        self.bulk_sources(35, padding=240 * 1024)
        decoded = []
        with patch.object(JSONField, 'from_db_value', self.decoder(decoded)):
            result = self.preview()
        assert result.status_code == 422, result.data
        assert decoded  # Stored SQL budget fits; canonical union fails after bounded decoding.

    def test_exact_canonical_single_snapshot_boundary_and_one_byte_over(self):
        snapshot = copy.deepcopy(self.source.result_snapshot)
        snapshot['padding'] = ''
        base = len(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
        snapshot['padding'] = 'x' * (c.MAX_SINGLE - base)
        assert c.bounded(snapshot, c.MAX_SINGLE) == c.MAX_SINGLE
        Submission.objects.filter(pk=self.source.pk).update(
            result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
        )
        assert self.preview().status_code == 200
        snapshot['padding'] += 'x'
        Submission.objects.filter(pk=self.source.pk).update(
            result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
        )
        assert self.preview().status_code == 422

    def fake_facts(self, number, proof, receipt=None):
        offset = 10000 + Fact.objects.count()
        return Fact.objects.bulk_create(
            [
                Fact(
                    project=self.project,
                    instance_id=INSTANCE,
                    producer_namespace=NAMESPACE,
                    profile_id=PROFILE,
                    profile_digest=self.profile_digest,
                    event_uuid=uuid4(),
                    submission_id_frozen=offset + i,
                    actor_id_frozen=self.producer.pk,
                    creator_principal='synthetic',
                    creator_source='synthetic',
                    request_hash='a' * 64,
                    proof=proof,
                    receipt=receipt or proof,
                    fact_hash='b' * 64,
                    receipt_hash='c' * 64,
                )
                for i in range(number)
            ]
        )

    def test_metadata_individual_canonical_exact_8kib_and_over(self):
        proof = {'padding': ''}
        base = len(json.dumps(proof, separators=(',', ':')).encode())
        proof['padding'] = 'x' * (c.MAX_FACT - base)
        rows = self.fake_facts(1, proof)
        budget = c.metadata_budget()
        assert len(budget.rows(Fact.objects.filter(pk=rows[0].pk), o.FIELDS, o.LIMITS)) == 1
        assert budget.canonical == c.MAX_FACT * 2
        self.fake_facts(1, {**proof, 'padding': proof['padding'] + 'x'})
        with self.assertRaises(APIException) as raised:
            c.metadata_budget().rows(Fact.objects.exclude(pk=rows[0].pk), o.FIELDS, o.LIMITS)
        assert raised.exception.status_code == 422

    def test_metadata_real_16mib_union_gate_before_decoder(self):
        self.fake_facts(270, {'budget-union-marker': 'x' * (31 * 1024)})
        decoded = []
        with patch.object(JSONField, 'from_db_value', self.decoder(decoded)), self.assertRaises(
            APIException
        ) as raised:
            c.metadata_budget().rows(Fact.objects.all(), o.FIELDS, o.LIMITS)
        assert raised.exception.status_code == 422 and not decoded

    def test_metadata_real_4mib_canonical_union_and_fact_32kib_sql_gate(self):
        self.fake_facts(270, {'budget-union-marker': 'x' * 7900})
        with self.assertRaises(APIException) as raised:
            c.metadata_budget().rows(Fact.objects.all(), o.FIELDS, o.LIMITS)
        assert raised.exception.status_code == 422
        rows = self.fake_facts(1, {'budget-union-marker': 'x' * (c.MAX_FACT_STORED + 1)})
        decoded = []
        with patch.object(JSONField, 'from_db_value', self.decoder(decoded)), self.assertRaises(
            APIException
        ) as raised:
            c.metadata_budget().rows(Fact.objects.filter(pk=rows[0].pk), o.FIELDS, o.LIMITS)
        assert raised.exception.status_code == 422 and not decoded

    def test_late_response_4mib_overflow_rolls_back_new_fact_and_batch(self):
        body = self.fact_body(self.source)
        original = JSONRenderer.render

        def render(renderer, data, *args, **kwargs):
            value = original(renderer, data, *args, **kwargs)
            return (
                value + b' ' * c.MAX_OUTPUT
                if isinstance(data, dict) and data.get('schema') in (c.RECEIPT, c.SCHEMA)
                else value
            )

        with patch.object(JSONRenderer, 'render', render):
            result = self.publish(body=body)
        assert result.status_code == 422 and not Fact.objects.exists(), result.data
        preview = self.preview()
        key = uuid4()
        full_body = {**self.args, 'expected_plan_hash': preview.data['plan_hash']}
        with patch.object(JSONRenderer, 'render', render):
            result = self.create_batch(full_body, key=key)
        assert result.status_code == 422 and not ReviewBatch.objects.exists(), result.data
        assert self.create_batch(full_body, key=key).status_code == 201

    def test_oversized_partial_plan_is_sql_gated_in_detail_and_mixed_page(self):
        from review_batches import service

        plan = {'algorithm': c.ALGORITHM, 'budget-union-marker': 'x' * (c.MAX_PLAN_STORED + 1)}
        row = ReviewBatch.objects.create(
            project=self.project,
            created_by=self.manager,
            actor_id_frozen=self.manager.pk,
            creator_principal=f'label_studio_local:{self.manager.pk}',
            name='oversized partial',
            idempotency_key=uuid4(),
            request_hash='a' * 64,
            plan=plan,
            plan_hash=service.digest(plan),
        )
        decoded = []
        self.client.force_authenticate(self.manager)
        with patch.object(JSONField, 'from_db_value', self.decoder(decoded)):
            assert self.client.get(self.base + f'{row.pk}/').status_code == 422
            assert self.client.get(self.base).status_code == 422
        assert not decoded

    def test_capability_twenty_profiles_and_descriptor_4kib_limit(self):
        entries = []
        for i in range(20):
            descriptor = {**self.descriptor, 'profile_id': str(uuid4())}
            entries.append({**self.entries[0], 'descriptor': descriptor, 'digest': c.digest(descriptor)})
        with self.settings(ANNOTATION_REVIEW_CONFIDENCE_PROFILES=entries):
            self.client.force_authenticate(self.manager)
            result = self.client.get(self.base + 'capability/')
            assert result.status_code == 200 and len(result.data['confidence_profiles']) == 20
        descriptor = {**self.descriptor, 'label': 'x' * 4097}
        with self.assertRaises(APIException):
            c.descriptor(descriptor)

    def test_recovery_and_historical_selection_never_materialize_confidence_sources(self):
        preview = self.preview()
        body, key = {**self.args, 'expected_plan_hash': preview.data['plan_hash']}, uuid4()
        result = self.create_batch(body, key=key)
        with patch.object(p, 'materialize', side_effect=AssertionError('no confidence reranking')):
            assert self.create_batch(body, key=key).status_code == 200
            self.client.force_authenticate(self.manager)
            assert self.client.get(self.base + f'{result.data["id"]}/').status_code == 200
