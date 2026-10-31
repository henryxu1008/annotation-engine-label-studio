import copy
import json
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from access_control.identity import LocalIdentityProvider
from classification_disagreements import field_api, field_batches, fields
from classification_disagreements.models import FieldBatch
from classification_disagreements.tests import test_field_api as fixtures
from classification_disagreements.tests import test_field_batches as batch_fixtures
from django.db import DatabaseError, transaction
from django.db.models import JSONField
from django.test import override_settings
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import ProjectMember
from rest_framework.exceptions import APIException
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APITestCase
from review_batches import field_priority as p
from review_batches import guarded, ner_priority, service
from review_batches.models import ReviewBatch
from tasks.models import ReviewDecision, Submission, Task, TaskAssignment
from tasks.serializers import SubmissionSerializer
from tasks.submissions import _snapshot_hash, review_submission
from users.models import User
from users.serializers import UserSerializer


class OrganizeExistingFieldPolicy:
    def evaluate(self, project):
        from access_control.project_operations import ORGANIZE_REVIEW, READ_WORK, ProjectOperationPolicy

        return ProjectOperationPolicy('existing-field-review/v1', 'active', frozenset({READ_WORK, ORGANIZE_REVIEW}))


@override_settings(
    ANNOTATION_REVIEW_BATCHES_ENABLED=True,
    ANNOTATION_FIELD_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_FIELD_BATCHES_ENABLED=True,
    ANNOTATION_FIELD_REVIEW_PRIORITY_ENABLED=True,
    ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=False,
    ANNOTATION_NER_BATCHES_ENABLED=False,
)
class TestFieldPriority(APITestCase):
    setUpTestData = classmethod(fixtures.TestFieldAPI.setUpTestData.__func__)
    make_source = fixtures.TestFieldAPI.make_source
    write = batch_fixtures.TestFieldBatches.write

    def setUp(self):
        fixtures.TestFieldAPI.setUp(self)
        mode = self.settings(ANNOTATION_REVIEW_BATCH_MODES={f'{self.org.pk}:{self.project.pk}': 'fork_review'})
        mode.enable()
        self.addCleanup(mode.disable)
        self.analysis = self.freeze_analysis()
        self.base = f'/api/projects/{self.project.pk}/review-batches/'
        self.args = {
            'name': 'Field priority 字段',
            'rate': 50,
            'seed': 'stable',
            'priority': {
                'kind': p.KIND,
                'batch_id': self.analysis['id'],
                'manifest_hash': self.analysis['manifest_hash'],
            },
        }
        self.independent = User.objects.create_user(
            'independent-field-priority@example.test', active_organization=self.org
        )
        self.org.add_user(self.independent)
        ProjectMember.objects.create(project=self.project, user=self.independent, role='reviewer')

    def freeze_analysis(self, ids=None):
        path = f'/api/projects/{self.project.pk}/field-disagreements/batches/'
        r = self.client.post(
            path + 'preview/',
            {'name': 'Frozen fields', 'scope': {'kind': 'tasks', 'task_ids': ids or [self.first.assignment.task_id]}},
            format='json',
        )
        assert r.status_code == 200, r.data
        result = self.client.post(
            path,
            {'preview_token': r.data['preview_token'], 'expected_plan_hash': r.data['plan_hash']},
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        )
        assert result.status_code == 201, result.data
        return result.data

    def preview(self, args=None):
        return self.client.post(self.base + 'preview/', args or self.args, format='json')

    def body(self):
        r = self.preview()
        assert r.status_code == 200, r.data
        return {**self.args, 'expected_plan_hash': r.data['plan_hash']}

    def create(self, body=None, key=None):
        return self.client.post(
            self.base, body or self.body(), format='json', HTTP_IDEMPOTENCY_KEY=str(key or uuid4())
        )

    def detail(self, identifier):
        return self.client.get(f'{self.base}{identifier}/')

    def refreeze(self):
        self.analysis = self.freeze_analysis()
        self.args['priority'].update(batch_id=self.analysis['id'], manifest_hash=self.analysis['manifest_hash'])

    def test_deterministic_quota_full_pins_and_exact_actual_cost_without_business_writes(self):
        self.make_source()
        self.args['rate'] = 33
        before = list(Submission.objects.order_by('pk').values())
        with patch.object(fields, 'normalize', wraps=fields.normalize) as n, patch.object(
            fields, 'equal', wraps=fields.equal
        ) as e:
            r = self.preview()
        assert r.status_code == 200, r.data
        assert n.call_count == e.call_count == 12
        assert self.preview().data == r.data
        plan = r.data['plan']
        assert plan['algorithm'] == p.ALGORITHM
        chosen = [c for c in plan['candidates'] if c['selected']]
        assert len(chosen) == 1 and chosen[0]['submission_id'] in (self.first.pk, self.second.pk)
        assert len([e for e in plan['priority']['evidence'] if e['reason'] == 'not_covered']) == 1
        assert plan['priority']['evidence'][0]['source_pin'] == field_api.pin(self.first)
        assert r.data['processing_cost']['total'] == {'normalized_values': 12, 'field_equalities': 12}
        assert r.data['processing_cost']['scorer_calls'] == 0
        created = self.create({**self.args, 'expected_plan_hash': r.data['plan_hash']})
        assert created.status_code == 201, created.data
        assert list(Submission.objects.order_by('pk').values()) == before
        assert not ReviewDecision.objects.exists()
        assert '上海' not in json.dumps(plan, ensure_ascii=False)

    def test_global_per_person_and_full_rate_preserve_ceil(self):
        self.make_source()
        for per_person, expected in ((False, 1), (True, 2)):
            self.args.update(rate=1, per_submitter=per_person)
            r = self.preview()
            assert r.status_code == 200, r.data
            assert sum(c['selected'] for c in r.data['plan']['candidates']) == expected
        self.args['rate'] = 100
        assert all(c['selected'] for c in self.preview().data['plan']['candidates'])

    def test_format_empty_agreement_and_missing_contract(self):
        for left, right, reason in (
            ((' 张三 ', 'A\r\nB', '001'), ('张三', 'A\nB', '001'), 'frozen_conflict'),
            (('x', 'y', 'z'), ('x', 'y', 'z'), 'frozen_agreement'),
            (('', '', ''), ('', ' \t', ''), 'metric_unavailable'),
            (('', '', ''), ('x', '', ''), 'frozen_conflict'),
        ):
            self.write(self.first, left)
            self.write(self.second, right)
            self.refreeze()
            r = self.preview()
            assert r.status_code == 200, r.data
            assert {e['reason'] for e in r.data['plan']['priority']['evidence']} == {reason}
            assert r.data['processing_cost']['total']['normalized_values'] == 12
        TaskAssignment.objects.create(
            project=self.project, task=self.first.assignment.task, assignee=self.manager, assigned_by=self.manager
        )
        self.refreeze()
        r = self.preview()
        assert r.status_code == 200, r.data
        assert {e['reason'] for e in r.data['plan']['priority']['evidence']} == {'metric_unavailable'}
        assert r.data['processing_cost']['total'] == field_batches.ZERO

    def test_new_revision_same_values_never_inherits_old_conflict(self):
        body = self.body()
        newer = self.make_source(assignment=self.first.assignment, revision=2)
        self.write(newer)
        assert self.create(body).status_code == 409
        r = self.preview()
        assert r.status_code == 200, r.data
        evidence = next(e for e in r.data['plan']['priority']['evidence'] if e['submission_id'] == newer.pk)
        assert evidence == {
            'submission_id': newer.pk,
            'state': 'unknown',
            'reason': 'revision_mismatch',
            'calculation_hash': None,
            'source_pin': None,
        }

    def test_empty_candidate_pool_invalidates_preview_and_leaves_key_reusable(self):
        body, key = self.body(), str(uuid4())
        Submission.objects.filter(assignment__project=self.project).update(status='superseded')
        result = self.create(body, key)
        assert result.status_code == 409, result.data
        assert result.data['display_context']['reason'] == 'REVIEW_BATCH_PREVIEW_CHANGED'
        assert not ReviewBatch.objects.exists()
        Submission.objects.filter(assignment__project=self.project).update(status='pending')
        assert self.create(body, key).status_code == 201

    def test_source_loss_and_manifest_mismatch_reject_before_calculation(self):
        bad = copy.deepcopy(self.args)
        bad['priority']['manifest_hash'] = '0' * 64
        with patch.object(fields, 'normalize', side_effect=AssertionError('Must not score')):
            assert self.preview(bad).status_code == 409
            self.first.delete()
            assert self.preview().status_code == 409

    def test_recovery_precedes_missing_source_and_tightened_budgets(self):
        body, key = self.body(), str(uuid4())
        created = self.create(body, key)
        assert created.status_code == 201, created.data
        FieldBatch.objects.filter(pk=self.analysis['id']).delete()
        self.first.delete()
        with patch.object(p, 'load', side_effect=AssertionError('No source recovery')), patch.object(
            fields, 'normalize', side_effect=AssertionError('No score recovery')
        ), patch.object(service, 'MAX_CANDIDATES', 1), patch.object(p, 'MAX_BYTES', 1024):
            r = self.create(body, key)
        assert r.status_code == 200 and r.data['id'] == created.data['id'], r.data
        assert r.data['replayed'] and r.data['processing_cost']['hydration_passes'] == 0
        assert self.create({**body, 'seed': 'changed'}, key).status_code == 409
        assert ReviewBatch.objects.count() == 1

    def test_self_cohort_excluded_before_load_count_pagination_and_direct_review(self):
        self.args['rate'] = 100
        created = self.create()
        assert created.status_code == 201, created.data
        identifier = created.data['id']
        self.client.force_authenticate(self.reviewer)
        with patch.object(p, 'load', side_effect=AssertionError('Hidden sources')):
            r = self.client.get(self.base)
            assert r.status_code == 200 and r.data['count'] == 0, r.data
            assert self.detail(identifier).status_code == 404
            assert self.client.get(f'{self.base}{identifier}/queue/').status_code == 404
            assert self.client.post(
                f'/api/submissions/{self.first.pk}/review/?review_batch={identifier}',
                {'decision': 'approved'},
                format='json',
            ).status_code in (403, 404)
        self.client.force_authenticate(self.independent)
        with patch.object(fields, 'normalize', side_effect=AssertionError('Historical reads never score')):
            r = self.detail(identifier)
            assert r.status_code == 200 and r.data['candidate_count'] == 2, r.data
            assert 'manifest_hash' not in json.dumps(r.data, default=str)
            assert self.client.get(f'{self.base}{identifier}/queue/').data['count'] == 2

    def test_current_gates_and_roles_and_default_compatibility(self):
        cap = self.client.get(self.base + 'capability/')
        assert cap.status_code == 200 and cap.data['can_prioritize_field'] and not cap.data['can_prioritize']
        for setting in (
            'ANNOTATION_FIELD_REVIEW_PRIORITY_ENABLED',
            'ANNOTATION_FIELD_BATCHES_ENABLED',
            'ANNOTATION_FIELD_DISAGREEMENTS_ENABLED',
            'ANNOTATION_REVIEW_BATCHES_ENABLED',
        ):
            with self.settings(**{setting: False}):
                assert self.preview().status_code in (403, 404)
        for actor in (self.worker, self.independent):
            self.client.force_authenticate(actor)
            assert self.preview().status_code in (403, 404)
        self.client.force_authenticate(self.manager)
        created = self.create()
        with self.settings(ANNOTATION_FIELD_REVIEW_PRIORITY_ENABLED=False):
            assert self.client.get(self.base).data['count'] == 0
            assert self.detail(created.data['id']).status_code == 403
            legacy = self.preview({'name': 'legacy', 'rate': 50, 'seed': 'stable'})
            assert legacy.status_code == 200 and legacy.data['plan']['algorithm'] == service.ALGORITHM
        with self.settings(ANNOTATION_FIELD_DISAGREEMENT_MODES={}):
            assert self.preview().status_code == 403

    def test_disabled_field_gate_keeps_ordinary_rows_and_excludes_markers_before_decode(self):
        created = self.create()
        assert created.status_code == 201, created.data
        args = {key: value for key, value in self.args.items() if key != 'priority'}
        preview = self.preview(args)
        ordinary = self.create({**args, 'expected_plan_hash': preview.data['plan_hash']})
        assert ordinary.status_code == 201, ordinary.data
        for adapter in (p, ner_priority):
            for marker in (
                {'algorithm': adapter.ALGORITHM},
                {'priority': {'kind': adapter.KIND}},
                {'priority': {'schema': adapter.SCHEMA}},
            ):
                plan = {**marker, 'padding': 'disabled-marker-must-not-decode'}
                ReviewBatch.objects.create(
                    project=self.project,
                    created_by=self.manager,
                    actor_id_frozen=self.manager.pk,
                    creator_principal='synthetic',
                    name='Malformed protected marker',
                    idempotency_key=uuid4(),
                    request_hash='a' * 64,
                    plan=plan,
                    plan_hash=service.digest(plan),
                )
        decoder = JSONField.from_db_value

        def decode(field, value, expression, db):
            assert not isinstance(value, str) or 'disabled-marker-must-not-decode' not in value
            assert not isinstance(value, str) or p.ALGORITHM not in value
            return decoder(field, value, expression, db)

        with self.settings(ANNOTATION_FIELD_REVIEW_PRIORITY_ENABLED=False), patch.object(
            JSONField, 'from_db_value', decode
        ):
            for actor in (self.manager, self.independent):
                self.client.force_authenticate(actor)
                result = self.client.get(self.base)
                assert result.status_code == 200, result.data
                assert result.data['count'] == 1
                assert [row['id'] for row in result.data['results']['batches']] == [ordinary.data['id']]

    def test_direct_review_hidden_cohort_and_current_gates_precede_snapshot_decode(self):
        self.args['rate'] = 100
        identifier = self.create().data['id']
        url = f'/api/submissions/{self.first.pk}/review/?review_batch={identifier}'
        decoder = JSONField.from_db_value

        def decode(field, value, expression, db):
            assert field.name != 'result_snapshot', 'Denied review decoded a source snapshot'
            return decoder(field, value, expression, db)

        self.client.force_authenticate(self.reviewer)
        with patch.object(JSONField, 'from_db_value', decode):
            assert self.client.post(url, {'decision': 'approved'}, format='json').status_code in (403, 404)
            with self.assertRaises(APIException):
                review_submission(
                    submission=self.first, reviewer=self.reviewer, decision='approved', review_batch_id=identifier
                )
        self.client.force_authenticate(self.independent)
        for settings in (
            {'ANNOTATION_FIELD_REVIEW_PRIORITY_ENABLED': False},
            {'ANNOTATION_FIELD_BATCHES_ENABLED': False},
            {'ANNOTATION_FIELD_DISAGREEMENTS_ENABLED': False},
            {'ANNOTATION_REVIEW_BATCHES_ENABLED': False},
            {'ANNOTATION_FIELD_DISAGREEMENT_MODES': {}},
            {'ANNOTATION_REVIEW_BATCH_MODES': {}},
        ):
            with self.settings(**settings), patch.object(JSONField, 'from_db_value', decode):
                result = self.client.post(url, {'decision': 'approved'}, format='json')
                assert result.status_code in (403, 404), result.data
        assert not ReviewDecision.objects.exists()

    def test_direct_review_stored_budget_precedes_snapshot_decode(self):
        self.args['rate'] = 100
        identifier = self.create().data['id']
        self.client.force_authenticate(self.independent)
        decoder = JSONField.from_db_value

        def decode(field, value, expression, db):
            assert field.name != 'result_snapshot', 'Over-budget review decoded a source snapshot'
            return decoder(field, value, expression, db)

        with patch.object(fields, 'MAX_STORED_BYTES', 1), patch.object(JSONField, 'from_db_value', decode):
            result = self.client.post(
                f'/api/submissions/{self.first.pk}/review/?review_batch={identifier}',
                {'decision': 'approved'},
                format='json',
            )
        assert result.status_code == 422, result.data
        assert not ReviewDecision.objects.exists()
        assert Submission.objects.get(pk=self.first.pk).status == 'pending'

    @override_settings(ANNOTATION_FIXED_PACKAGES_ENABLED=True)
    def test_direct_review_response_only_hydrates_through_guarded_loader(self):
        self.args['rate'] = 100
        identifier = self.create().data['id']
        package = self.client.post(
            f'/api/projects/{self.project.pk}/task-packages/',
            {'name': 'Review scope', 'task_ids': [self.first.assignment.task_id]},
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        )
        assert package.status_code == 201, package.data
        self.client.force_authenticate(self.independent)
        decoder, load = JSONField.from_db_value, guarded.load
        depth, calls = [], []

        def bounded(*args, **kwargs):
            depth.append(True)
            calls.append(kwargs.get('for_serialization', False))
            try:
                return load(*args, **kwargs)
            finally:
                depth.pop()

        def decode(field, value, expression, db):
            assert field.name != 'result_snapshot' or depth, 'Unbounded source hydration'
            return decoder(field, value, expression, db)

        with patch.object(guarded, 'load', bounded), patch.object(JSONField, 'from_db_value', decode):
            result = self.client.post(
                f'/api/submissions/{self.first.pk}/review/?review_batch={identifier}&task_package={package.data["id"]}',
                {'decision': 'approved'},
                format='json',
            )
        assert result.status_code == 200, result.data
        assert True in calls
        assert result.data['result_snapshot'] == self.first.result_snapshot
        assert result.data['review']['reviewer']['id'] == self.independent.pk
        assert result.data['status'] == 'approved'
        assert ReviewDecision.objects.filter(submission=self.first).count() == 1

    def test_direct_review_post_serialization_revocation_and_corruption_roll_back(self):
        self.args['rate'] = 100
        identifier = self.create().data['id']
        self.client.force_authenticate(self.independent)
        serialize = SubmissionSerializer.to_representation
        for change in ('revocation', 'source', 'representation'):

            def alter(serializer, instance):
                data = serialize(serializer, instance)
                if change == 'revocation':
                    ProjectMember.objects.filter(project=self.project, user=self.independent).update(enabled=False)
                elif change == 'source':
                    Submission.objects.filter(pk=self.first.pk).update(result_hash='f' * 64)
                else:
                    data['result_snapshot']['tampered'] = True
                return data

            with patch.object(SubmissionSerializer, 'to_representation', alter):
                result = self.client.post(
                    f'/api/submissions/{self.first.pk}/review/?review_batch={identifier}',
                    {'decision': 'approved'},
                    format='json',
                )
            assert result.status_code in (403, 404) if change == 'revocation' else result.status_code == 409
            assert not ReviewDecision.objects.exists()
            assert Submission.objects.get(pk=self.first.pk).status == 'pending'
            assert ProjectMember.objects.get(project=self.project, user=self.independent).enabled

    def test_revocation_blocks_original_key_and_ordinary_reads(self):
        body, key = self.body(), str(uuid4())
        r = self.create(body, key)
        self.client.force_authenticate(self.independent)
        assert self.detail(r.data['id']).status_code == 200
        ProjectMember.objects.filter(project=self.project, user=self.independent).delete()
        assert self.detail(r.data['id']).status_code in (403, 404)
        self.client.force_authenticate(self.manager)
        OrganizationMember.objects.filter(organization=self.org, user=self.manager).update(deleted_at=timezone.now())
        assert self.create(body, key).status_code in (403, 404)

    def test_union_limits_overlap_once_and_unknown_candidates_are_included(self):
        total = sum(fields.bounded(r.result_snapshot, fields.MAX_SNAPSHOT_BYTES) for r in (self.first, self.second))
        with patch.object(fields, 'MAX_SNAPSHOT_BYTES', total):
            r = self.preview()
            assert r.status_code == 200, r.data
        with patch.object(fields, 'MAX_SNAPSHOT_BYTES', total - 1), patch.object(fields, 'normalize') as n:
            assert self.preview().status_code == 422
            n.assert_not_called()
        extra = self.make_source()
        extra.result_snapshot['padding'] = 'decoder-marker' * 1000
        Submission.objects.filter(pk=extra.pk).update(
            result_snapshot=extra.result_snapshot, result_hash=_snapshot_hash(extra.result_snapshot)
        )
        decoder = JSONField.from_db_value

        def decode(field, value, expression, db):
            assert not isinstance(value, str) or 'decoder-marker' not in value
            return decoder(field, value, expression, db)

        with patch.object(JSONField, 'from_db_value', decode), patch.object(fields, 'MAX_STORED_BYTES', 1):
            assert self.preview().status_code == 422

    def test_cpu_and_plan_limits_precede_first_normalization(self):
        for target, key, limit in (
            (field_batches, 'MAX_NORMALIZATIONS', 5),
            (field_batches, 'MAX_EQUALITIES', 5),
            (p, 'MAX_BYTES', 100),
        ):
            with patch.object(target, key, limit), patch.object(fields, 'normalize') as n, patch.object(
                fields, 'equal'
            ) as e:
                assert self.preview().status_code == 422
                n.assert_not_called()
                e.assert_not_called()

    def test_last_equality_source_change_rolls_back_and_key_remains_available(self):
        body, key = self.body(), str(uuid4())
        equal, calls = fields.equal, []

        def mutate(a, b):
            calls.append(1)
            if len(calls) == 12:
                Submission.objects.filter(pk=self.first.pk).update(result_hash='f' * 64)
            return equal(a, b)

        with patch.object(fields, 'equal', mutate):
            r = self.create(body, key)
        assert r.status_code == 409, r.data
        assert not ReviewBatch.objects.exists()
        assert self.create(body, key).status_code == 201

    def test_bad_ack_representation_rolls_back(self):
        body = self.body()
        original = JSONRenderer.render

        def render(renderer, data, *args, **kwargs):
            result = original(renderer, data, *args, **kwargs)
            return result.replace(b'"replayed":false', b'"replayed":true')

        with patch.object(JSONRenderer, 'render', render):
            r = self.create(body)
        assert r.status_code == 409 and not ReviewBatch.objects.exists(), r.data

    def test_last_counter_cannot_replace_already_rendered_receipt(self):
        body, key = self.body(), str(uuid4())
        original_response, equal = p.response, fields.equal
        retained, calls = [], []

        def capture(*args, **kwargs):
            result = original_response(*args, **kwargs)
            retained.append(result)
            return result

        def corrupt(left, right):
            calls.append(1)
            if len(calls) == 12:
                retained[0].data['id'] += 1
                retained[0].content = JSONRenderer().render(retained[0].data)
            return equal(left, right)

        with patch.object(p, 'response', capture), patch.object(fields, 'equal', corrupt):
            result = self.create(body, key)
        assert result.status_code == 409, result.data
        assert not ReviewBatch.objects.exists()
        assert self.create(body, key).status_code == 201

    def test_strict_input_and_local_pin_validation_and_historical_source_loss(self):
        for value in (True, 1.0, '1', 2**53):
            args = copy.deepcopy(self.args)
            args['priority']['batch_id'] = value
            assert self.preview(args).status_code == 400
        args = {**self.args, 'actor_id': self.manager.pk}
        assert self.preview(args).status_code == 400
        plan = self.preview().data['plan']
        plan['priority']['evidence'][0]['source_pin']['revision'] += 1
        with self.assertRaises(APIException):
            p.validate(plan)
        created = self.create()
        original = ReviewBatch.objects.get(pk=created.data['id']).plan
        FieldBatch.objects.filter(pk=self.analysis['id']).delete()
        assert self.detail(created.data['id']).status_code == 200
        assert ReviewBatch.objects.get(pk=created.data['id']).plan == original

    def test_real_1000_1001_candidate_boundary_precedes_snapshot_loading(self):
        self.bulk_unknown(998)
        result = self.preview()
        assert result.status_code == 200, result.data
        assert len(result.data['plan']['candidates']) == 1000
        self.bulk_unknown(1)
        with patch.object(p, 'load', side_effect=AssertionError('ID denominator first')):
            assert self.preview().status_code == 422

    def test_entire_source_batch_survives_candidate_filtering(self):
        a = self.make_source(worker=self.manager)
        b = self.make_source(worker=self.independent)
        b.assignment.task = a.assignment.task
        b.assignment.save()
        for row in (a, b):
            self.write(row)
        self.analysis = self.freeze_analysis([self.first.assignment.task_id, a.assignment.task_id])
        self.args['priority'].update(batch_id=self.analysis['id'], manifest_hash=self.analysis['manifest_hash'])
        self.args['submitter_ids'] = [self.worker.pk]
        Submission.objects.filter(pk=b.pk).update(result_hash='f' * 64)
        with patch.object(fields, 'normalize', side_effect=AssertionError('All source scopes validate first')):
            assert self.preview().status_code == 409

    def test_field_plan_page_bytes_are_guarded_before_decoder_and_inclusive(self):
        first, second = self.create(), self.create()
        assert first.status_code == second.status_code == 201
        total = sum(p.canonical_size(row.plan) for row in ReviewBatch.objects.all())
        with patch.object(p, 'MAX_PAGE_BYTES', total):
            assert self.client.get(self.base).status_code == 200
        with patch.object(p, 'MAX_PAGE_BYTES', total - 1):
            assert self.client.get(self.base).status_code == 422
        decode = JSONField.from_db_value

        def guarded_decode(field, value, *args):
            if isinstance(value, str) and p.SCHEMA in value:
                raise AssertionError('Page overflow decoded before SQL gate')
            return decode(field, value, *args)

        with patch.object(p, 'MAX_PAGE_STORED', 1), patch.object(JSONField, 'from_db_value', guarded_decode):
            assert self.client.get(self.base).status_code == 422

    def test_rehashed_local_plan_cannot_change_pin_rule_scope_or_selection(self):
        original = self.preview().data['plan']
        for mutation in (
            lambda v: v['priority']['evidence'][0]['source_pin'].update(assignee_id=True),
            lambda v: v['priority']['participants'][0].update(task_id=float(self.first.assignment.task_id)),
            lambda v: v['priority'].update(queue_rule='normalized-only/v2'),
            lambda v: v['candidates'][0].update(selected=not v['candidates'][0]['selected']),
            lambda v: v['priority']['evidence'][0].update(state='agreement'),
        ):
            value = copy.deepcopy(original)
            mutation(value)
            with self.assertRaises(APIException):
                p.validate(value)

    def test_true_400_7600_work_boundary_and_tightened_processing_limit(self):
        rows = [self.first, self.second]
        for i in range(18):
            actor = User.objects.create_user(f'field-priority-boundary-{i}@example.test', active_organization=self.org)
            row = self.make_source(worker=actor)
            row.assignment.task = self.first.assignment.task
            row.assignment.save()
            rows.append(row)
        xml = (
            '<View><Text name="doc" value="$text"/>'
            + ''.join(f'<TextArea name="f{i}" toName="doc" maxSubmissions="1"/>' for i in range(20))
            + '</View>'
        )
        for row in rows:
            value = copy.deepcopy(self.first.result_snapshot)
            value['project']['label_config'] = xml
            value['annotation']['result'] = [
                {'id': str(i), 'type': 'textarea', 'from_name': f'f{i}', 'to_name': 'doc', 'value': {'text': ['x']}}
                for i in range(20)
            ]
            Submission.objects.filter(pk=row.pk).update(result_snapshot=value, result_hash=_snapshot_hash(value))
        self.refreeze()
        with patch.object(fields, 'normalize', wraps=fields.normalize) as n, patch.object(
            fields, 'equal', wraps=fields.equal
        ) as e:
            result = self.preview()
        assert result.status_code == 200, result.data
        assert n.call_count == 800 and e.call_count == 15200
        assert result.data['processing_cost']['per_pass'] == {'normalized_values': 400, 'field_equalities': 7600}
        for name, limit in (('MAX_NORMALIZATIONS', 399), ('MAX_EQUALITIES', 7599)):
            with patch.object(field_batches, name, limit), patch.object(
                fields, 'normalize', side_effect=AssertionError('Preflight first')
            ):
                assert self.preview().status_code == 422

    def test_organizing_existing_field_needs_no_field_create_or_review_authority(self):
        with self.settings(ANNOTATION_PROJECT_OPERATION_PROVIDER=f'{__name__}.OrganizeExistingFieldPolicy'):
            result = self.create()
            assert result.status_code == 201, result.data
            self.client.force_authenticate(self.independent)
            assert self.detail(result.data['id']).status_code == 200
            assert (
                self.client.post(
                    f'/api/submissions/{self.first.pk}/review/?review_batch={result.data["id"]}',
                    {'decision': 'approved'},
                    format='json',
                ).status_code
                == 403
            )
        self.client.force_authenticate(self.manager)
        with self.settings(ANNOTATION_PROJECT_OPERATION_PROVIDER='review_batches.tests.test_batches.ReadOnlyPolicy'):
            assert self.preview().status_code == 403

    def test_frozen_local_receipt_database_guards(self):
        created = self.create()
        row = ReviewBatch.objects.get(pk=created.data['id'])
        with self.assertRaises(DatabaseError), transaction.atomic():
            ReviewBatch.objects.filter(pk=row.pk).update(plan_hash='0' * 64)
        p.validate(row.plan, row)

    def test_real_token_roles_revocation_and_source_bound_recovery(self):
        self.org.jwt.legacy_api_tokens_enabled = True
        self.org.jwt.save(update_fields=['legacy_api_tokens_enabled'])
        session_body = self.body()
        session_key = str(uuid4())
        session_created = self.create(session_body, session_key)
        assert session_created.status_code == 201
        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.manager.get_token().key}')
        token_body = self.body()
        created = self.create(token_body)
        assert created.status_code == 201, created.data
        # When the provider gives tokens a different source identity, an old
        # session receipt must not be borrowed even by the same human actor.
        old = ReviewBatch.objects.get(pk=session_created.data['id']).plan['priority']['creator']
        current = self.preview().data['plan']['priority']['creator']
        if current['source'] != old['source']:
            assert self.create(session_body, session_key).status_code == 409
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.worker.get_token().key}')
        assert self.detail(created.data['id']).status_code in (403, 404)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.reviewer.get_token().key}')
        assert self.client.get(self.base).data['count'] == 0
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.independent.get_token().key}')
        assert self.detail(created.data['id']).status_code == 200
        ProjectMember.objects.filter(project=self.project, user=self.independent).update(enabled=False)
        with patch.object(field_api, 'load', side_effect=AssertionError('Revoked token cannot hydrate')):
            assert self.detail(created.data['id']).status_code in (403, 404)

    def test_foreign_ids_and_actor_fields_do_not_expand_authority(self):
        for key in ('actor_id', 'reviewer_id', 'creator', 'source'):
            args = {**self.args, key: self.manager.pk}
            assert self.preview(args).status_code == 400
        bad = copy.deepcopy(self.args)
        bad['priority']['batch_id'] += 100000
        with patch.object(field_api, 'load', side_effect=AssertionError('Foreign origin cannot hydrate')):
            assert self.preview(bad).status_code == 409
        url = self.base
        self.base = f'/api/projects/{self.other.pk}/review-batches/'
        assert self.preview().status_code in (403, 404)
        self.base = url

    def bulk_unknown(self, count):
        tasks = Task.objects.bulk_create([Task(project=self.project, data={}) for _ in range(count)])
        assignments = TaskAssignment.objects.bulk_create(
            [
                TaskAssignment(project=self.project, task=t, assignee=self.worker, assigned_by=self.manager)
                for t in tasks
            ]
        )
        rows = []
        for assignment in assignments:
            value = {'project': {'id': self.project.pk}, 'task': {'id': assignment.task_id}}
            rows.append(
                Submission(
                    assignment=assignment,
                    revision=1,
                    submitted_by=self.worker,
                    result_snapshot=value,
                    result_hash=_snapshot_hash(value),
                )
            )
        return Submission.objects.bulk_create(rows)

    def test_create_render_time_withdrawal_rolls_back_and_original_key_is_reusable(self):
        body, key = self.body(), str(uuid4())
        render = JSONRenderer.render

        def withdraw(renderer, data, *args, **kwargs):
            if isinstance(data, dict) and data.get('replayed') is False:
                OrganizationMember.objects.filter(organization=self.org, user=self.manager).update(
                    deleted_at=timezone.now()
                )
            return render(renderer, data, *args, **kwargs)

        with patch.object(JSONRenderer, 'render', withdraw):
            result = self.create(body, key)
        assert result.status_code in (403, 404), result.data
        assert not ReviewBatch.objects.exists()
        assert self.create(body, key).status_code == 201

    def test_same_principal_and_human_cannot_borrow_receipt_from_another_source(self):
        body, key = self.body(), str(uuid4())
        assert self.create(body, key).status_code == 201
        resolve = LocalIdentityProvider.resolve

        def alternate(provider, request):
            return replace(resolve(provider, request), source='isolated-alternate-provider')

        with patch.object(LocalIdentityProvider, 'resolve', alternate):
            with patch.object(field_api, 'load', side_effect=AssertionError('No source load on conflicting receipt')):
                assert self.create(body, key).status_code == 409

    def test_guarded_review_metadata_has_no_per_submission_queries(self):
        self.args['rate'] = 100
        created = self.create()
        self.client.force_authenticate(self.independent)
        for source in (self.first, self.second):
            reviewed = self.client.post(
                f'/api/submissions/{source.pk}/review/',
                {'decision': 'rejected', 'reason': 'Synthetic correction'},
                format='json',
            )
            assert reviewed.status_code == 200, reviewed.data
        expected_annotations = {row.pk: row.annotation_id for row in (self.first, self.second)}
        rows = p.load(Submission.objects.filter(pk__in=[self.first.pk, self.second.pk]))
        with self.assertNumQueries(0):
            for row in rows:
                assert row.review.reason == 'Synthetic correction'
                assert row.review.reviewer_id == self.independent.pk
                assert row.review.created_at
                assert row.submitted_at
                assert row.annotation_id == expected_annotations[row.pk]
        result = self.detail(created.data['id'])
        assert result.status_code == 200, result.data
        assert all(item['review_reason'] == 'Synthetic correction' for item in result.data['items'])

    def test_guarded_submission_projection_eagerly_loads_serialized_fields(self):
        created = self.create()
        self.client.force_authenticate(self.independent)
        reviewed = self.client.post(
            f'/api/submissions/{self.first.pk}/review/',
            {'decision': 'rejected', 'reason': 'Synthetic correction'},
            format='json',
        )
        assert reviewed.status_code == 200, reviewed.data
        rows = p.load(Submission.objects.filter(pk__in=[self.first.pk, self.second.pk]), for_serialization=True)
        # Isolate eager relation/column loading from the upstream user serializer's
        # organization presentation queries, whose contract is unchanged here.
        with patch.object(UserSerializer, 'to_representation', lambda _, user: {'id': user.pk}):
            with self.assertNumQueries(0):
                data = SubmissionSerializer(rows, many=True).data
        assert len(data) == 2
        assert data[0]['review']['reason'] == 'Synthetic correction'
        assert data[0]['review']['reviewer']['id'] == self.independent.pk
        assert data[0]['submitted_by']['id'] == self.first.submitted_by_id
        assert data[1]['review'] is None
        assert self.detail(created.data['id']).status_code == 200
