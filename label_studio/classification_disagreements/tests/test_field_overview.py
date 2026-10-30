"""Field task/peer macro, current authority, immutable replay and actual work."""
import copy
from unittest.mock import patch

from classification_disagreements import field_api, fields
from classification_disagreements import field_batches as batches
from classification_disagreements import field_overview as overview
from classification_disagreements.models import FieldBatch
from classification_disagreements.tests import test_field_batches as f
from django.core import signing
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from organizations.models import OrganizationMember
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, APITestCase, force_authenticate
from tasks.models import ReviewDecision, Submission, TaskAssignment


@override_settings(
    ANNOTATION_FIELD_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_FIELD_BATCHES_ENABLED=True,
    ANNOTATION_FIELD_QUALITY_OVERVIEW_ENABLED=True,
)
class TestFieldOverview(APITestCase):
    setUpTestData = classmethod(f.TestFieldBatches.setUpTestData.__func__)
    make_source = f.TestFieldBatches.make_source
    write = f.TestFieldBatches.write
    extra_task = f.TestFieldBatches.extra_task
    preview = f.TestFieldBatches.preview
    create = f.TestFieldBatches.create

    def setUp(self):
        f.TestFieldBatches.setUp(self)
        self.freeze()

    def freeze(self, ids=None):
        created, self.body, self.key = self.create(self.preview(ids).data)
        self.batch = FieldBatch.objects.get(pk=created.data['id'])
        self.url = f'{self.base}{self.batch.pk}/quality-overview/'
        return self.batch

    def read(self, **query):
        return self.client.get(self.url, query)

    def projection(self):
        from classification_disagreements.field_overview_api import OverviewAPI

        view = OverviewAPI()
        request = APIRequestFactory().get(self.url)
        force_authenticate(request, self.manager)
        return overview.project(self.batch, view.context(view.initialize_request(request), self.project.pk))[0]

    def mixed(self, extra=False):
        for row in (self.first, self.second):
            self.write(row, ('X',) * 3)
        triple = self.extra_task((self.worker, self.reviewer, self.manager))
        for row, v in zip(triple, (' A ', 'A', 'B')):
            self.write(row, (v,) * 3)
        ids = [self.task_id, triple[0].assignment.task_id]
        groups = []
        if extra:
            for values in (('', '', 'Z'), ('', ''), ('X', 'X')):
                rows = self.extra_task((self.worker, self.reviewer, self.manager)[: len(values)])
                for row, value in zip(rows, values):
                    self.write(row, (value,) * 3)
                groups.append(rows)
                ids.append(rows[0].assignment.task_id)
            groups[-1][-1].delete()
        self.freeze(ids)
        return triple, groups

    def test_task_macro_is_not_pair_micro_or_mean_people_and_exact_cost(self):
        self.mixed()
        with patch.object(fields, 'normalize', wraps=fields.normalize) as n, patch.object(
            fields, 'equal', wraps=fields.equal
        ) as e, patch.object(field_api, 'load', wraps=field_api.load) as loader:
            result = self.read()
        self.assertEqual(result.status_code, 200, result.data)
        data = result.data
        self.assertEqual(data['summary']['raw_agreement'], fields.fraction(1, 2))
        self.assertEqual(data['summary']['normalized_agreement'], fields.fraction(2, 3))
        people = {r['actor_id']: r for r in data['annotators']}
        self.assertEqual(people[self.worker.pk]['normalized_agreement'], fields.fraction(3, 4))
        self.assertEqual(people[self.reviewer.pk]['normalized_agreement'], fields.fraction(3, 4))
        self.assertEqual(people[self.manager.pk]['normalized_agreement'], fields.fraction(0, 1))
        self.assertEqual((n.call_count, e.call_count, loader.call_count), (30, 48, 2))
        self.assertEqual(
            data['processing_cost']['per_pass'],
            {
                'normalized_values': 15,
                'field_equalities': 24,
                'aggregation_pair_visits': 4,
                'annotator_pair_updates': 8,
            },
        )
        self.assertEqual(data['processing_cost']['scorer_calls'], 0)
        self.assertEqual(data['scope_counts']['comparable_pairs'], 4)
        self.assertEqual(sum(r['comparable_peer_pairs'] for r in people.values()), 8)

    def test_mixed_empty_peers_missing_and_all_empty_preserve_distinct_denominators(self):
        self.mixed(extra=True)
        result = self.read()
        self.assertEqual(result.status_code, 200, result.data)
        data, c = result.data, result.data['scope_counts']
        self.assertEqual((c['tasks'], c['comparable_tasks'], c['unavailable_tasks']), (5, 3, 2))
        self.assertEqual((c['total_pairs'], c['comparable_pairs'], c['unavailable_pairs']), (9, 6, 3))
        self.assertEqual(data['summary']['raw_agreement'], fields.fraction(1, 3))
        self.assertEqual(data['summary']['normalized_agreement'], fields.fraction(4, 9))
        people = {r['actor_id']: r for r in data['annotators']}
        self.assertEqual(people[self.worker.pk]['normalized_agreement'], fields.fraction(1, 2))
        self.assertEqual(people[self.worker.pk]['comparable_peer_pairs'], 4)
        self.assertEqual(data['processing_cost']['per_pass']['aggregation_pair_visits'], 8)
        self.assertEqual(data['processing_cost']['per_pass']['annotator_pair_updates'], 12)
        self.assertEqual(data['processing_cost']['per_pass']['normalized_values'], 30)

    def test_all_empty_is_verified_null_not_perfect_and_still_performs_work(self):
        self.write(self.first, ('',) * 3)
        self.write(self.second, (' \t',) * 3)
        self.freeze()
        with patch.object(fields, 'normalize', wraps=fields.normalize) as n:
            result = self.read()
        self.assertEqual(result.status_code, 200, result.data)
        self.assertTrue(result.data['receipt_verified'])
        self.assertEqual(result.data['reason'], 'no_comparable_tasks')
        self.assertIsNone(result.data['summary']['raw_agreement'])
        self.assertIsNone(result.data['summary']['normalized_agreement'])
        self.assertEqual(
            result.data['scope_counts']['unavailable_pair_reasons'], [{'reason': 'no_values', 'count': 1}]
        )
        self.assertEqual(n.call_count, 12)
        self.assertEqual(result.data['processing_cost']['total']['field_equalities'], 12)

    def test_no_assignments_has_zero_people_but_keeps_unavailable_task(self):
        TaskAssignment.objects.filter(task_id=self.task_id).delete()
        self.freeze()
        result = self.read()
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data['annotators'], [])
        self.assertEqual(result.data['pagination']['count'], 0)
        self.assertEqual(result.data['scope_counts']['tasks'], 1)
        self.assertIsNone(result.data['summary']['raw_agreement'])

    def test_signed_replay_keeps_full_result_time_and_original_batch(self):
        first = self.read().data
        before = copy.deepcopy(self.batch.manifest)
        new = self.make_source(assignment=self.first.assignment, revision=2)
        self.write(new, ('new',) * 3)
        Submission.objects.filter(pk=self.first.pk).update(status=Submission.Status.SUPERSEDED)
        result = self.read(frozen=first['frozen'], page_size=1)
        self.assertEqual(result.status_code, 200, result.data)
        for key in ('scope', 'scope_counts', 'summary', 'result_hash', 'summarized_at', 'frozen'):
            self.assertEqual(result.data[key], first[key])
        second_page = self.read(frozen=first['frozen'], page_size=1, page=2)
        self.assertEqual(second_page.status_code, 200, second_page.data)
        self.assertEqual(second_page.data['result_hash'], first['result_hash'])
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.manifest, before)

    def test_source_loss_masks_whole_overview_and_exact_restoration_replays(self):
        first = self.read().data
        row = Submission.objects.filter(pk=self.first.pk).values().get()
        self.first.delete()
        for query in ({}, {'frozen': first['frozen']}):
            lost = self.read(**query)
            self.assertEqual(lost.status_code, 200, lost.data)
            self.assertEqual(lost.data['scope_counts'], first['scope_counts'])
            for key in ('summary', 'annotators', 'pagination', 'result_hash'):
                self.assertIsNone(lost.data[key])
            self.assertFalse(lost.data['receipt_verified'])
            self.assertEqual(lost.data['reason'], 'source_unavailable')
        Submission.objects.create(**row)
        restored = self.read(frozen=first['frozen'])
        self.assertEqual(restored.status_code, 200, restored.data)
        for key in ('summary', 'annotators', 'frozen', 'result_hash', 'summarized_at'):
            self.assertEqual(restored.data[key], first[key])

    def test_reviewer_filters_whole_self_cohort_before_loading_and_counts(self):
        others = self.extra_task((self.worker, self.manager), equal=True)
        hidden_id = self.task_id
        self.freeze([hidden_id, others[0].assignment.task_id])
        Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64)
        self.client.force_authenticate(self.reviewer)
        loaded = []
        original = field_api.load

        def load(query, **kwargs):
            loaded.extend(query.values_list('pk', flat=True))
            return original(query, **kwargs)

        with patch.object(field_api, 'load', load):
            result = self.read()
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data['scope_counts']['tasks'], 1)
        self.assertEqual(result.data['scope']['reference_kind'], 'projection_hash')
        self.assertNotIn(self.first.pk, loaded)
        self.assertNotIn(self.second.pk, loaded)
        self.assertEqual(result.data['summary']['normalized_agreement'], fields.fraction(1, 1))
        self.assertNotIn(hidden_id, [t for r in result.data['annotators'] for t in r['task_ids']])

    def test_current_revocation_denies_old_frozen_receipt_and_annotator_cannot_read(self):
        first = self.read().data
        self.client.force_authenticate(self.worker)
        self.assertIn(self.read(frozen=first['frozen']).status_code, (403, 404))
        self.client.force_authenticate(self.manager)
        OrganizationMember.objects.filter(user=self.manager, organization=self.org).update(deleted_at=timezone.now())
        self.assertIn(self.read(frozen=first['frozen']).status_code, (401, 403, 404))

    def test_all_hidden_reviewer_batch_is_not_a_zero_count_overview(self):
        self.client.force_authenticate(self.reviewer)
        self.assertEqual(self.read().status_code, 404)

    def test_receipts_bind_batch_viewer_and_strict_identity_types(self):
        first = self.read().data
        self.freeze()
        self.assertEqual(self.read(frozen=first['frozen']).status_code, 404)
        for value in (True, float(self.project.pk)):
            payload = signing.loads(self.read().data['frozen'], salt=overview.SCHEMA)
            payload['origin']['scope']['project_id'] = value
            with patch.object(field_api, 'load') as loader:
                self.assertEqual(self.read(frozen=signing.dumps(payload, salt=overview.SCHEMA)).status_code, 400)
            loader.assert_not_called()
        payload = signing.loads(self.read().data['frozen'], salt=overview.SCHEMA)
        payload['origin']['viewer'] = '0' * 64
        self.assertEqual(self.read(frozen=signing.dumps(payload, salt=overview.SCHEMA)).status_code, 409)

    def test_unknown_arguments_duplicate_query_invalid_paging_and_writes_reject(self):
        for query in (
            {'page': 2},
            {'actor': self.manager.pk},
            {'state': 'agreement'},
            {'weights': '1'},
            {'page_size': 101},
            {'page': 'True'},
            {'frozen': 'bad'},
        ):
            self.assertEqual(self.read(**query).status_code, 400)
        self.assertEqual(self.client.get(self.url + '?page=1&page=1').status_code, 400)
        for method in ('post', 'put', 'patch', 'delete'):
            self.assertEqual(getattr(self.client, method)(self.url, {}).status_code, 405)

    def test_independent_flags_core_modes_and_read_only_capability(self):
        self.assertTrue(self.client.get(self.base + 'capability/').data['can_view_quality_overview'])
        for flag in (
            'ANNOTATION_FIELD_QUALITY_OVERVIEW_ENABLED',
            'ANNOTATION_FIELD_BATCHES_ENABLED',
            'ANNOTATION_FIELD_DISAGREEMENTS_ENABLED',
        ):
            with self.settings(**{flag: False}):
                self.assertEqual(self.read().status_code, 404)
        for mode in ('core_only_internal', 'unknown'):
            with self.settings(ANNOTATION_FIELD_DISAGREEMENT_MODES={f'{self.org.pk}:{self.project.pk}': mode}):
                self.assertEqual(self.read().status_code, 404)

    def test_read_work_does_not_require_or_grant_planning_or_review(self):
        with self.settings(
            ANNOTATION_PROJECT_OPERATION_PROVIDER='classification_disagreements.tests.test_field_overview.ReadOnlyPolicy'
        ):
            self.assertEqual(self.read().status_code, 200)
            capability = self.client.get(self.base + 'capability/').data
            self.assertTrue(capability['can_view_quality_overview'])
            self.assertFalse(capability['can_create'])

    def test_whole_cpu_input_limits_reject_before_first_scoring_and_recovery_stays_metadata(self):
        with patch.object(batches, 'MAX_NORMALIZATIONS', 5), patch.object(fields, 'normalize') as n, patch.object(
            fields, 'equal'
        ) as e:
            self.assertEqual(self.read().status_code, 422)
            recovered = self.client.post(self.base, self.body, format='json', HTTP_IDEMPOTENCY_KEY=self.key)
            self.assertEqual(recovered.status_code, 200, recovered.data)
            self.assertTrue(recovered.data['replayed'])
        n.assert_not_called()
        e.assert_not_called()

    def test_exact_logical_output_boundary_is_after_calculation(self):
        data = self.projection()
        size = fields.bounded(data, 1024 * 1024)
        with patch.object(overview, 'MAX_BYTES', size):
            self.assertEqual(self.read().status_code, 200)
        with patch.object(overview, 'MAX_BYTES', size - 1), patch.object(
            fields, 'normalize', wraps=fields.normalize
        ) as n:
            self.assertEqual(self.read().status_code, 422)
        self.assertGreater(n.call_count, 0)

    def test_actual_response_and_token_boundaries_reject_after_performed_work(self):
        first = self.read()
        size = len(first.content)
        with patch.object(fields, 'MAX_OUTPUT', size - 10), patch.object(
            fields, 'normalize', wraps=fields.normalize
        ) as n:
            self.assertEqual(self.read().status_code, 422)
        self.assertGreater(n.call_count, 0)
        token_size = len(first.data['frozen'].encode())
        with patch.object(overview, 'MAX_TOKEN', token_size):
            self.assertEqual(self.read().status_code, 200)
        with patch.object(overview, 'MAX_TOKEN', token_size - 1), patch.object(
            fields, 'normalize', wraps=fields.normalize
        ) as n:
            self.assertEqual(self.read().status_code, 422)
        self.assertGreater(n.call_count, 0)

    def test_serialized_counts_cannot_be_laundered_by_an_unchanged_receipt(self):
        render = Response.render

        def tamper(response):
            if isinstance(response.data, dict) and response.data.get('schema') == overview.SCHEMA:
                response.data['summary']['raw_agreement'] = fields.fraction(1, 1)
            return render(response)

        with patch.object(Response, 'render', tamper):
            response = self.read()
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('summary', response.data)

    def test_get_has_no_business_writes_and_uses_two_hydrations(self):
        before = list(Submission.objects.order_by('pk').values())
        batches_before, decisions = FieldBatch.objects.count(), ReviewDecision.objects.count()
        with CaptureQueriesContext(connection) as queries, patch.object(
            field_api, 'load', wraps=field_api.load
        ) as loader:
            response = self.read()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(loader.call_count, 2)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
        self.assertEqual(list(Submission.objects.order_by('pk').values()), before)
        self.assertEqual((FieldBatch.objects.count(), ReviewDecision.objects.count()), (batches_before, decisions))
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')


