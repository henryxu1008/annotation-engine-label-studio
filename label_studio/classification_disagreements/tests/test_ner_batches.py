import copy
import json
from importlib import import_module
from unittest.mock import patch
from uuid import uuid4

from classification_disagreements import ner, ner_api
from classification_disagreements import ner_batches as b
from classification_disagreements.models import ArbitrationTask, NerBatch
from classification_disagreements.tests import test_ner_api as fixtures
from classification_disagreements.tests.test_ner_metrics import snapshot
from django.db import DatabaseError, connection, transaction
from django.db.models import JSONField
from django.db.models.query import QuerySet
from django.test import override_settings
from django.utils import timezone
from organizations.models import OrganizationMember
from rest_framework.response import Response
from rest_framework.test import APITestCase
from tasks.models import ReviewDecision, Submission, Task, TaskAssignment
from tasks.submissions import _snapshot_hash
from users.models import User


@override_settings(ANNOTATION_NER_DISAGREEMENTS_ENABLED=True, ANNOTATION_NER_BATCHES_ENABLED=True)
class TestNerBatches(APITestCase):
    setUpTestData = classmethod(fixtures.TestNerAPI.setUpTestData.__func__)
    make_source = fixtures.TestNerAPI.make_source

    def write(self, row, spans, mutate=None):
        value = snapshot(spans)
        value['project']['id'] = self.project.pk
        value['task']['id'] = row.assignment.task_id
        if mutate:
            mutate(value)
        row.result_snapshot, row.result_hash = value, _snapshot_hash(value)
        Submission.objects.filter(pk=row.pk).update(result_snapshot=value, result_hash=row.result_hash)

    def setUp(self):
        fixtures.TestNerAPI.setUp(self)
        self.base += 'batches/'
        self.task_id = self.first.assignment.task_id

    def preview(self, ids=None, **extra):
        return self.client.post(self.base + 'preview/', {'name': 'NER frozen 冻结',
            'scope': {'kind': 'tasks', 'task_ids': ids or [self.task_id]}, **extra}, format='json')

    def create(self, preview=None, key=None, expected=201):
        preview = preview or self.preview().data
        body = {'preview_token': preview['preview_token'], 'expected_plan_hash': preview['plan_hash']}
        key = key or str(uuid4())
        result = self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(result.status_code, expected, result.data)
        return result, body, key

    def extra_task(self, owners=None, equal=False):
        rows = []
        for owner in owners or (self.worker, self.manager):
            row = self.make_source(worker=owner)
            if rows:
                row.assignment.task = rows[0].assignment.task
                row.assignment.save()
            self.write(row, [(0, 5, 'PER')] if equal or not rows else [(0, 4, 'PER')])
            rows.append(row)
        return rows

    def item(self, batch, task=None, **query):
        return self.client.get(f'{self.base}{batch}/items/{task or self.task_id}/', query)

    def test_create_frozen_queue_and_pair_detail_do_not_write_business_state(self):
        submissions = list(Submission.objects.order_by('pk').values())
        before = (ReviewDecision.objects.count(), ArbitrationTask.objects.count())
        with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as engine:
            preview = self.preview()
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(engine.call_count, 2)
        result, _, _ = self.create(preview.data)
        batch = NerBatch.objects.get(pk=result.data['id'])
        self.assertEqual(batch.manifest['tasks'][0]['result']['outcome'], 'disagreement')
        self.assertNotIn('Alice', json.dumps(batch.manifest))
        self.assertEqual(batch.manifest['scorer_calls_per_pass'], 1)
        rows = self.client.get(f'{self.base}{batch.pk}/items/').data
        self.assertEqual((rows['count'], rows['display_counts']['disagreement']), (1, 1))
        item = self.item(batch.pk)
        self.assertEqual(item.status_code, 200, item.data)
        self.assertEqual(item.data['item']['comparison']['pairs'][0]['f1'], ner.fraction(1, 2))
        self.assertEqual(item['Cache-Control'], 'private, no-store')
        self.assertEqual(list(Submission.objects.order_by('pk').values()), submissions)
        self.assertEqual((ReviewDecision.objects.count(), ArbitrationTask.objects.count()), before)

    def test_latest_is_per_assignment_and_never_falls_back_from_unusable_revision(self):
        newer = self.make_source(assignment=self.first.assignment, revision=4)
        self.write(newer, [])
        result, _, _ = self.create()
        source_ids = [e['source']['submission_id'] for e in NerBatch.objects.get(pk=result.data['id']).manifest['tasks'][0]['cohort']]
        self.assertEqual(set(source_ids), {newer.pk, self.second.pk})
        Submission.objects.filter(pk=newer.pk).update(status=Submission.Status.SUPERSEDED)
        later, _, _ = self.create()
        item = self.item(later.data['id']).data['item']
        self.assertEqual(item['outcome'], 'unavailable')
        self.assertEqual(item['reason'], 'latest_revision_unusable')
        self.assertEqual(set(p['submission_id'] for p in item['inputs']), {newer.pk, self.second.pk})

    def test_frozen_replay_ignores_new_revision_and_legitimate_superseding(self):
        result, _, _ = self.create()
        old = self.item(result.data['id']).data
        new = self.make_source(assignment=self.first.assignment, revision=2)
        self.write(new, [])
        Submission.objects.filter(pk=self.first.pk).update(status=Submission.Status.SUPERSEDED)
        current = self.item(result.data['id'])
        self.assertEqual(current.status_code, 200, current.data)
        for key in ('calculated_at', 'manifest_hash'):
            self.assertEqual(current.data[key], old[key])
        self.assertEqual(current.data['item'], old['item'])

    def test_missing_participant_stays_missing_after_later_submission(self):
        missing = TaskAssignment.objects.create(project=self.project, task=self.first.assignment.task,
                                                assignee=self.manager, assigned_by=self.manager)
        result, _, _ = self.create()
        row = self.make_source(worker=self.manager, assignment=missing)
        self.write(row, [(0, 5, 'PER')])
        response = self.item(result.data['id'])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['item']['reason'], 'missing_submission')
        self.assertEqual(response.data['item']['input_count'], 3)
        self.assertIsNone(response.data['item']['comparison']['pairs'])

    def test_missing_or_corrupt_frozen_source_masks_answers_without_shrinking_scope(self):
        for delete in (False, True):
            with self.subTest(delete=delete):
                result, _, _ = self.create()
                if delete:
                    self.first.delete()
                else:
                    Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64)
                response = self.item(result.data['id'])
                self.assertEqual(response.status_code, 200, response.data)
                item = response.data['item']
                self.assertEqual((item['outcome'], item['input_count'], item['total_pairs']), ('unavailable', 2, 1))
                self.assertEqual(item['inputs'], [])
                self.assertIsNone(item['comparison']['selected_pair'])
                self.assertNotIn('Alice', json.dumps(response.data))
                if not delete:
                    Submission.objects.filter(pk=self.first.pk).update(result_hash=self.first.result_hash)

    def test_empty_sets_and_missing_values_do_not_become_perfect_scores(self):
        self.write(self.first, [])
        self.write(self.second, [])
        result, _, _ = self.create()
        item = self.item(result.data['id']).data['item']
        self.assertEqual(item['outcome'], 'unavailable')
        self.assertEqual(item['reason'], 'no_entities')
        self.assertIsNone(item['comparison']['pairs'][0]['f1'])

    def test_project_scope_is_bounded_and_does_not_follow_later_tasks(self):
        response = self.preview(scope={'kind': 'project'})
        ids = response.data['task_ids']
        self.extra_task()
        created, _, _ = self.create(response.data)
        self.assertEqual(NerBatch.objects.get(pk=created.data['id']).manifest['task_ids'], ids)

    def test_changed_preview_creates_nothing_and_requires_explicit_repreview(self):
        preview = self.preview().data
        newer = self.make_source(assignment=self.first.assignment, revision=2)
        self.write(newer, [])
        self.create(preview, expected=409)
        self.assertEqual(NerBatch.objects.count(), 0)

    def test_original_key_recovers_metadata_before_expiry_rotation_sources_and_budgets(self):
        created, body, key = self.create()
        self.first.delete()
        with self.settings(SECRET_KEY='new-isolated-key'), patch.object(b, 'PREVIEW_AGE', -1), patch.object(ner, 'MAX_SNAPSHOT_BYTES', 1), patch.object(ner, 'MAX_WORK', 0), patch.object(b, 'MAX_CALLS', 0), patch.object(
                ner_api, 'load', side_effect=AssertionError('Recovery must not load sources')):
            replay = self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertTrue(replay.data['replayed'])
        self.assertEqual(replay.data['id'], created.data['id'])
        self.assertEqual(replay.data['manifest_hash'], created.data['manifest_hash'])
        self.assertEqual(replay.data['idempotency_key'], key)
        self.assertEqual(NerBatch.objects.count(), 1)
        changed = {**body, 'expected_plan_hash': '0' * 64}
        self.assertEqual(self.client.post(self.base, changed, format='json', HTTP_IDEMPOTENCY_KEY=key).status_code, 409)
        self.assertEqual(self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())).status_code, 409)

    def test_reviewer_excludes_whole_self_cohort_before_counts_pagination_and_loading(self):
        other = self.extra_task()
        created, _, _ = self.create(self.preview([self.task_id, other[0].assignment.task_id]).data)
        batch = created.data['id']
        Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64)
        self.client.force_authenticate(self.reviewer)
        with patch.object(ner_api, 'load', wraps=ner_api.load) as load:
            response = self.client.get(f'{self.base}{batch}/items/', {'state': 'all', 'page_size': 1})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['visible_task_count'], 1)
        self.assertNotIn('manifest_hash', response.data)
        self.assertIn('projection_hash', response.data)
        self.assertEqual(response.data['results'][0]['task_id'], other[0].assignment.task_id)
        for call in load.call_args_list:
            self.assertEqual(set(call.args[0].values_list('pk', flat=True)), {r.pk for r in other})
        self.assertEqual(self.item(batch).status_code, 404)
        self.assertEqual(self.client.post(self.base+'preview/', {'name': 'x', 'scope': {'kind':'project'}}, format='json').status_code, 404)

    def test_annotator_foreign_ids_revocation_and_core_modes_fail_closed(self):
        created, body, key = self.create()
        batch = created.data['id']
        self.client.force_authenticate(self.worker)
        for path in ('', 'capability/', f'{batch}/', f'{batch}/items/', f'{batch}/items/{self.task_id}/'):
            self.assertIn(self.client.get(self.base+path).status_code, (403, 404))
        self.client.force_authenticate(self.manager)
        for mode in ({}, {f'{self.org.pk}:{self.project.pk}':'core_only_internal'},
                     {f'{self.org.pk}:{self.project.pk}':'unknown'}):
            with self.settings(ANNOTATION_NER_DISAGREEMENT_MODES=mode):
                self.assertEqual(self.item(batch).status_code, 404)
                self.assertEqual(self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=key).status_code, 404)
        with self.settings(ANNOTATION_NER_BATCHES_ENABLED=False):
            self.assertEqual(self.item(batch).status_code, 404)
        OrganizationMember.objects.filter(user=self.manager, organization=self.org).update(deleted_at=timezone.now())
        self.assertIn(self.item(batch).status_code, (401, 403, 404))
        self.assertIn(self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=key).status_code, (401,403,404))

    def test_invalid_bodies_duplicate_queries_and_foreign_tasks(self):
        for extra in ({'actor':self.manager.pk}, {'scope':{'kind':'tasks','task_ids':[True]}},
                      {'scope':{'kind':'tasks','task_ids':[self.task_id,self.task_id]}}):
            self.assertEqual(self.preview(**extra).status_code, 400)
        self.assertEqual(self.preview([99999999]).status_code, 409)
        created, _, _ = self.create()
        batch = created.data['id']
        self.assertEqual(self.client.get(f'{self.base}{batch}/items/?state=all&state=all').status_code, 400)
        self.assertEqual(self.item(batch, left=self.first.pk).status_code, 400)
        self.assertEqual(self.item(batch, left=self.first.pk, right=self.first.pk).status_code, 400)
        self.assertEqual(self.item(batch, left=self.first.pk, right=99999999).status_code, 400)
        for method in ('put','patch','delete'):
            self.assertEqual(getattr(self.client,method)(f'{self.base}{batch}/').status_code, 405)

    def test_freeze_database_guards_include_pk_and_creator_cleanup(self):
        created, _, _ = self.create()
        row = NerBatch.objects.get(pk=created.data['id'])
        changes = {'id':row.pk+10000,'project_id':self.project.pk+10000,'creator_principal':'foreign',
                   'actor_id_frozen':self.worker.pk,'created_at':timezone.now(),'name':'changed',
                   'idempotency_key':uuid4(),'request_hash':'0'*64,'manifest':{},'manifest_hash':'0'*64,
                   'created_by_id':self.worker.pk}
        for field, value in changes.items():
            with self.subTest(field=field), self.assertRaises(DatabaseError), transaction.atomic():
                NerBatch.objects.filter(pk=row.pk).update(**{field:value})
        NerBatch.objects.filter(pk=row.pk).update(created_by=None)
        self.assertEqual(self.item(row.pk).status_code, 200)

    def test_manifest_corruption_rejects_even_if_admin_rehashes_wrong_rules(self):
        created, _, _ = self.create()
        row = NerBatch.objects.get(pk=created.data['id'])
        migration = import_module('classification_disagreements.migrations.0006_ner_batches')
        editor = connection.schema_editor(atomic=False)
        migration.uninstall(None, editor)
        try:
            manifest = copy.deepcopy(row.manifest)
            manifest['rule'] = 'wrong/v1'
            NerBatch.objects.filter(pk=row.pk).update(manifest=manifest, manifest_hash=ner.digest(manifest))
            self.assertEqual(self.item(row.pk).status_code, 409)
        finally:
            migration.install(None, editor)

    def test_render_tampering_rolls_creation_back_and_does_not_spend_scorer_calls(self):
        preview = self.preview().data
        original = Response.render
        def tamper(response):
            result = original(response)
            if response.status_code == 201:
                response.content = b'{"tampered":true}'
            return result
        with patch.object(Response, 'render', tamper), patch.object(ner, 'Evaluator') as engine:
            _, body, key = self.create(preview, expected=409)
        engine.assert_not_called()
        self.assertEqual(NerBatch.objects.count(), 0)
        replay = self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(replay.status_code, 201, replay.data)

    def test_response_revocation_cannot_commit_hidden_creation(self):
        preview = self.preview().data
        original = Response.render
        def revoke(response):
            result = original(response)
            if response.status_code == 201:
                OrganizationMember.objects.filter(user=self.manager, organization=self.org).update(deleted_at=timezone.now())
            return result
        with patch.object(Response, 'render', revoke):
            result, _, _ = self.create(preview, expected=404)
        self.assertEqual(NerBatch.objects.count(), 0)

    def test_whole_batch_work_and_call_budget_precedes_the_first_library_call(self):
        other = self.extra_task(equal=True)
        ids = [self.task_id, other[0].assignment.task_id]
        plan, _ = b.build_plan(self.project, ids)
        for field, maximum, exact in ((ner,'MAX_WORK',plan['work_units']), (b,'MAX_CALLS',2*plan['scorer_calls_per_pass'])):
            with patch.object(field, maximum, exact):
                self.assertEqual(self.preview(ids).status_code, 200)
            with patch.object(field, maximum, exact-1), patch.object(ner, 'Evaluator') as engine:
                self.assertEqual(self.preview(ids).status_code, 422)
            engine.assert_not_called()

    def test_canonical_and_stored_byte_limits_are_cumulative_inclusive(self):
        used = sum(ner.bounded(r.result_snapshot) for r in (self.first,self.second))
        with patch.object(ner, 'MAX_SNAPSHOT_BYTES', used):
            self.assertEqual(self.preview().status_code, 200)
        with patch.object(ner, 'MAX_SNAPSHOT_BYTES', used-1), patch.object(ner,'Evaluator') as engine:
            self.assertEqual(self.preview().status_code, 422)
        engine.assert_not_called()
        column = connection.ops.quote_name(Submission._meta.db_table)+'.'+connection.ops.quote_name('result_snapshot')
        expression = f'OCTET_LENGTH(CAST({column} AS TEXT))' if connection.vendor=='postgresql' else f'LENGTH(CAST({column} AS BLOB))'
        from django.db.models.expressions import RawSQL
        used = sum(Submission.objects.filter(pk__in=[self.first.pk,self.second.pk]).annotate(size=RawSQL(expression,[])).values_list('size',flat=True))
        with patch.object(ner,'MAX_STORED_BYTES',used):
            self.assertEqual(self.preview().status_code, 200)
        with patch.object(ner,'MAX_STORED_BYTES',used-1), patch.object(JSONField,'from_db_value',side_effect=AssertionError('Decode after SQL budget')):
            with self.assertRaises(ner.Error) as error:
                ner_api.load(Submission.objects.filter(pk__in=[self.first.pk, self.second.pk]))
            self.assertEqual(error.exception.status, 422)

    def test_same_statement_guard_blocks_growth_after_preflight(self):
        original = QuerySet.iterator
        mutated = False
        def iterate(query, *args, **kwargs):
            nonlocal mutated
            if query.model is Submission and not mutated:
                mutated = True
                value = copy.deepcopy(self.first.result_snapshot)
                value['padding'] = 'x'*8192
                Submission.objects.filter(pk=self.first.pk).update(result_snapshot=value, result_hash=_snapshot_hash(value))
            return original(query,*args,**kwargs)
        with patch.object(QuerySet,'iterator',iterate), patch.object(ner,'MAX_STORED_BYTES',8192), patch.object(ner,'Evaluator') as engine:
            self.assertEqual(self.preview().status_code, 422)
        engine.assert_not_called()

    def test_true_task_limit_allows_100_and_rejects_101_without_truncation(self):
        ids = [Task.objects.create(project=self.project,data={}).pk for _ in range(101)]
        self.assertEqual(self.preview(ids[:100]).status_code, 200)
        with patch.object(ner,'Evaluator') as engine:
            self.assertEqual(self.preview(ids).status_code, 422)
        engine.assert_not_called()

    def test_true_twenty_participant_call_limit_is_not_multiplied_by_tasks(self):
        for index in range(18):
            actor = User.objects.create_user(f'ner-batch-limit-{index}@example.test', None, username=f'ner-batch-limit-{index}')
            row = self.make_source(worker=actor)
            row.assignment.task = self.first.assignment.task
            row.assignment.save()
            self.write(row, [(0, 5, 'PER')])
        with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as engine:
            response = self.preview()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(engine.call_count, 380)
        other = self.extra_task()
        with patch.object(ner, 'Evaluator') as engine:
            self.assertEqual(self.preview([self.task_id, other[0].assignment.task_id]).status_code, 422)
        engine.assert_not_called()

    def test_true_two_thousand_cohort_entries_and_twenty_per_task(self):
        tasks = Task.objects.bulk_create([Task(project=self.project, data={}) for _ in range(100)])
        actors = User.objects.bulk_create([User(email=f'ner-cohort-{i}@example.test', username=f'cohort-{i}') for i in range(2000)])
        TaskAssignment.objects.bulk_create([TaskAssignment(project=self.project, task=tasks[i//20], assignee=actors[i],
                                                          assigned_by=self.manager) for i in range(2000)])
        ids = [t.pk for t in tasks]
        plan, _ = b.build_plan(self.project, ids)
        self.assertEqual(sum(len(i['cohort']) for i in plan['tasks']), 2000)
        self.assertEqual(self.preview(ids).status_code, 200)
        with patch.object(b, 'MAX_INPUTS', 1999), patch.object(ner_api, 'load', side_effect=AssertionError('Metadata first')):
            self.assertEqual(self.preview(ids).status_code, 422)
        TaskAssignment.objects.create(project=self.project, task=tasks[0], assignee=self.manager, assigned_by=self.manager)
        self.assertEqual(self.preview([tasks[0].pk]).status_code, 422)

    def test_organizing_ner_is_independent_of_review_classification_and_arbitration(self):
        from access_control.project_operations import READ_WORK, LegacyProjectOperationProvider, ProjectOperationPolicy
        for operations in ((READ_WORK,), ('review', 'organize_classification', 'organize_arbitration', READ_WORK)):
            with patch.object(LegacyProjectOperationProvider, 'evaluate', return_value=ProjectOperationPolicy(
                    'isolated-test-v1', 'active', frozenset(operations))):
                self.assertEqual(self.client.get(self.base+'capability/').data['can_create'], False)
                self.assertEqual(self.preview().status_code, 403)

    def test_stale_authenticated_account_cannot_read_or_recover_after_deactivation(self):
        created, body, key = self.create()
        administrator = User.objects.create_user('ner-security-admin@example.test', None, username='ner-security-admin',
                                                  is_staff=True, is_superuser=True)
        User.objects.filter(pk=self.manager.pk).update(is_active=False, session_actor=administrator)
        self.assertIn(self.item(created.data['id']).status_code, (401, 403, 404))
        self.assertIn(self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=key).status_code, (401,403,404))
        self.assertEqual(NerBatch.objects.count(), 1)

    def test_original_key_does_not_cross_identity_source_even_with_same_actor_and_principal_id(self):
        from dataclasses import replace
        created, body, key = self.create()
        resolve = ner_api.resolve_actor
        def changed(request):
            principal, actor = resolve(request)
            return replace(principal, source='isolated-other-source'), actor
        with patch.object(ner_api, 'resolve_actor', changed):
            response = self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=key)
        self.assertIn(response.status_code, (403,404,409), response.data)
        self.assertEqual(NerBatch.objects.count(), 1)
