import copy
from fractions import Fraction
from unittest.mock import patch

from classification_disagreements import batches, overview, partial
from classification_disagreements.tests import test_partial as fixtures
from classification_disagreements.tests import test_batches as batch_fixtures
from django.core import signing
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from organizations.models import OrganizationMember
from projects.models import ProjectMember
from rest_framework.response import Response
from rest_framework.test import APITestCase
from tasks.models import Submission, TaskAssignment
from tasks.submissions import _snapshot_hash


@override_settings(ANNOTATION_CLASSIFICATION_QUALITY_OVERVIEW_ENABLED=True,
                   ANNOTATION_CLASSIFICATION_PARTIAL_MATCH_ENABLED=True,
                   ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=True,
                   ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=True)
class TestOverview(APITestCase):
    setUpTestData = classmethod(fixtures.TestPartial.setUpTestData.__func__)
    make_source = fixtures.TestPartial.make_source
    set_answer = fixtures.TestPartial.set_answer
    multiple = fixtures.TestPartial.multiple
    preview = fixtures.TestPartial.preview
    body = fixtures.TestPartial.body
    create = fixtures.TestPartial.create
    new_batch = fixtures.TestPartial.new_batch

    def setUp(self):
        batch_fixtures.TestClassificationBatches.setUp(self)
        self.multiple(self.first, ['A', 'B'])
        self.multiple(self.second, ['B', 'C'])
        self.batch = self.new_batch()
        self.url = f'{self.base}{self.batch.pk}/quality-overview/'

    def read_overview(self, **query):
        return self.client.get(self.url, query)

    def mixed(self, missing=False):
        # T1 has two equal inputs. T2 has three independent nonidentical inputs.
        for row in (self.first, self.second):
            self.multiple(row, ['A'])
        rows = [self.make_source(worker=actor) for actor in (self.worker, self.reviewer, self.manager)]
        for row in rows[1:]:
            row.assignment.task = rows[0].assignment.task
            row.assignment.save()
        for row, labels in zip(rows, (['A', 'B'], ['B', 'C'], ['B'])):
            self.multiple(row, labels)
            snapshot = copy.deepcopy(row.result_snapshot)
            snapshot['task']['id'] = rows[0].assignment.task_id
            snapshot['task']['data'] = {'text': 'Synthetic 合成'}
            Submission.objects.filter(pk=row.pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
        ids = [self.first.assignment.task_id, rows[0].assignment.task_id]
        if missing:
            row = self.make_source()
            task = row.assignment.task
            row.delete()
            TaskAssignment.objects.create(project=self.project, task=task, assignee=self.manager)
            ids.append(task.pk)
        self.args['scope']['task_ids'] = sorted(ids)
        self.batch = self.new_batch()
        self.url = f'{self.base}{self.batch.pk}/quality-overview/'
        return rows

    def test_mixed_task_weights_person_weights_and_missing_denominators(self):
        self.mixed(missing=True)
        result = self.read_overview()
        assert result.status_code == 200, result.data
        data = result.data
        assert data['scope_counts']['tasks'] == 3
        assert data['scope_counts']['comparable_tasks'] == 2 and data['scope_counts']['unavailable_tasks'] == 1
        assert data['summary']['coverage'] == partial.fraction(Fraction(2, 3))
        assert data['summary']['exact_agreement'] == partial.fraction(Fraction(1, 2))
        assert data['summary']['partial_match'] == partial.fraction(Fraction(13, 18))
        people = {p['actor_id']: p for p in data['annotators']}
        assert people[self.worker.pk]['tasks'] == 3 and people[self.worker.pk]['comparable_tasks'] == 2
        assert people[self.worker.pk]['partial_match'] == partial.fraction(Fraction(17, 24))
        assert people[self.manager.pk]['partial_match'] == partial.fraction(Fraction(1, 2))
        assert people[self.manager.pk]['unavailable_tasks'] == 1
        assert data['summary']['unanimous_tasks'] == 1 and data['summary']['disagreement_tasks'] == 1

    def test_exact_requires_all_controls_jaccard_averages_controls(self):
        for row, other in ((self.first, ['A']), (self.second, ['B'])):
            self.multiple(row, ['A'], second=other)
        self.batch = self.new_batch()
        self.url = f'{self.base}{self.batch.pk}/quality-overview/'
        summary = self.read_overview().data['summary']
        assert summary['exact_agreement'] == partial.fraction(Fraction())
        assert summary['partial_match'] == partial.fraction(Fraction(1, 2))

    def test_frozen_pages_replay_all_rows_hash_time_and_ignore_new_revision(self):
        first = self.read_overview(page_size=1).data
        newer = self.make_source(assignment=self.first.assignment, revision=2)
        self.multiple(newer, ['C'])
        replay = self.read_overview(frozen=first['frozen'], page=2, page_size=1)
        assert replay.status_code == 200, replay.data
        assert replay.data['annotators'][0]['actor_id'] != first['annotators'][0]['actor_id']
        for key in ('summary', 'scope_counts', 'result_hash', 'frozen', 'summarized_at'):
            assert replay.data[key] == first[key]
        assert self.read_overview(page=2).status_code == 400
        assert self.read_overview(frozen=first['frozen'], page=100).status_code == 404

    def test_zero_comparable_empty_values_and_missing_remain_null(self):
        for row in (self.first, self.second):
            self.multiple(row, [])
        self.batch = self.new_batch()
        self.url = f'{self.base}{self.batch.pk}/quality-overview/'
        data = self.read_overview().data
        assert data['source_status'] == 'verified' and data['receipt_verified']
        assert data['availability'] == 'unavailable' and data['reason'] == 'no_comparable_tasks'
        assert data['summary']['exact_agreement'] is None and data['summary']['partial_match'] is None
        assert all(row['comparable_tasks'] == 0 and row['exact_agreement'] is None for row in data['annotators'])

    def test_lost_or_corrupt_source_masks_whole_overview_and_keeps_denominator(self):
        initial = self.read_overview().data
        Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64)
        for query in ({}, {'frozen': initial['frozen']}):
            data = self.read_overview(**query).data
            assert data['scope_counts'] == initial['scope_counts']
            assert data['summary'] is None and data['annotators'] is None and data['pagination'] is None
            assert data['result_hash'] is None and data['receipt_verified'] is False
        Submission.objects.filter(pk=self.first.pk).update(result_hash=self.first.result_hash)
        assert self.read_overview(frozen=initial['frozen']).data['result_hash'] == initial['result_hash']
        self.first.delete()
        assert self.read_overview(frozen=initial['frozen']).data['summary'] is None

    def test_reviewer_filters_before_counts_people_reads_and_hash(self):
        self.mixed()
        # The reviewer belongs to both tasks, so the entire batch is hidden.
        manager_receipt = self.read_overview().data['frozen']
        self.client.force_authenticate(self.reviewer)
        assert self.read_overview().status_code == 404
        assert self.read_overview(frozen=manager_receipt).status_code == 404
        self.client.force_authenticate(self.manager)
        # A separate reviewer not in either cohort sees exactly its authorized projection.
        from users.tests.factories import UserFactory
        viewer = UserFactory(active_organization=self.org)
        ProjectMember.objects.create(project=self.project, user=viewer, role='reviewer')
        self.client.force_authenticate(viewer)
        data = self.read_overview().data
        assert data['scope']['visibility'] == 'visible_subset'
        assert data['scope']['reference_kind'] == 'projection_hash'
        assert self.batch.manifest_hash not in str(data)
        assert self.read_overview(frozen=manager_receipt).status_code == 409

    def test_roles_modes_operations_actor_fields_and_token_revocation(self):
        receipt = self.read_overview().data['frozen']
        self.client.force_authenticate(self.worker)
        assert self.read_overview(frozen=receipt).status_code in (403, 404)
        self.client.force_authenticate(self.manager)
        for name in ('actor', 'weight', 'state', 'submissions'):
            assert self.read_overview(**{name: '1'}).status_code == 400
        assert self.client.get(self.url + '?page=1&page=2').status_code == 400
        for setting in ('ANNOTATION_CLASSIFICATION_QUALITY_OVERVIEW_ENABLED',
                        'ANNOTATION_CLASSIFICATION_PARTIAL_MATCH_ENABLED',
                        'ANNOTATION_CLASSIFICATION_BATCHES_ENABLED', 'ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED'):
            with override_settings(**{setting: False}):
                assert self.read_overview(frozen=receipt).status_code == 404
        with override_settings(ANNOTATION_CLASSIFICATION_DISAGREEMENT_MODES={f'{self.org.pk}:{self.project.pk}': 'core_only_review'}):
            assert self.read_overview().status_code == 404
        OrganizationMember.objects.filter(organization=self.org, user=self.manager).update(deleted_at='2026-10-26T00:00:00Z')
        assert self.read_overview(frozen=receipt).status_code in (403, 404)

    def test_receipt_tampering_foreign_batch_and_secret_rotation(self):
        data = self.read_overview().data
        assert self.read_overview(frozen=data['frozen'] + 'x').status_code == 400
        payload = overview.decode(data['frozen'])
        payload['result_hash'] = '0' * 64
        assert self.read_overview(frozen=signing.dumps(payload, salt=overview.SCHEMA)).status_code == 409
        payload['origin']['scope']['batch_id'] += 100
        assert self.read_overview(frozen=signing.dumps(payload, salt=overview.SCHEMA)).status_code == 404
        with override_settings(SECRET_KEY='overview-test-rotation', SECRET_KEY_FALLBACKS=[]):
            assert self.read_overview(frozen=data['frozen']).status_code == 400

    def test_hidden_broken_task_never_enters_reviewer_loader_or_denominator(self):
        first = self.make_source()
        second = self.make_source(worker=self.manager)
        second.assignment.task = first.assignment.task
        second.assignment.save()
        for row in (first, second):
            self.multiple(row, ['A'])
            snapshot = copy.deepcopy(row.result_snapshot)
            snapshot['task'] = {'id': first.assignment.task_id, 'data': {'text': 'Visible 合成'}, 'meta': {}}
            Submission.objects.filter(pk=row.pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
        self.args['scope']['task_ids'] = sorted([self.first.assignment.task_id, first.assignment.task_id])
        batch = self.new_batch()
        self.url = f'{self.base}{batch.pk}/quality-overview/'
        self.first.delete()  # Hidden self-cohort task is now broken.
        self.client.force_authenticate(self.reviewer)
        with patch.object(batches, 'load_sources', wraps=batches.load_sources) as loader:
            response = self.read_overview()
        assert response.status_code == 200, response.data
        assert response.data['scope_counts']['tasks'] == 1 and response.data['source_status'] == 'verified'
        assert {p['actor_id'] for p in response.data['annotators']} == {self.worker.pk, self.manager.pk}
        assert len(loader.call_args_list) == 2
        assert all(set(args.args[0]) == {first.pk, second.pk} for args in loader.call_args_list)

    def test_read_work_is_independent_of_review_permission(self):
        with override_settings(ANNOTATION_PROJECT_OPERATION_PROVIDER='review_batches.tests.test_batches.ReviewWithoutReadPolicy'):
            assert self.read_overview().status_code == 403

    def test_input_limits_and_utf8_snapshot_bytes_include_the_boundary(self):
        self.mixed()
        for name, inclusive in (('MAX_TASKS', 2), ('MAX_PARTICIPANTS', 3), ('MAX_INPUTS', 5)):
            with patch.object(batches, name, inclusive):
                assert self.read_overview().status_code == 200
            with patch.object(batches, name, inclusive - 1):
                assert self.read_overview().status_code == 422
        pins = [entry['source']['submission_id'] for item in self.batch.manifest['tasks'] for entry in item['cohort']]
        size = sum(len(batches.canonical(row.result_snapshot)) for row in Submission.objects.filter(pk__in=pins))
        with patch.object(batches, 'MAX_BYTES', size):
            assert self.read_overview().status_code == 200
        with patch.object(batches, 'MAX_BYTES', size - 1):
            assert self.read_overview().status_code == 422

    def test_read_only_old_manifest_hash_keys_and_response_render_revalidation(self):
        before = copy.deepcopy(self.batch.manifest)
        source_before = list(Submission.objects.order_by('pk').values())
        with CaptureQueriesContext(connection) as queries:
            response = self.read_overview()
        assert response.status_code == 200, response.data
        assert not any(q['sql'].lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ')) for q in queries)
        self.batch.refresh_from_db()
        assert self.batch.manifest == before and list(Submission.objects.order_by('pk').values()) == source_before
        assert response['Cache-Control'] == 'private, no-store'
        original = Response.render
        def render(value):
            result = original(value)
            if isinstance(value.data, dict) and value.data.get('schema') == overview.SCHEMA:
                Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64)
            return result
        with patch.object(Response, 'render', render):
            assert self.read_overview().status_code == 409

    def test_output_and_token_exact_budget_edges(self):
        from classification_disagreements.overview_api import OverviewAPI
        from rest_framework.test import APIRequestFactory, force_authenticate
        request = APIRequestFactory().get(self.url)
        force_authenticate(request, self.manager)
        view = OverviewAPI()
        request = view.initialize_request(request)
        context = view.context(request, self.project.pk)
        data = overview.project(self.batch, context)
        exact = len(batches.canonical(data))
        with patch.object(overview, 'MAX_BYTES', exact):
            assert self.read_overview().status_code == 200
        with patch.object(overview, 'MAX_BYTES', exact - 1):
            assert self.read_overview().status_code == 422
        token = self.read_overview().data['frozen']
        with patch.object(overview, 'MAX_TOKEN', len(token.encode()) - 1):
            assert self.read_overview().status_code == 422
            assert self.read_overview(frozen=token).status_code == 400