class ReadOnlyPolicy:
    def evaluate(self, project):
        from access_control.project_operations import READ_WORK, ProjectOperationPolicy

        return ProjectOperationPolicy('field-overview-read-only/v1', 'active', frozenset({READ_WORK}))


class TestFieldOverviewWorkLimits(APITestCase):
    def test_maximum_pair_and_peer_work_is_exact_and_cpu_overflow_is_prescore(self):
        from classification_disagreements.tests.test_field_metrics import snapshot

        xml = '<View><Text name="doc" value="$text"/><TextArea name="name" toName="doc" maxSubmissions="1"/></View>'
        items, inspected = [], {}
        for task in range(1, 21):
            ids = [task * 100 + actor for actor in range(1, 21)]
            values = []
            for _ in ids:
                value = snapshot(('X',))
                value['project']['label_config'] = xml
                values.append(value)
            inspected[task] = fields.inspect(ids, values)
            logical = fields.calculate(inspected[task])['logical']
            items.append(
                {
                    'task_id': task,
                    'result': batches.summary(logical),
                    'cohort': [
                        {'assignee_id': actor, 'source': {'submission_id': key}} for actor, key in enumerate(ids, 1)
                    ],
                }
            )
        prepared = {
            'reference': {},
            'items': items,
            'inspected': inspected,
            'missing': set(),
            'calculated_at': 'frozen',
        }
        with patch.object(fields, 'normalize', wraps=fields.normalize) as n, patch.object(
            fields, 'equal', wraps=fields.equal
        ) as e:
            data = overview.recalculate(prepared)
        self.assertEqual((n.call_count, e.call_count), (400, 7600))
        self.assertEqual(
            data['processing_cost']['per_pass'],
            {
                'normalized_values': 400,
                'field_equalities': 7600,
                'aggregation_pair_visits': 3800,
                'annotator_pair_updates': 7600,
            },
        )
        self.assertEqual(data['summary']['raw_agreement'], fields.fraction(1, 1))
        for limit, maximum in (('MAX_NORMALIZATIONS', 399), ('MAX_EQUALITIES', 7599)):
            with patch.object(batches, limit, maximum), patch.object(fields, 'normalize') as n, patch.object(
                fields, 'equal'
            ) as e:
                with self.assertRaises(fields.Error) as rejected:
                    overview.recalculate(prepared)
                self.assertEqual(rejected.exception.status, 422)
            n.assert_not_called()
            e.assert_not_called()
