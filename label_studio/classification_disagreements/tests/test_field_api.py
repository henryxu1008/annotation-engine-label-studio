import copy
import json
from unittest.mock import patch

from classification_disagreements import field_api, fields
from classification_disagreements.tests.test_field_metrics import XML, snapshot
from django.conf import settings
from django.core import signing
from django.db import connection
from django.db.models import JSONField
from django.db.models.query import QuerySet
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import ProjectMember
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.test import APITestCase
from review_batches.tests import test_batches as fixtures
from tasks.models import ReviewDecision, Submission, TaskAssignment
from tasks.submissions import _snapshot_hash
from users.models import User


@override_settings(ANNOTATION_FIELD_DISAGREEMENTS_ENABLED=True)
class TestFieldAPI(APITestCase):
    setUpTestData = classmethod(fixtures.TestReviewBatches.setUpTestData.__func__)
    make_source = fixtures.TestReviewBatches.make_source

    def setUp(self):
        self.project.label_config = XML
        self.project.save()
        self.client.force_authenticate(self.manager)
        mode = self.settings(ANNOTATION_FIELD_DISAGREEMENT_MODES={f'{self.org.pk}:{self.project.pk}': 'fork_review'})
        mode.enable()
        self.addCleanup(mode.disable)
        self.first = self.make_source()
        self.second = self.make_source(worker=self.reviewer)
        self.second.assignment.task = self.first.assignment.task
        self.second.assignment.save()
        self.write(self.first)
        self.write(self.second, ('张三', '上海\n浦东', '1'))
        self.base = f'/api/projects/{self.project.pk}/field-disagreements/'
        self.url = f'{self.base}tasks/{self.first.assignment.task_id}/'

    def write(self, row, values=(' 张三 ', '上海\r\n浦东', '001'), mutate=None):
        value = snapshot(values)
        value['project']['id'] = self.project.pk
        value['task']['id'] = self.first.assignment.task_id
        if mutate:
            mutate(value)
        row.result_snapshot, row.result_hash = value, _snapshot_hash(value)
        Submission.objects.filter(pk=row.pk).update(result_snapshot=value, result_hash=row.result_hash)

    def third(self):
        row = self.make_source(worker=self.manager)
        row.assignment.task = self.first.assignment.task
        row.assignment.save()
        self.write(row)
        return row

    def read(self, rows=None, frozen=None, **query):
        return self.client.get(
            self.url,
            {'frozen': frozen, **query}
            if frozen
            else {'submissions': ','.join(str(r.pk) for r in (rows or [self.first, self.second])), **query},
        )

    def test_frozen_pair_replay_cost_and_no_business_writes(self):
        third = self.third()
        before = list(Submission.objects.order_by('pk').values())
        decisions = ReviewDecision.objects.count()
        with patch.object(fields, 'normalize', wraps=fields.normalize) as normalize:
            response = self.read([third, self.second, self.first])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(normalize.call_count, 18)  # 3 people * 3 fields * 2 source passes
        body = response.data
        self.assertEqual(body['cost_per_pass'], {'normalized_values': 9, 'field_equalities': 18})
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')
        replay = self.read(frozen=body['frozen'], left=self.first.pk, right=third.pk)
        self.assertEqual(replay.status_code, 200, replay.data)
        for key in ('frozen', 'manifest', 'manifest_hash', 'result_hash', 'calculated_at', 'pairs'):
            self.assertEqual(body[key], replay.data[key])
        self.assertEqual(replay.data['selected_pair']['right'], third.pk)
        self.assertEqual(list(Submission.objects.order_by('pk').values()), before)
        self.assertEqual(ReviewDecision.objects.count(), decisions)
        self.assertEqual(self.client.post(self.url, {}).status_code, 405)

    def test_original_receipt_recovers_exact_restored_source(self):
        original = self.read().data
        record = Submission.objects.filter(pk=self.first.pk).values().get()
        self.first.delete()
        lost = self.read(frozen=original['frozen'])
        self.assertEqual(lost.status_code, 200, lost.data)
        self.assertEqual(lost.data['configured_fields'], 3)
        self.assertIsNone(lost.data['selected_pair'])
        restored = Submission.objects.create(**record)
        replay = self.read(frozen=original['frozen'])
        self.assertEqual(replay.status_code, 200, replay.data)
        for key in ('result_hash', 'selected_pair', 'pairs', 'manifest_hash', 'calculated_at', 'frozen'):
            self.assertEqual(original[key], replay.data[key])
        self.assertEqual(restored.pk, original['scope']['input_ids'][0])

    def test_twenty_independent_inputs_and_actual_receipt_budget(self):
        rows = [self.first, self.second]
        for index in range(18):
            actor = User.objects.create_user(
                f'field-limit-{index}@example.test',
                None,
                username=f'field-limit-{index}',
                active_organization=self.org,
            )
            self.org.add_user(actor)
            ProjectMember.objects.create(project=self.project, user=actor, role='annotator')
            assignment = TaskAssignment.objects.create(
                project=self.project, task=self.first.assignment.task, assignee=actor, assigned_by=self.manager
            )
            row = self.make_source(worker=actor, assignment=assignment)
            self.write(row)
            rows.append(row)
        response = self.read(rows)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data['pairs']), 190)
        self.assertGreater(len(response.data['frozen'].encode()), 8192)
        self.assertLessEqual(len(response.data['frozen'].encode()), fields.MAX_TOKEN)
        replay = self.read(frozen=response.data['frozen'])
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data['result_hash'], response.data['result_hash'])

    def test_source_collection_reads_metadata_without_snapshots_or_live_task_data(self):
        with patch.object(field_api, 'load', side_effect=AssertionError('Do not hydrate snapshots')):
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(self.url + 'sources/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data['sources']), 2)
        self.assertNotIn('annotation', json.dumps(response.data))
        self.assertFalse(any('result_snapshot' in q['sql'] for q in queries))
        with patch.object(fields, 'MAX_SOURCES', 1):
            self.assertEqual(self.client.get(self.url + 'sources/').status_code, 422)

    def test_later_revision_does_not_replace_intact_frozen_source(self):
        original = self.read().data
        newer = self.make_source(assignment=self.first.assignment, revision=2)
        self.write(newer, ('new', 'b', 'c'))
        response = self.read(frozen=original['frozen'])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['result_hash'], original['result_hash'])
        self.assertEqual(response.data['selected_pair'], original['selected_pair'])
        self.assertEqual(self.read([self.first, newer]).status_code, 400)

    def test_missing_original_masks_every_pair_without_selecting_newer_revision(self):
        third = self.third()
        original = self.read([self.first, self.second, third]).data
        newer = self.make_source(assignment=self.first.assignment, revision=2)
        self.write(newer, ('new', 'b', 'c'))
        self.first.delete()
        with patch.object(fields, 'public') as engine:
            response = self.read(frozen=original['frozen'])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['input_count'], response.data['total_pairs']), (3, 3))
        self.assertFalse(response.data['receipt_verified'])
        self.assertIsNone(response.data['pairs'])
        self.assertIsNone(response.data['selected_pair'])
        self.assertIsNone(response.data['result_hash'])
        self.assertEqual(response.data['frozen'], original['frozen'])
        engine.assert_not_called()

    def test_corrupt_hash_or_rebound_task_never_returns_frozen_answers(self):
        original = self.read().data
        value = copy.deepcopy(self.first.result_snapshot)
        value['annotation']['result'][0]['value']['text'] = ['tampered']
        Submission.objects.filter(pk=self.first.pk).update(result_snapshot=value)
        response = self.read(frozen=original['frozen'])
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('张三', response.content.decode())

    def test_reviewer_cannot_include_self_and_annotator_cannot_bypass_direct_apis(self):
        original = self.read().data
        self.client.force_authenticate(self.reviewer)
        response = self.client.get(self.url + 'sources/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([s['id'] for s in response.data['sources']], [self.first.pk])
        self.assertEqual(self.read().status_code, 404)
        self.assertEqual(self.read(frozen=original['frozen']).status_code, 409)
        self.client.force_authenticate(self.worker)
        for url, query in [
            (self.base + 'capability/', {}),
            (self.url + 'sources/', {}),
            (self.url, {'submissions': f'{self.first.pk},{self.second.pk}'}),
            (self.url, {'frozen': original['frozen']}),
        ]:
            self.assertIn(self.client.get(url, query).status_code, (403, 404))

    def test_revocation_and_disabled_mode_block_every_read_and_receipt(self):
        third = self.third()
        self.client.force_authenticate(self.reviewer)
        original = self.read([self.first, third]).data
        ProjectMember.objects.filter(project=self.project, user=self.reviewer).delete()
        for suffix in ('', 'sources/'):
            self.assertIn(
                self.client.get(self.url + suffix, {'frozen': original['frozen']} if not suffix else {}).status_code,
                (403, 404),
            )
        self.client.force_authenticate(self.manager)
        for mode in ('core_only_internal', 'unknown', None):
            with self.settings(ANNOTATION_FIELD_DISAGREEMENT_MODES={f'{self.org.pk}:{self.project.pk}': mode}):
                for url, params in [
                    (self.base + 'capability/', {}),
                    (self.url + 'sources/', {}),
                    (self.url, {'frozen': original['frozen']}),
                ]:
                    self.assertEqual(self.client.get(url, params).status_code, 404)
        with self.settings(ANNOTATION_FIELD_DISAGREEMENTS_ENABLED=False):
            self.assertEqual(self.read().status_code, 404)

    def test_cross_scope_and_injected_fields_and_bad_signatures_are_rejected(self):
        original = self.read().data
        for query in (
            {'submissions': f'{self.first.pk},{self.second.pk}', 'actor': self.manager.pk},
            {'frozen': original['frozen'], 'threshold': '0.5'},
            {'frozen': original['frozen'], 'left': self.first.pk},
            {'submissions': f'{self.first.pk},{self.first.pk}'},
            {'submissions': str(2**53)},
            {'frozen': signing.dumps(original['manifest'], salt='classification-disagreements/v1')},
            {'frozen': original['frozen'] + 'x'},
        ):
            self.assertEqual(self.client.get(self.url, query).status_code, 400)
        other = self.make_source()
        self.assertEqual(self.read([self.first, other]).status_code, 404)
        self.assertEqual(self.read(frozen=original['frozen'], left=self.first.pk, right=other.pk).status_code, 400)
        self.assertEqual(self.client.get(self.url + '?submissions=1&submissions=2').status_code, 400)

    def test_key_fallback_replays_the_original_viewer_and_retirement_denies(self):
        original = self.read().data
        with self.settings(SECRET_KEY='fields-new-synthetic-key', SECRET_KEY_FALLBACKS=[settings.SECRET_KEY]):
            response = self.read(frozen=original['frozen'])
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data['frozen'], original['frozen'])
            self.assertEqual(response.data['viewer'], original['viewer'])
        with self.settings(SECRET_KEY='fields-new-synthetic-key', SECRET_KEY_FALLBACKS=[]):
            self.assertEqual(self.read(frozen=original['frozen']).status_code, 400)

    def test_storage_preflight_rejects_before_hydration_and_score_calls(self):
        with (
            patch.object(fields, 'MAX_STORED_BYTES', 1),
            patch.object(fields, 'prepare') as normalize,
            patch.object(field_api, 'valid_source') as validate,
            patch.object(fields, 'public') as engine,
        ):
            response = self.read()
        self.assertEqual(response.status_code, 422)
        normalize.assert_not_called()
        validate.assert_not_called()
        engine.assert_not_called()

    def test_deleted_between_authorization_and_load_rejects_without_a_partial_receipt(self):
        original = field_api.load

        def delete_then_load(query):
            Submission.objects.filter(pk=self.first.pk).delete()
            return original(query)

        with patch.object(field_api, 'load', delete_then_load), patch.object(fields, 'public') as engine:
            response = self.read()
        self.assertEqual(response.status_code, 409, response.data)
        self.assertNotIn('张三', response.content.decode())
        engine.assert_not_called()

    def test_storage_growth_after_preflight_is_blocked_in_the_hydrating_statement(self):
        self.assert_storage_growth_guard()

    def test_aggregate_growth_after_preflight_is_blocked_even_when_each_row_fits(self):
        self.assert_storage_growth_guard(spread=True)

    def assert_storage_growth_guard(self, spread=False):
        maximum, changed = 8192, False
        original_iterator, decode_json = QuerySet.iterator, JSONField.from_db_value

        def grow_then_iterate(query, *args, **kwargs):
            nonlocal changed
            if query.model is Submission and not changed:
                changed = True
                for row in (self.first, self.second) if spread else (self.first,):
                    value = copy.deepcopy(row.result_snapshot)
                    value['padding'] = 'x' * (maximum // 2 if spread else maximum)
                    if spread:
                        self.assertLess(
                            len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()), maximum
                        )
                    Submission.objects.filter(pk=row.pk).update(
                        result_snapshot=value, result_hash=_snapshot_hash(value)
                    )
            return original_iterator(query, *args, **kwargs)

        def bounded_decode(field, value, expression, conn):
            if isinstance(value, str) and len(value.encode()) > maximum:
                raise AssertionError('Oversized JSON reached the Python decoder')
            return decode_json(field, value, expression, conn)

        with (
            patch.object(fields, 'MAX_STORED_BYTES', maximum),
            patch.object(QuerySet, 'iterator', grow_then_iterate),
            patch.object(JSONField, 'from_db_value', bounded_decode),
            patch.object(fields, 'public') as engine,
        ):
            response = self.read()
        self.assertTrue(changed)
        self.assertEqual(response.status_code, 422, response.data)
        engine.assert_not_called()

    def test_output_and_receipt_reject_before_the_scorer(self):
        for name in ('MAX_OUTPUT', 'MAX_TOKEN'):
            with self.subTest(name=name), patch.object(fields, name, 32):
                self.assertEqual(self.read().status_code, 422)

    def test_actual_renderer_revocation_or_changed_representation_is_rejected(self):
        third = self.third()
        self.client.force_authenticate(self.reviewer)
        original = Response.render

        def revoke(response):
            result = original(response)
            ProjectMember.objects.filter(project=self.project, user=self.reviewer).delete()
            return result

        with patch.object(Response, 'render', revoke):
            response = self.read([self.first, third])
        self.assertIn(response.status_code, (403, 404))
        self.assertNotIn('张三', response.content.decode())

    def test_actual_renderer_cannot_replace_the_json_bytes(self):
        original = Response.render

        def tamper(response):
            result = original(response)
            if response.status_code == 200:
                response.content = b'{"other":"injected"}'
            return result

        with patch.object(Response, 'render', tamper):
            response = self.read()
        self.assertEqual(response.status_code, 409)

    def test_organization_membership_revocation_denies_old_receipt(self):
        original = self.read().data
        OrganizationMember.objects.filter(user=self.manager, organization=self.org).update(deleted_at=timezone.now())
        self.assertIn(self.read(frozen=original['frozen']).status_code, (401, 403, 404))

    def test_independent_feature_and_read_work_policy_gate(self):
        with self.settings(
            ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=False, ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=False
        ):
            original = self.read()
            self.assertEqual(original.status_code, 200, original.data)
        with patch.object(field_api, 'require_project_operation', side_effect=PermissionDenied):
            for url, query in [
                (self.base + 'capability/', {}),
                (self.url + 'sources/', {}),
                (self.url, {'frozen': original.data['frozen']}),
            ]:
                self.assertEqual(self.client.get(url, query).status_code, 403)

    def test_actual_source_list_100_101_and_input_20_21_boundaries(self):
        for revision in range(2, 100):
            row = self.make_source(assignment=self.first.assignment, revision=revision)
            self.write(row)
        response = self.client.get(self.url + 'sources/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data['sources']), 100)
        row = self.make_source(assignment=self.first.assignment, revision=100)
        self.write(row)
        self.assertEqual(self.client.get(self.url + 'sources/').status_code, 422)
        ids = [r['id'] for r in response.data['sources'][:21]]
        with patch.object(fields, 'public') as engine:
            self.assertEqual(self.client.get(self.url, {'submissions': ','.join(map(str, ids))}).status_code, 422)
            engine.assert_not_called()
