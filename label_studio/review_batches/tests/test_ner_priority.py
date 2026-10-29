import copy
import json
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from access_control.identity import LocalIdentityProvider
from classification_disagreements import ner, ner_api, ner_batches
from classification_disagreements.models import NerBatch
from classification_disagreements.tests import test_ner_api as fixtures
from classification_disagreements.tests import test_ner_batches as batch_fixtures
from django.db import DatabaseError, transaction
from django.db.models import JSONField
from django.test import override_settings
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import ProjectMember
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APITestCase
from review_batches import ner_priority as p
from review_batches import service
from review_batches.models import ReviewArbitrationHandoff, ReviewBatch
from tasks.models import ReviewDecision, Submission, Task, TaskAssignment
from tasks.serializers import SubmissionSerializer
from tasks.submissions import _snapshot_hash
from users.models import User


@override_settings(
    ANNOTATION_REVIEW_BATCHES_ENABLED=True,
    ANNOTATION_NER_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_NER_BATCHES_ENABLED=True,
    ANNOTATION_NER_REVIEW_PRIORITY_ENABLED=True,
    ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=False,
    ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=False,
)
class TestNerPriority(APITestCase):
    setUpTestData = classmethod(fixtures.TestNerAPI.setUpTestData.__func__)
    make_source = fixtures.TestNerAPI.make_source
    write = batch_fixtures.TestNerBatches.write

    def setUp(self):
        fixtures.TestNerAPI.setUp(self)
        settings = self.settings(ANNOTATION_REVIEW_BATCH_MODES={f'{self.org.pk}:{self.project.pk}': 'fork_review'})
        settings.enable()
        self.addCleanup(settings.disable)
        self.analysis = self.freeze_analysis()
        self.base = f'/api/projects/{self.project.pk}/review-batches/'
        self.args = {
            'name': 'NER priority 冲突优先',
            'rate': 50,
            'seed': 'stable',
            'priority': {
                'kind': p.KIND,
                'batch_id': self.analysis['id'],
                'manifest_hash': self.analysis['manifest_hash'],
            },
        }
        self.independent = User.objects.create_user(
            'independent-ner-priority@example.test', active_organization=self.org
        )
        self.org.add_user(self.independent)
        ProjectMember.objects.create(project=self.project, user=self.independent, role='reviewer')

    def freeze_analysis(self, ids=None):
        path = f'/api/projects/{self.project.pk}/ner-disagreements/batches/'
        result = self.client.post(
            path + 'preview/',
            {'name': 'Frozen NER', 'scope': {'kind': 'tasks', 'task_ids': ids or [self.first.assignment.task_id]}},
            format='json',
        )
        assert result.status_code == 200, result.data
        created = self.client.post(
            path,
            {'preview_token': result.data['preview_token'], 'expected_plan_hash': result.data['plan_hash']},
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        )
        assert created.status_code == 201, created.data
        return created.data

    def preview(self, args=None):
        return self.client.post(self.base + 'preview/', args or self.args, format='json')

    def body(self):
        result = self.preview()
        assert result.status_code == 200, result.data
        return {**self.args, 'expected_plan_hash': result.data['plan_hash']}

    def create(self, body=None, key=None):
        return self.client.post(
            self.base, body or self.body(), format='json', HTTP_IDEMPOTENCY_KEY=str(key or uuid4())
        )

    def detail(self, batch):
        return self.client.get(f'{self.base}{batch}/')

    def test_deterministic_conflict_first_exact_quota_and_no_business_writes(self):
        self.make_source()
        self.args['rate'] = 33
        before = list(Submission.objects.order_by('pk').values())
        with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as counter:
            result = self.preview()
        assert result.status_code == 200, result.data
        assert counter.call_count == 2
        assert self.preview().data == result.data
        plan = result.data['plan']
        assert plan['algorithm'] == p.ALGORITHM
        expected = min(
            [service.identity(self.first), service.identity(self.second)],
            key=lambda x: (service.digest([p.ALGORITHM, self.project.pk, 'stable', x]), x['submission_id']),
        )
        assert [x['submission_id'] for x in plan['candidates'] if x['selected']] == [expected['submission_id']]
        assert [e['state'] for e in plan['priority']['evidence']].count('unknown') == 1
        assert all(word not in json.dumps(plan) for word in ('Alice', 'result_snapshot', 'engine_inputs'))
        with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as counter:
            created = self.create({**self.args, 'expected_plan_hash': result.data['plan_hash']})
        assert created.status_code == 201, created.data
        assert counter.call_count == 2
        assert created.data['plan_hash'] == result.data['plan_hash']
        assert list(Submission.objects.order_by('pk').values()) == before
        assert not ReviewDecision.objects.exists() and not ReviewArbitrationHandoff.objects.exists()

    def test_global_and_per_submitter_rounding_and_all_selection(self):
        self.make_source()
        self.args['rate'] = 1
        assert sum(x['selected'] for x in self.preview().data['plan']['candidates']) == 1
        self.args['per_submitter'] = True
        r = self.preview()
        assert r.status_code == 200, r.data
        assert sum(x['selected'] for x in r.data['plan']['candidates']) == 2
        self.args['rate'] = 100
        assert all(x['selected'] for x in self.preview().data['plan']['candidates'])

    def test_empty_and_missing_preserve_ner_unavailable_semantics(self):
        self.write(self.first, [])
        self.write(self.second, [])
        self.analysis = self.freeze_analysis()
        self.args['priority'].update(batch_id=self.analysis['id'], manifest_hash=self.analysis['manifest_hash'])
        with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as counter:
            r = self.preview()
        assert r.status_code == 200, r.data
        assert counter.call_count == 0
        assert {e['reason'] for e in r.data['plan']['priority']['evidence']} == {'metric_unavailable'}
        self.write(self.first, [(0, 5, 'PER')])
        self.analysis = self.freeze_analysis()
        self.args['priority'].update(batch_id=self.analysis['id'], manifest_hash=self.analysis['manifest_hash'])
        assert {e['state'] for e in self.preview().data['plan']['priority']['evidence']} == {'conflict'}
        TaskAssignment.objects.create(
            project=self.project, task=self.first.assignment.task, assignee=self.manager, assigned_by=self.manager
        )
        self.analysis = self.freeze_analysis()
        self.args['priority'].update(batch_id=self.analysis['id'], manifest_hash=self.analysis['manifest_hash'])
        assert {e['reason'] for e in self.preview().data['plan']['priority']['evidence']} == {'metric_unavailable'}

    def test_revision_mismatch_is_unknown_and_invalidates_old_preview(self):
        body = self.body()
        newer = self.make_source(assignment=self.first.assignment, revision=2)
        self.write(newer, [(0, 5, 'PER')])
        result = self.create(body)
        assert result.status_code == 409, result.data
        assert not ReviewBatch.objects.exists()
        result = self.preview()
        assert result.status_code == 200, result.data
        row = next(e for e in result.data['plan']['priority']['evidence'] if e['submission_id'] == newer.pk)
        assert row == {
            'submission_id': newer.pk,
            'state': 'unknown',
            'reason': 'revision_mismatch',
            'calculation_hash': None,
        }

    def test_manifest_mismatch_or_current_source_change_rejects_before_scoring(self):
        bad = copy.deepcopy(self.args)
        bad['priority']['manifest_hash'] = '0' * 64
        with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as counter:
            assert self.preview(bad).status_code == 409
            self.write(self.first, [])
            assert self.preview().status_code == 409
        assert counter.call_count == 0
        assert not ReviewBatch.objects.exists()

    def test_original_key_recovers_metadata_before_missing_sources_and_tighter_budgets(self):
        body, key = self.body(), str(uuid4())
        created = self.create(body, key)
        assert created.status_code == 201, created.data
        NerBatch.objects.filter(pk=self.analysis['id']).delete()
        self.first.delete()
        with patch.object(ner_api, 'load', side_effect=AssertionError('No source hydration on recovery')):
            with patch.object(ner, 'Evaluator', side_effect=AssertionError('No counter on recovery')):
                with patch.object(ner, 'MAX_SNAPSHOT_BYTES', 1):
                    with patch.object(service, 'MAX_CANDIDATES', 1):
                        recovered = self.create(body, key)
        assert recovered.status_code == 200, recovered.data
        assert recovered.data['id'] == created.data['id'] and recovered.data['replayed']
        assert ReviewBatch.objects.count() == 1
        changed = {**body, 'seed': 'other'}
        assert self.create(changed, key).status_code == 409

    def test_gates_and_role_capabilities_are_independent_from_classification(self):
        cap = self.client.get(self.base + 'capability/')
        assert cap.status_code == 200
        assert cap.data['can_prioritize_ner'] and not cap.data['can_prioritize']
        for setting in (
            'ANNOTATION_NER_REVIEW_PRIORITY_ENABLED',
            'ANNOTATION_NER_BATCHES_ENABLED',
            'ANNOTATION_NER_DISAGREEMENTS_ENABLED',
        ):
            with self.settings(**{setting: False}):
                assert self.preview().status_code in (403, 404)
        with self.settings(
            ANNOTATION_NER_DISAGREEMENT_MODES={f'{self.org.pk}:{self.project.pk}': 'core_only_internal'}
        ):
            assert self.preview().status_code in (403, 404)
        self.client.force_authenticate(self.worker)
        assert self.preview().status_code in (403, 404)
        self.client.force_authenticate(self.independent)
        assert self.preview().status_code in (403, 404)
        self.client.force_authenticate(self.manager)
        assert self.client.post(
            f'/api/submissions/{self.first.pk}/review/', {'decision': 'approved'}, format='json'
        ).status_code in (403, 404)

    def test_reviewer_whole_cohort_exclusion_precedes_count_page_and_sources(self):
        self.args['rate'] = 100
        created = self.create()
        assert created.status_code == 201, created.data
        identifier = created.data['id']
        self.client.force_authenticate(self.reviewer)
        with patch.object(ner_api, 'load', side_effect=AssertionError('Hidden answers must not load')):
            listed = self.client.get(self.base)
            assert listed.status_code == 200, listed.data
            assert listed.data['count'] == 0
            assert self.detail(identifier).status_code == 404
            assert self.client.get(f'{self.base}{identifier}/queue/').status_code == 404
            assert self.client.post(
                f'/api/submissions/{self.first.pk}/review/?review_batch={identifier}',
                {'decision': 'approved'},
                format='json',
            ).status_code in (403, 404)
        self.client.force_authenticate(self.independent)
        with patch.object(ner, 'Evaluator', side_effect=AssertionError('Historical reads never score')):
            detail = self.detail(identifier)
            assert detail.status_code == 200, detail.data
            assert detail.data['candidate_count'] == 2 and 'priority_source' not in detail.data
            queue = self.client.get(f'{self.base}{identifier}/queue/')
            assert queue.status_code == 200, queue.data
            assert queue.data['count'] == 2
        assert 'manifest_hash' not in json.dumps(detail.data, default=str)
        assert 'participants' not in json.dumps(detail.data, default=str)

    def test_disable_ner_strategy_hides_only_ner_metadata_and_denies_replay(self):
        created = self.create()
        with self.settings(ANNOTATION_NER_REVIEW_PRIORITY_ENABLED=False):
            assert self.client.get(self.base).data['count'] == 0
            assert self.detail(created.data['id']).status_code == 403
            normal = self.client.post(
                self.base + 'preview/', {'name': 'old', 'rate': 100, 'seed': 'old'}, format='json'
            )
            assert normal.status_code == 200, normal.data

    def test_revocation_rejects_reads_and_original_key_recovery(self):
        body, key = self.body(), str(uuid4())
        created = self.create(body, key)
        self.client.force_authenticate(self.independent)
        assert self.detail(created.data['id']).status_code == 200
        ProjectMember.objects.filter(project=self.project, user=self.independent).delete()
        assert self.detail(created.data['id']).status_code in (403, 404)
        assert self.client.get(f'{self.base}{created.data["id"]}/queue/').status_code in (403, 404)
        self.client.force_authenticate(self.manager)
        OrganizationMember.objects.filter(organization=self.org, user=self.manager).update(deleted_at=timezone.now())
        with patch.object(ner_api, 'load', side_effect=AssertionError('Revoked recovery must not hydrate')):
            assert self.create(body, key).status_code in (403, 404)

    def test_union_byte_budget_counts_overlap_once_and_includes_unknown_candidates(self):
        total = sum(len(ner.canonical(row.result_snapshot)) for row in (self.first, self.second))
        with patch.object(ner, 'MAX_SNAPSHOT_BYTES', total):
            r = self.preview()
        assert r.status_code == 200, r.data
        with patch.object(ner, 'MAX_SNAPSHOT_BYTES', total - 1):
            with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as counter:
                assert self.preview().status_code == 422
            assert counter.call_count == 0
        extra = self.make_source()
        extra.result_snapshot['payload'] = 'unknown-budget-marker' * 100
        Submission.objects.filter(pk=extra.pk).update(
            result_snapshot=extra.result_snapshot, result_hash=_snapshot_hash(extra.result_snapshot)
        )
        original = JSONField.from_db_value

        def decode(field, value, expression, db):
            if isinstance(value, str) and 'unknown-budget-marker' in value:
                raise AssertionError('Oversized snapshot decoded before SQL gate')
            return original(field, value, expression, db)

        with patch.object(JSONField, 'from_db_value', decode):
            with patch.object(ner, 'MAX_STORED_BYTES', 1):
                with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as counter:
                    assert self.preview().status_code == 422
                assert counter.call_count == 0

    def test_call_and_work_and_response_budgets_precede_first_counter(self):
        for target, value in ((('ner_batches', 'MAX_CALLS'), 1), (('ner', 'MAX_WORK'), 1), (('p', 'MAX_BYTES'), 1)):
            module = {'ner_batches': ner_batches, 'ner': ner, 'p': p}[target[0]]
            with patch.object(module, target[1], value):
                with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as counter:
                    r = self.preview()
                assert r.status_code == 422, r.data
                assert counter.call_count == 0

    def test_source_change_at_last_counter_rolls_back_and_keeps_original_key(self):
        body, key = self.body(), str(uuid4())
        real = ner.check_engine
        calls = []

        def changed(prepared):
            real(prepared)
            calls.append(1)
            if len(calls) == 2:
                Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64)

        with patch.object(ner, 'check_engine', changed):
            r = self.create(body, key)
        assert r.status_code == 409, r.data
        assert not ReviewBatch.objects.exists()
        Submission.objects.filter(pk=self.first.pk).update(result_hash=self.first.result_hash)
        assert self.create(body, key).status_code == 201

    def test_render_tampering_and_read_time_revocation_fail_closed(self):
        original = JSONRenderer.render

        def corrupt(renderer, data, *args, **kwargs):
            if isinstance(data, dict) and 'plan' in data:
                data['plan_hash'] = '0' * 64
            return original(renderer, data, *args, **kwargs)

        with patch.object(JSONRenderer, 'render', corrupt):
            assert self.preview().status_code == 409
        created = self.create()
        self.client.force_authenticate(self.independent)
        changed = []

        def revoke(renderer, data, *args, **kwargs):
            if isinstance(data, dict) and data.get('algorithm') == p.ALGORITHM and not changed:
                changed.append(1)
                ProjectMember.objects.filter(project=self.project, user=self.independent).delete()
            return original(renderer, data, *args, **kwargs)

        with patch.object(JSONRenderer, 'render', revoke):
            assert self.detail(created.data['id']).status_code in (403, 404)

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
        with patch.object(ner_api, 'load', side_effect=AssertionError('Revoked token cannot hydrate')):
            assert self.detail(created.data['id']).status_code in (403, 404)

    def test_foreign_ids_and_actor_fields_do_not_expand_authority(self):
        for key in ('actor_id', 'reviewer_id', 'creator', 'source'):
            args = {**self.args, key: self.manager.pk}
            assert self.preview(args).status_code == 400
        bad = copy.deepcopy(self.args)
        bad['priority']['batch_id'] += 100000
        with patch.object(ner_api, 'load', side_effect=AssertionError('Foreign origin cannot hydrate')):
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

    def test_true_1000_1001_candidate_boundary_and_no_truncation(self):
        self.bulk_unknown(998)
        with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as counter:
            result = self.preview()
        assert result.status_code == 200, result.data
        assert len(result.data['plan']['candidates']) == 1000
        assert counter.call_count == 2
        self.bulk_unknown(1)
        with patch.object(ner_api, 'load', side_effect=AssertionError('ID denominator must reject before JSON')):
            assert self.preview().status_code == 422

    def test_source_analysis_limits_cannot_be_lifted_by_review_filters(self):
        for name, limit in [('MAX_TASKS', 0), ('MAX_PARTICIPANTS', 1), ('MAX_INPUTS', 1), ('MAX_CALLS', 1)]:
            with patch.object(ner_batches, name, limit):
                with patch.object(ner_api, 'load', side_effect=AssertionError('Frozen envelope preflight first')):
                    assert self.preview().status_code == 422
        args = copy.deepcopy(self.args)
        args['submitter_ids'] = [self.worker.pk]
        with patch.object(ner_batches, 'MAX_CALLS', 1):
            assert self.preview(args).status_code == 422

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

    def test_frozen_priority_remains_historical_after_source_loss(self):
        created = self.create()
        row = ReviewBatch.objects.get(pk=created.data['id'])
        frozen = copy.deepcopy(row.plan)
        NerBatch.objects.filter(pk=self.analysis['id']).delete()
        self.first.delete()
        with patch.object(ner, 'Evaluator', side_effect=AssertionError('Historical facts never rescore')):
            result = self.detail(row.pk)
        assert result.status_code == 200, result.data
        assert result.data['candidate_count'] == 2
        assert any(i['status'] == 'unavailable' for i in result.data['items'])
        assert ReviewBatch.objects.get(pk=row.pk).plan == frozen

    def test_ner_plan_cannot_enter_classification_handoff(self):
        row = self.create().data['id']
        path = f'{self.base}{row}/sources/{self.first.pk}/arbitration-context/'
        with self.settings(ANNOTATION_REVIEW_ARBITRATION_HANDOFF_ENABLED=True):
            assert self.client.get(path).status_code in (403, 404, 409)
        assert not ReviewArbitrationHandoff.objects.exists()

    def test_same_principal_and_human_cannot_borrow_receipt_from_another_source(self):
        body, key = self.body(), str(uuid4())
        assert self.create(body, key).status_code == 201
        resolve = LocalIdentityProvider.resolve

        def alternate(provider, request):
            return replace(resolve(provider, request), source='isolated-alternate-provider')

        with patch.object(LocalIdentityProvider, 'resolve', alternate):
            with patch.object(ner_api, 'load', side_effect=AssertionError('No source load on conflicting receipt')):
                assert self.create(body, key).status_code == 409

    def test_real_380_counter_boundary_and_next_realizable_382_rejects_before_scoring(self):
        for index in range(18):
            actor = User.objects.create_user(f'ner-priority-limit-{index}@example.test', None)
            row = self.make_source(worker=actor)
            row.assignment.task = self.first.assignment.task
            row.assignment.save()
            self.write(row, [(0, 5, 'PER')])
        self.analysis = self.freeze_analysis()
        self.args['priority'].update(batch_id=self.analysis['id'], manifest_hash=self.analysis['manifest_hash'])
        with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as counter:
            result = self.preview()
        assert result.status_code == 200, result.data
        assert counter.call_count == 380
        # A second two-input task adds one pair per pass: totals are even.
        left = self.make_source()
        right = self.make_source(worker=self.reviewer)
        right.assignment.task = left.assignment.task
        right.assignment.save()
        self.write(left, [(0, 5, 'PER')])
        self.write(right, [(0, 5, 'PER')])
        # Public NerBatch creation cannot produce 382-call V1 input. Insert an
        # owned deliberately invalid envelope to verify the consumer rejects it
        # at Manifest validation, without loading snapshots or invoking counters.
        with patch.object(ner_batches, 'MAX_CALLS', 382):
            plan, _ = ner_batches.build_plan(self.project, [self.first.assignment.task_id, left.assignment.task_id])
        assert plan['scorer_calls_per_pass'] == 191
        previous = NerBatch.objects.get(pk=self.analysis['id'])
        manifest = {**previous.manifest, **plan}
        extra = NerBatch.objects.create(
            project=self.project,
            created_by=self.manager,
            creator_principal=previous.creator_principal,
            actor_id_frozen=self.manager.pk,
            name=previous.name,
            idempotency_key=uuid4(),
            request_hash=service.digest({'over': 382}),
            manifest=manifest,
            manifest_hash=service.digest(manifest),
        )
        self.args['priority'].update(batch_id=extra.pk, manifest_hash=extra.manifest_hash)
        with patch.object(ner_api, 'load', side_effect=AssertionError('Envelope before snapshots')):
            with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as counter:
                assert self.preview().status_code == 409
        assert counter.call_count == 0

    def test_metadata_and_receipt_cost_zero_and_review_reads_never_score(self):
        body, key = self.body(), str(uuid4())
        created = self.create(body, key)
        with patch.object(ner_api, 'load', side_effect=AssertionError('Metadata never loads snapshots')):
            with patch.object(ner, 'Evaluator', side_effect=AssertionError('Metadata never scores')):
                assert self.client.get(self.base + 'capability/').status_code == 200
                assert self.client.get(self.base).status_code == 200
                assert self.create(body, key).status_code == 200
        with patch.object(ner, 'Evaluator', side_effect=AssertionError('Review source validation is not NER scoring')):
            with patch.object(ner_api, 'load', wraps=ner_api.load) as loader:
                assert self.detail(created.data['id']).status_code == 200
                assert loader.call_count > 0
            self.client.force_authenticate(self.independent)
            assert self.client.get(f"{self.base}{created.data['id']}/queue/").status_code == 200

    def test_rework_successor_growth_after_serialization_rejects_before_json_decoder(self):
        self.args['rate'] = 100
        created = self.create()
        self.client.force_authenticate(self.independent)
        rejected = self.client.post(
            f'/api/submissions/{self.first.pk}/review/',
            {'decision': 'rejected', 'reason': 'Synthetic rework'},
            format='json',
        )
        assert rejected.status_code == 200, rejected.data
        newer = self.make_source(assignment=self.first.assignment, revision=2)
        self.write(newer, [(0, 5, 'PER')])
        represent = SubmissionSerializer.to_representation
        decode = JSONField.from_db_value
        marker = 'ner-successor-growth-marker'
        changed = []

        def serialize(serializer, row):
            value = represent(serializer, row)
            if row.pk == newer.pk and not changed:
                changed.append(1)
                snapshot = copy.deepcopy(newer.result_snapshot)
                snapshot[marker] = 'x' * 16384
                Submission.objects.filter(pk=newer.pk).update(
                    result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
                )
            return value

        def guarded_decoder(field, value, *args):
            if isinstance(value, str) and marker in value:
                raise AssertionError('Growing successor decoded before SQL guard')
            return decode(field, value, *args)

        with patch.object(ner, 'MAX_STORED_BYTES', 16384):
            with patch.object(SubmissionSerializer, 'to_representation', serialize):
                with patch.object(JSONField, 'from_db_value', guarded_decoder):
                    result = self.client.get(f"{self.base}{created.data['id']}/rework/{self.first.pk}/")
        assert changed
        assert result.status_code == 422, result.data

    def test_uncovered_candidate_with_missing_submitter_cannot_freeze_invalid_plan(self):
        row = self.make_source()
        Submission.objects.filter(pk=row.pk).update(submitted_by=None)
        with patch.object(ner, 'Evaluator', side_effect=AssertionError('Validate receipt before scoring')):
            assert self.preview().status_code == 409
        assert not ReviewBatch.objects.exists()
