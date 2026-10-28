"""Frozen NER aggregation, denial, replay and whole-request cost contracts."""
import copy
from unittest.mock import patch

from access_control.project_operations import READ_WORK, LegacyProjectOperationProvider, ProjectOperationPolicy
from classification_disagreements import ner, ner_api
from classification_disagreements import ner_batches as batches
from classification_disagreements import ner_overview as overview
from classification_disagreements.models import NerBatch
from classification_disagreements.tests import test_ner_batches as f
from django.core import signing
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from organizations.models import OrganizationMember
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, APITestCase, force_authenticate
from tasks.models import Submission, Task, TaskAssignment
from users.models import User


@override_settings(ANNOTATION_NER_DISAGREEMENTS_ENABLED=True, ANNOTATION_NER_BATCHES_ENABLED=True,
                   ANNOTATION_NER_QUALITY_OVERVIEW_ENABLED=True)
class TestNerOverview(APITestCase):
    setUpTestData = classmethod(f.TestNerBatches.setUpTestData.__func__)
    make_source = f.TestNerBatches.make_source
    write = f.TestNerBatches.write
    extra_task = f.TestNerBatches.extra_task
    preview = f.TestNerBatches.preview
    create = f.TestNerBatches.create

    def setUp(self):
        f.TestNerBatches.setUp(self)
        self.freeze()

    def freeze(self, ids=None):
        created, self.body, self.key = self.create(self.preview(ids).data)
        self.batch = NerBatch.objects.get(pk=created.data['id'])
        self.url = f'{self.base}{self.batch.pk}/quality-overview/'
        return self.batch

    def read(self, **query):
        return self.client.get(self.url, query)

    def projection(self):
        from classification_disagreements.ner_overview_api import OverviewAPI
        view = OverviewAPI()
        request = APIRequestFactory().get(self.url)
        force_authenticate(request, self.manager)
        return overview.project(self.batch, view.context(view.initialize_request(request), self.project.pk))[0]

    def mixed(self):
        for row in (self.first, self.second):
            self.write(row, [(0, 5, 'PER')])
        triple = self.extra_task((self.worker, self.reviewer, self.manager))
        self.write(triple[0], [(0, 5, 'PER'), (13, 17, 'ORG')])
        self.write(triple[1], [(0, 5, 'PER'), (19, 20, '人')])
        self.write(triple[2], [])
        empty = self.extra_task((self.worker, self.reviewer))
        for row in empty:
            self.write(row, [])
        missing = self.extra_task((self.worker, self.manager))
        missing[1].delete()
        self.freeze([self.task_id, triple[0].assignment.task_id, empty[0].assignment.task_id,
                     missing[0].assignment.task_id])
        return triple, empty, missing

    def test_task_and_person_macro_empty_missing_denominators_and_actual_calls(self):
        self.mixed()
        with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as scorer:
            result = self.read()
        self.assertEqual(result.status_code, 200, result.data)
        data, c = result.data, result.data['scope_counts']
        self.assertEqual((c['tasks'], c['comparable_tasks'], c['unavailable_tasks']), (4, 2, 2))
        self.assertEqual((c['total_pairs'], c['comparable_pairs'], c['unavailable_pairs']), (6, 4, 2))
        self.assertEqual(c['unavailable_reasons'], [{'reason': 'missing_submission', 'count': 1},
                                                    {'reason': 'no_entities', 'count': 1}])
        self.assertEqual(data['summary']['strict_f1'], ner.fraction(7, 12))
        self.assertEqual(data['summary']['task_coverage'], ner.fraction(1, 2))
        self.assertEqual(data['summary']['pair_coverage'], ner.fraction(2, 3))
        people = {r['actor_id']: r for r in data['annotators']}
        self.assertEqual(people[self.worker.pk]['strict_f1'], ner.fraction(5, 8))
        self.assertEqual(people[self.reviewer.pk]['strict_f1'], ner.fraction(5, 8))
        self.assertEqual(people[self.manager.pk]['strict_f1'], ner.fraction(0, 1))
        self.assertEqual(people[self.worker.pk]['tasks'], 4)
        self.assertEqual(scorer.call_count, 8)
        self.assertEqual(sum(r['comparable_peer_pairs'] for r in people.values()), 2*c['comparable_pairs'])
        self.assertEqual(sum(r['peer_pairs'] for r in people.values()), 2*c['total_pairs'])

    def test_mixed_double_empty_pairs_are_excluded_from_each_correct_denominator(self):
        fourth = User.objects.create_user('ner-overview-fourth@example.test', None, username='ner-fourth')
        rows = self.extra_task((self.worker, self.reviewer, self.manager, fourth))
        for index, row in enumerate(rows):
            self.write(row, [] if index < 2 else [(0, 5, 'PER')])
        self.freeze([rows[0].assignment.task_id])
        data = self.read().data
        self.assertEqual(data['summary']['strict_f1'], ner.fraction(1, 5))
        self.assertEqual(data['summary']['pair_coverage'], ner.fraction(5, 6))
        people = {r['actor_id']: r for r in data['annotators']}
        self.assertEqual(people[self.worker.pk]['pair_coverage'], ner.fraction(2, 3))
        self.assertEqual(people[self.manager.pk]['strict_f1'], ner.fraction(1, 3))
        self.assertEqual(data['scope_counts']['unavailable_pair_reasons'], [{'reason': 'no_entities', 'count': 1}])

    def test_all_empty_null_missing_and_no_participants_never_become_perfect(self):
        for mutate, reason in ((None, 'no_entities'),
                (lambda s: s['annotation'].pop('result'), 'missing_result'),
                (lambda s: s['annotation'].update(result=None), 'missing_result')):
            self.write(self.first, [], mutate)
            self.write(self.second, [])
            self.freeze()
            data = self.read().data
            self.assertEqual(data['availability'], 'unavailable')
            self.assertIsNone(data['summary']['strict_f1'])
            self.assertTrue(data['receipt_verified'])
            self.assertEqual(data['scope_counts']['comparable_pairs'], 0)
            self.assertTrue(all(r['strict_f1'] is None for r in data['annotators']))
            if mutate is None:
                self.assertEqual(data['scope_counts']['unavailable_reasons'], [{'reason': reason, 'count': 1}])
        task = Task.objects.create(project=self.project, data={})
        self.freeze([task.pk])
        data = self.read().data
        self.assertIsNone(data['summary']['pair_coverage'])
        self.assertEqual(data['summary']['task_coverage'], ner.fraction(0, 1))
        self.assertEqual(data['annotators'], [])
        self.assertEqual(data['pagination']['count'], 0)

    def test_replay_pagination_and_new_revision_preserve_full_frozen_result(self):
        first = self.read(page_size=1).data
        newer = self.make_source(assignment=self.first.assignment, revision=2)
        self.write(newer, [])
        Submission.objects.filter(pk=self.first.pk).update(status=Submission.Status.SUPERSEDED)
        second = self.read(frozen=first['frozen'], page=2, page_size=1)
        self.assertEqual(second.status_code, 200, second.data)
        for key in ('summary', 'scope_counts', 'frozen', 'result_hash', 'summarized_at', 'batch_calculated_at'):
            self.assertEqual(second.data[key], first[key])
        self.assertNotEqual(second.data['annotators'][0]['actor_id'], first['annotators'][0]['actor_id'])
        self.assertEqual(self.read(page=2).status_code, 400)

    def test_missing_at_freeze_never_upgrades_after_later_submission(self):
        assignment = TaskAssignment.objects.create(project=self.project, task_id=self.task_id, assignee=self.manager)
        self.freeze()
        before = self.read().data
        row = self.make_source(worker=self.manager, assignment=assignment)
        self.write(row, [(0, 5, 'PER')])
        after = self.read(frozen=before['frozen']).data
        self.assertEqual(after['result_hash'], before['result_hash'])
        self.assertEqual(after['scope_counts']['total_pairs'], 3)
        self.assertIsNone(after['summary']['strict_f1'])

    def test_any_visible_source_failure_masks_whole_overview_and_intact_restoration_replays(self):
        self.mixed()
        first = self.read().data
        Submission.objects.filter(pk=self.first.pk).update(result_hash='0'*64)
        for query in ({}, {'frozen': first['frozen']}):
            response = self.read(**query)
            self.assertEqual(response.status_code, 200, response.data)
            data = response.data
            self.assertEqual(data['scope_counts'], first['scope_counts'])
            for field in ('summary', 'annotators', 'pagination', 'result_hash'):
                self.assertIsNone(data[field])
            self.assertFalse(data['receipt_verified'])
            self.assertEqual(data['source_status'], 'unavailable')
            self.assertEqual(data['frozen'], first['frozen'] if query else None)
        Submission.objects.filter(pk=self.first.pk).update(result_hash=self.first.result_hash)
        self.assertEqual(self.read(frozen=first['frozen']).data['result_hash'], first['result_hash'])
        self.first.delete()
        self.assertIsNone(self.read(frozen=first['frozen']).data['summary'])

    def test_reviewer_exclusion_precedes_source_reads_counts_rows_and_receipts(self):
        manager_receipt = self.read().data['frozen']
        rows = self.extra_task((self.worker, self.manager), equal=True)
        self.freeze([self.task_id, rows[0].assignment.task_id])
        self.first.delete()  # This is a broken, hidden self-cohort task.
        self.client.force_authenticate(self.reviewer)
        load, observed = ner_api.load, []
        def capture(query, *args, **kwargs):
            observed.append(set(query.values_list('pk', flat=True)))
            return load(query, *args, **kwargs)
        with patch.object(ner_api, 'load', capture):
            result = self.read()
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(observed, [{r.pk for r in rows}]*2)
        self.assertEqual(result.data['scope_counts']['tasks'], 1)
        self.assertEqual(result.data['scope']['reference_kind'], 'projection_hash')
        self.assertNotIn(self.reviewer.pk, [r['actor_id'] for r in result.data['annotators']])
        self.assertNotIn('manifest_hash', str(signing.loads(result.data['frozen'], salt=overview.SCHEMA)))
        self.assertEqual(self.read(frozen=manager_receipt).status_code, 404)

    def test_authority_flags_modes_operations_and_no_create_requirement(self):
        for flag in ('ANNOTATION_NER_QUALITY_OVERVIEW_ENABLED', 'ANNOTATION_NER_BATCHES_ENABLED',
                     'ANNOTATION_NER_DISAGREEMENTS_ENABLED'):
            with self.settings(**{flag: False}), patch.object(ner, 'Evaluator') as engine:
                self.assertEqual(self.read().status_code, 404)
                engine.assert_not_called()
        for mode in ('core_only_review', 'unknown'):
            with self.settings(ANNOTATION_NER_DISAGREEMENT_MODES={f'{self.org.pk}:{self.project.pk}': mode}):
                self.assertEqual(self.read().status_code, 404)
        with patch.object(LegacyProjectOperationProvider, 'evaluate', return_value=ProjectOperationPolicy(
                'overview-read/v1', 'active', frozenset({READ_WORK}))):
            self.assertEqual(self.read().status_code, 200)
        with patch.object(LegacyProjectOperationProvider, 'evaluate', return_value=ProjectOperationPolicy(
                'overview-deny/v1', 'active', frozenset({'review', 'organize_ner'}))):
            self.assertEqual(self.read().status_code, 403)
        self.client.force_authenticate(self.worker)
        self.assertIn(self.read().status_code, (403, 404))
        self.client.force_authenticate(self.manager)
        OrganizationMember.objects.filter(user=self.manager, organization=self.org).delete()
        self.assertIn(self.read().status_code, (401, 403, 404))

    def test_bad_receipts_input_and_methods_fail_before_scoring(self):
        first = self.read().data
        payload = signing.loads(first['frozen'], salt=overview.SCHEMA)
        changes = [{'rules': {}}, {'summarized_at': 'not-a-time'}, {'origin': {}}, {'result_hash': 'z'*64}]
        bad = ['broken', '.'+first['frozen']]
        bad += [signing.dumps({**payload, **change}, salt=overview.SCHEMA) for change in changes]
        for token in bad:
            with patch.object(ner, 'Evaluator') as engine:
                self.assertIn(self.read(frozen=token).status_code, (400, 404, 409))
                engine.assert_not_called()
        for query in ({'actor': 1}, {'state': 'all'}, {'page': '0'}, {'page_size': 101}, {'left': 1}):
            self.assertEqual(self.read(**query).status_code, 400)
        self.assertEqual(self.client.get(self.url+'?page=1&page=1').status_code, 400)
        for method in ('post', 'put', 'patch', 'delete'):
            self.assertEqual(getattr(self.client, method)(self.url, {}, format='json').status_code, 405)

    def test_whole_logical_output_receipt_and_input_budget_edges_precede_scorer(self):
        self.mixed()
        data = self.projection()
        size = len(ner.canonical(data))
        with patch.object(overview, 'MAX_BYTES', size):
            self.assertEqual(self.read().status_code, 200)
        with patch.object(overview, 'MAX_BYTES', size-1), patch.object(ner, 'Evaluator') as engine:
            self.assertEqual(self.read(page_size=1).status_code, 422)
            engine.assert_not_called()
        for name, boundary in (('MAX_TASKS', 4), ('MAX_PARTICIPANTS', 3), ('MAX_INPUTS', 9)):
            with patch.object(batches, name, boundary):
                self.assertEqual(self.read().status_code, 200)
            with patch.object(batches, name, boundary-1), patch.object(ner, 'Evaluator') as engine:
                self.assertEqual(self.read().status_code, 422)
                engine.assert_not_called()
        token = self.read().data['frozen']
        with patch.object(overview, 'MAX_TOKEN', len(token.encode())-1), patch.object(ner, 'Evaluator') as engine:
            self.assertEqual(self.read().status_code, 422)
            self.assertEqual(self.read(frozen=token).status_code, 400)
            engine.assert_not_called()

    def test_processing_call_budget_does_not_change_original_key_metadata_recovery(self):
        with patch.object(batches, 'MAX_CALLS', 2):
            self.assertEqual(self.read().status_code, 200)
        with patch.object(batches, 'MAX_CALLS', 1), patch.object(ner, 'Evaluator') as engine:
            self.assertEqual(self.read().status_code, 422)
            replay = self.client.post(self.base, self.body, format='json', HTTP_IDEMPOTENCY_KEY=self.key)
            self.assertEqual(replay.status_code, 200, replay.data)
            self.assertEqual(replay.data['id'], self.batch.pk)
            engine.assert_not_called()

    def test_true_380_call_ceiling_and_191_pair_rejection_without_scorer(self):
        for index in range(18):
            actor = User.objects.create_user(f'ner-overview-limit-{index}@example.test', None, username=f'ner-overview-limit-{index}')
            row = self.make_source(worker=actor)
            row.assignment.task_id = self.task_id
            row.assignment.save()
            self.write(row, [(0, 5, 'PER')])
        self.freeze()
        with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as engine:
            response = self.read()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(engine.call_count, 380)
        self.assertEqual(response.data['scope_counts']['comparable_pairs'], 190)
        with patch.object(batches, 'MAX_CALLS', 379), patch.object(ner, 'Evaluator') as engine:
            self.assertEqual(self.read().status_code, 422)
            engine.assert_not_called()
        other = self.extra_task(equal=True)
        with patch.object(ner, 'Evaluator') as engine:
            self.assertEqual(self.preview([self.task_id, other[0].assignment.task_id]).status_code, 422)
            engine.assert_not_called()

    def test_snapshot_work_and_actual_response_boundaries_precede_engine_and_failure_has_no_fallback(self):
        size = sum(len(ner.canonical(r.result_snapshot)) for r in (self.first, self.second))
        work = self.batch.manifest['work_units']
        for module, name, limit in ((ner, 'MAX_SNAPSHOT_BYTES', size), (ner, 'MAX_WORK', work)):
            with patch.object(module, name, limit):
                self.assertEqual(self.read().status_code, 200)
            with patch.object(module, name, limit-1), patch.object(ner, 'Evaluator') as engine:
                self.assertEqual(self.read().status_code, 422)
                engine.assert_not_called()
        # The paged representation (including the token) has its own cap.
        data = self.projection()
        with patch.object(ner, 'MAX_OUTPUT', len(ner.canonical(data))+1), patch.object(ner, 'Evaluator') as engine:
            self.assertEqual(self.read().status_code, 422)
            engine.assert_not_called()
        with patch.object(ner, 'Evaluator', side_effect=RuntimeError('Synthetic scorer failure')):
            self.assertEqual(self.read().status_code, 503)

    def test_read_is_write_free_manifest_unchanged_and_after_render_changes_discard(self):
        manifest = copy.deepcopy(self.batch.manifest)
        with CaptureQueriesContext(connection) as queries:
            result = self.read()
        self.assertEqual(result.status_code, 200, result.data)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ')) for q in queries))
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.manifest, manifest)
        self.assertEqual(result['Cache-Control'], 'private, no-store')
        render = Response.render
        def mutate(response):
            value = render(response)
            if response.data.get('schema') == overview.SCHEMA:
                Submission.objects.filter(pk=self.first.pk).update(result_hash='0'*64)
            return value
        with patch.object(Response, 'render', mutate):
            self.assertEqual(self.read().status_code, 409)
