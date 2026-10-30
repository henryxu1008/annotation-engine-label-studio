import copy
import json
from importlib import import_module
from unittest.mock import patch
from uuid import uuid4

from classification_disagreements import field_api, fields
from classification_disagreements import field_batches as b
from classification_disagreements.models import FieldBatch
from classification_disagreements.tests import test_field_api as fixtures
from classification_disagreements.tests.test_field_metrics import snapshot
from django.core import signing
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


@override_settings(ANNOTATION_FIELD_DISAGREEMENTS_ENABLED=True, ANNOTATION_FIELD_BATCHES_ENABLED=True)
class TestFieldBatches(APITestCase):
    setUpTestData = classmethod(fixtures.TestFieldAPI.setUpTestData.__func__)
    make_source = fixtures.TestFieldAPI.make_source

    def write(self, row, values=(' 张三 ', '上海\r\n浦东', '001'), mutate=None):
        value = snapshot(values)
        value['project']['id'] = self.project.pk
        value['task']['id'] = row.assignment.task_id
        if mutate:
            mutate(value)
        row.result_snapshot, row.result_hash = value, _snapshot_hash(value)
        Submission.objects.filter(pk=row.pk).update(result_snapshot=value, result_hash=row.result_hash)

    def setUp(self):
        fixtures.TestFieldAPI.setUp(self)
        self.base += 'batches/'
        self.task_id = self.first.assignment.task_id

    def preview(self, ids=None, **extra):
        return self.client.post(
            self.base + 'preview/',
            {'name': 'Fields frozen 冻结', 'scope': {'kind': 'tasks', 'task_ids': ids or [self.task_id]}, **extra},
            format='json',
        )

    def create(self, preview=None, key=None, expected=201):
        preview = preview or self.preview().data
        body = {'preview_token': preview['preview_token'], 'expected_plan_hash': preview['plan_hash']}
        key = key or str(uuid4())
        result = self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(result.status_code, expected, result.data)
        return result, body, key

    def item(self, batch, task=None, **query):
        return self.client.get(f'{self.base}{batch}/items/{task or self.task_id}/', query)

    def extra_task(self, owners=None, equal=False):
        rows = []
        for owner in owners or (self.worker, self.manager):
            row = self.make_source(worker=owner)
            if rows:
                row.assignment.task = rows[0].assignment.task
                row.assignment.save()
            self.write(row, ('A', 'B', 'C') if equal or not rows else ('D', 'E', 'F'))
            rows.append(row)
        return rows

    def test_frozen_metric_formula_evidence_and_no_business_writes(self):
        before = list(Submission.objects.order_by('pk').values())
        reviews = ReviewDecision.objects.count()
        with patch.object(fields, 'normalize', wraps=fields.normalize) as norm:
            p = self.preview()
        self.assertEqual(p.status_code, 200, p.data)
        self.assertEqual(norm.call_count, 12)
        created, _, _ = self.create(p.data)
        item = self.item(created.data['id'])
        self.assertEqual(item.status_code, 200, item.data)
        row = item.data['item']
        c = row['comparison']
        self.assertEqual(c['pairs'][0]['raw_rate'], fields.fraction(0, 3))
        self.assertEqual(c['pairs'][0]['normalized_rate'], fields.fraction(2, 3))
        self.assertTrue(row['has_value_difference'])
        self.assertTrue(row['has_format_difference'])
        self.assertEqual(fields.digest(row['evidence']), row['evidence_hash'])
        self.assertNotIn('上海', json.dumps(FieldBatch.objects.get(pk=created.data['id']).manifest, ensure_ascii=False))
        self.assertEqual(item.data['processing_cost']['total'], {'normalized_values': 12, 'field_equalities': 12})
        self.assertEqual(list(Submission.objects.order_by('pk').values()), before)
        self.assertEqual(ReviewDecision.objects.count(), reviews)
        self.assertEqual(item['Cache-Control'], 'private, no-store')

    def test_format_only_is_disagreement_and_empty_work_is_counted(self):
        self.write(self.second, ('张三', '上海\n浦东', '001'))
        created, _, _ = self.create()
        item = self.item(created.data['id']).data['item']
        self.assertEqual(item['outcome'], 'disagreement')
        self.assertFalse(item['has_value_difference'])
        self.assertTrue(item['has_format_difference'])
        self.write(self.first, ('', '', ''))
        self.write(self.second, ('', ' \t', ''))
        created, _, _ = self.create()
        data = self.item(created.data['id']).data
        self.assertEqual(data['item']['reason'], 'no_values')
        self.assertIsNone(data['item']['comparison']['pairs'][0]['raw_rate'])
        self.assertEqual(data['processing_cost']['total']['normalized_values'], 12)

    def test_source_restoration_restores_same_frozen_evidence(self):
        created, _, _ = self.create()
        old = self.item(created.data['id']).data['item']
        record = Submission.objects.filter(pk=self.first.pk).values().get()
        self.first.delete()
        lost = self.item(created.data['id']).data['item']
        self.assertIsNone(lost['comparable_pairs'])
        self.assertIsNone(lost['evidence'])
        self.assertEqual(lost['frozen_comparable_pairs'], 1)
        Submission.objects.create(**record)
        self.assertEqual(self.item(created.data['id']).data['item'], old)

    def test_whole_batch_preflight_rejects_before_first_normalization_or_equality(self):
        with patch.object(b, 'MAX_NORMALIZATIONS', 5), patch.object(fields, 'normalize') as n, patch.object(
            fields, 'equal'
        ) as e:
            self.assertEqual(self.preview().status_code, 422)
        n.assert_not_called()
        e.assert_not_called()

    def test_list_metadata_never_loads_sources_and_page_limit_is_eight(self):
        created, _, _ = self.create()
        with patch.object(field_api, 'load', side_effect=AssertionError('no snapshots')):
            response = self.client.get(self.base)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['processing_cost']['total'], b.ZERO)
        self.assertEqual(self.client.get(self.base, {'page_size': 9}).status_code, 400)

    def test_latest_is_per_assignment_and_never_falls_back_from_unusable_revision(self):
        newer = self.make_source(assignment=self.first.assignment, revision=4)
        self.write(newer, ('N', 'E', 'W'))
        result, _, _ = self.create()
        source_ids = [
            e['source']['submission_id']
            for e in FieldBatch.objects.get(pk=result.data['id']).manifest['tasks'][0]['cohort']
        ]
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
        self.write(new, ('N', 'E', 'W'))
        Submission.objects.filter(pk=self.first.pk).update(status=Submission.Status.SUPERSEDED)
        current = self.item(result.data['id'])
        self.assertEqual(current.status_code, 200, current.data)
        for key in ('calculated_at', 'manifest_hash'):
            self.assertEqual(current.data[key], old[key])
        self.assertEqual(current.data['item'], old['item'])

    def test_missing_participant_stays_missing_after_later_submission(self):
        missing = TaskAssignment.objects.create(
            project=self.project, task=self.first.assignment.task, assignee=self.manager, assigned_by=self.manager
        )
        result, _, _ = self.create()
        row = self.make_source(worker=self.manager, assignment=missing)
        self.write(row, ('A', 'B', 'C'))
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
                self.assertNotIn('上海', json.dumps(response.data))
                if not delete:
                    Submission.objects.filter(pk=self.first.pk).update(result_hash=self.first.result_hash)

    def test_project_scope_is_bounded_and_does_not_follow_later_tasks(self):
        response = self.preview(scope={'kind': 'project'})
        ids = response.data['task_ids']
        self.extra_task()
        created, _, _ = self.create(response.data)
        self.assertEqual(FieldBatch.objects.get(pk=created.data['id']).manifest['task_ids'], ids)

    def test_changed_preview_creates_nothing_and_requires_explicit_repreview(self):
        preview = self.preview().data
        newer = self.make_source(assignment=self.first.assignment, revision=2)
        self.write(newer, ('N', 'E', 'W'))
        self.create(preview, expected=409)
        self.assertEqual(FieldBatch.objects.count(), 0)

    def test_original_key_recovers_metadata_before_expiry_rotation_sources_and_budgets(self):
        created, body, key = self.create()
        self.first.delete()
        with self.settings(SECRET_KEY='new-isolated-key'), patch.object(b, 'PREVIEW_AGE', -1), patch.object(
            fields, 'MAX_SNAPSHOT_BYTES', 1
        ), patch.object(b, 'MAX_NORMALIZATIONS', 0), patch.object(b, 'MAX_EQUALITIES', 0), patch.object(
            field_api, 'load', side_effect=AssertionError('Recovery must not load sources')
        ):
            replay = self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertTrue(replay.data['replayed'])
        self.assertEqual(replay.data['id'], created.data['id'])
        self.assertEqual(replay.data['manifest_hash'], created.data['manifest_hash'])
        self.assertEqual(replay.data['idempotency_key'], key)
        self.assertEqual(FieldBatch.objects.count(), 1)
        changed = {**body, 'expected_plan_hash': '0' * 64}
        self.assertEqual(
            self.client.post(self.base, changed, format='json', HTTP_IDEMPOTENCY_KEY=key).status_code, 409
        )
        self.assertEqual(
            self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())).status_code, 409
        )

    def test_reviewer_excludes_whole_self_cohort_before_counts_pagination_and_loading(self):
        other = self.extra_task()
        created, _, _ = self.create(self.preview([self.task_id, other[0].assignment.task_id]).data)
        batch = created.data['id']
        Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64)
        self.client.force_authenticate(self.reviewer)
        with patch.object(field_api, 'load', wraps=field_api.load) as load:
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
        self.assertEqual(
            self.client.post(
                self.base + 'preview/', {'name': 'x', 'scope': {'kind': 'project'}}, format='json'
            ).status_code,
            404,
        )

    def test_annotator_foreign_ids_revocation_and_core_modes_fail_closed(self):
        created, body, key = self.create()
        batch = created.data['id']
        self.client.force_authenticate(self.worker)
        for path in ('', 'capability/', f'{batch}/', f'{batch}/items/', f'{batch}/items/{self.task_id}/'):
            self.assertIn(self.client.get(self.base + path).status_code, (403, 404))
        self.client.force_authenticate(self.manager)
        for mode in (
            {},
            {f'{self.org.pk}:{self.project.pk}': 'core_only_internal'},
            {f'{self.org.pk}:{self.project.pk}': 'unknown'},
        ):
            with self.settings(ANNOTATION_FIELD_DISAGREEMENT_MODES=mode):
                self.assertEqual(self.item(batch).status_code, 404)
                self.assertEqual(
                    self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=key).status_code, 404
                )
        with self.settings(ANNOTATION_FIELD_BATCHES_ENABLED=False):
            self.assertEqual(self.item(batch).status_code, 404)
        OrganizationMember.objects.filter(user=self.manager, organization=self.org).update(deleted_at=timezone.now())
        self.assertIn(self.item(batch).status_code, (401, 403, 404))
        self.assertIn(
            self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=key).status_code, (401, 403, 404)
        )

    def test_invalid_bodies_duplicate_queries_and_foreign_tasks(self):
        for extra in (
            {'actor': self.manager.pk},
            {'scope': {'kind': 'tasks', 'task_ids': [True]}},
            {'scope': {'kind': 'tasks', 'task_ids': [self.task_id, self.task_id]}},
        ):
            self.assertEqual(self.preview(**extra).status_code, 400)
        self.assertEqual(self.preview([99999999]).status_code, 409)
        created, _, _ = self.create()
        batch = created.data['id']
        self.assertEqual(self.client.get(f'{self.base}{batch}/items/?state=all&state=all').status_code, 400)
        self.assertEqual(self.item(batch, left=self.first.pk).status_code, 400)
        self.assertEqual(self.item(batch, left=self.first.pk, right=self.first.pk).status_code, 400)
        self.assertEqual(self.item(batch, left=self.first.pk, right=99999999).status_code, 400)
        for method in ('put', 'patch', 'delete'):
            self.assertEqual(getattr(self.client, method)(f'{self.base}{batch}/').status_code, 405)

    def test_freeze_database_guards_include_pk_and_creator_cleanup(self):
        created, _, _ = self.create()
        row = FieldBatch.objects.get(pk=created.data['id'])
        changes = {
            'id': row.pk + 10000,
            'project_id': self.project.pk + 10000,
            'creator_principal': 'foreign',
            'actor_id_frozen': self.worker.pk,
            'created_at': timezone.now(),
            'name': 'changed',
            'idempotency_key': uuid4(),
            'request_hash': '0' * 64,
            'manifest': {},
            'manifest_hash': '0' * 64,
            'created_by_id': self.worker.pk,
        }
        for field, value in changes.items():
            with self.subTest(field=field), self.assertRaises(DatabaseError), transaction.atomic():
                FieldBatch.objects.filter(pk=row.pk).update(**{field: value})
        FieldBatch.objects.filter(pk=row.pk).update(created_by=None)
        self.assertEqual(self.item(row.pk).status_code, 200)

    def test_manifest_corruption_rejects_even_if_admin_rehashes_wrong_rules(self):
        created, _, _ = self.create()
        row = FieldBatch.objects.get(pk=created.data['id'])
        migration = import_module('classification_disagreements.migrations.0009_field_batches')
        editor = connection.schema_editor(atomic=False)
        migration.uninstall(None, editor)
        try:
            manifest = copy.deepcopy(row.manifest)
            manifest['rule'] = 'wrong/v1'
            FieldBatch.objects.filter(pk=row.pk).update(manifest=manifest, manifest_hash=fields.digest(manifest))
            self.assertEqual(self.item(row.pk).status_code, 409)
        finally:
            migration.install(None, editor)

    def test_response_revocation_cannot_commit_hidden_creation(self):
        preview = self.preview().data
        original = Response.render

        def revoke(response):
            result = original(response)
            if response.status_code == 201:
                OrganizationMember.objects.filter(user=self.manager, organization=self.org).update(
                    deleted_at=timezone.now()
                )
            return result

        with patch.object(Response, 'render', revoke):
            result, _, _ = self.create(preview, expected=404)
        self.assertEqual(FieldBatch.objects.count(), 0)

    def test_canonical_and_stored_byte_limits_are_cumulative_inclusive(self):
        used = sum(fields.bounded(r.result_snapshot) for r in (self.first, self.second))
        with patch.object(fields, 'MAX_SNAPSHOT_BYTES', used):
            self.assertEqual(self.preview().status_code, 200)
        with patch.object(fields, 'MAX_SNAPSHOT_BYTES', used - 1), patch.object(fields, 'normalize') as engine:
            self.assertEqual(self.preview().status_code, 422)
        engine.assert_not_called()
        column = (
            connection.ops.quote_name(Submission._meta.db_table) + '.' + connection.ops.quote_name('result_snapshot')
        )
        expression = (
            f'OCTET_LENGTH(CAST({column} AS TEXT))'
            if connection.vendor == 'postgresql'
            else f'LENGTH(CAST({column} AS BLOB))'
        )
        from django.db.models.expressions import RawSQL

        used = sum(
            Submission.objects.filter(pk__in=[self.first.pk, self.second.pk])
            .annotate(size=RawSQL(expression, []))
            .values_list('size', flat=True)
        )
        with patch.object(fields, 'MAX_STORED_BYTES', used):
            self.assertEqual(self.preview().status_code, 200)
        with patch.object(fields, 'MAX_STORED_BYTES', used - 1), patch.object(
            JSONField, 'from_db_value', side_effect=AssertionError('Decode after SQL budget')
        ):
            with self.assertRaises(fields.Error) as error:
                field_api.load(Submission.objects.filter(pk__in=[self.first.pk, self.second.pk]))
            self.assertEqual(error.exception.status, 422)

    def test_same_statement_guard_blocks_growth_after_preflight(self):
        original = QuerySet.iterator
        mutated = False

        def iterate(query, *args, **kwargs):
            nonlocal mutated
            if query.model is Submission and not mutated:
                mutated = True
                value = copy.deepcopy(self.first.result_snapshot)
                value['padding'] = 'x' * 8192
                Submission.objects.filter(pk=self.first.pk).update(
                    result_snapshot=value, result_hash=_snapshot_hash(value)
                )
            return original(query, *args, **kwargs)

        with patch.object(QuerySet, 'iterator', iterate), patch.object(fields, 'MAX_STORED_BYTES', 8192), patch.object(
            fields, 'normalize'
        ) as engine:
            self.assertEqual(self.preview().status_code, 422)
        engine.assert_not_called()

    def test_true_task_limit_allows_100_and_rejects_101_without_truncation(self):
        ids = [Task.objects.create(project=self.project, data={}).pk for _ in range(101)]
        self.assertEqual(self.preview(ids[:100]).status_code, 200)
        with patch.object(fields, 'normalize') as engine:
            self.assertEqual(self.preview(ids).status_code, 422)
        engine.assert_not_called()

    def test_true_two_thousand_cohort_entries_and_twenty_per_task(self):
        tasks = Task.objects.bulk_create([Task(project=self.project, data={}) for _ in range(100)])
        actors = User.objects.bulk_create(
            [User(email=f'fields-cohort-{i}@example.test', username=f'cohort-{i}') for i in range(2000)]
        )
        TaskAssignment.objects.bulk_create(
            [
                TaskAssignment(project=self.project, task=tasks[i // 20], assignee=actors[i], assigned_by=self.manager)
                for i in range(2000)
            ]
        )
        ids = [t.pk for t in tasks]
        plan, _ = b.build_plan(self.project, ids)
        self.assertEqual(sum(len(i['cohort']) for i in plan['tasks']), 2000)
        self.assertEqual(self.preview(ids).status_code, 200)
        with patch.object(b, 'MAX_INPUTS', 1999), patch.object(
            field_api, 'load', side_effect=AssertionError('Metadata first')
        ):
            self.assertEqual(self.preview(ids).status_code, 422)
        TaskAssignment.objects.create(
            project=self.project, task=tasks[0], assignee=self.manager, assigned_by=self.manager
        )
        self.assertEqual(self.preview([tasks[0].pk]).status_code, 422)

    def test_organizing_field_is_independent_of_review_classification_and_arbitration(self):
        from access_control.project_operations import READ_WORK, LegacyProjectOperationProvider, ProjectOperationPolicy

        for operations in ((READ_WORK,), ('review', 'organize_classification', 'organize_arbitration', READ_WORK)):
            with patch.object(
                LegacyProjectOperationProvider,
                'evaluate',
                return_value=ProjectOperationPolicy('isolated-test-v1', 'active', frozenset(operations)),
            ):
                self.assertEqual(self.client.get(self.base + 'capability/').data['can_create'], False)
                self.assertEqual(self.preview().status_code, 403)

    def test_stale_authenticated_account_cannot_read_or_recover_after_deactivation(self):
        created, body, key = self.create()
        administrator = User.objects.create_user(
            'fields-security-admin@example.test',
            None,
            username='fields-security-admin',
            is_staff=True,
            is_superuser=True,
        )
        User.objects.filter(pk=self.manager.pk).update(is_active=False, session_actor=administrator)
        self.assertIn(self.item(created.data['id']).status_code, (401, 403, 404))
        self.assertIn(
            self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=key).status_code, (401, 403, 404)
        )
        self.assertEqual(FieldBatch.objects.count(), 1)

    def test_original_key_does_not_cross_identity_source_even_with_same_actor_and_principal_id(self):
        from dataclasses import replace

        created, body, key = self.create()
        resolve = field_api.resolve_actor

        def changed(request):
            principal, actor = resolve(request)
            return replace(principal, source='isolated-other-source'), actor

        with patch.object(field_api, 'resolve_actor', changed):
            response = self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=key)
        self.assertIn(response.status_code, (403, 404, 409), response.data)
        self.assertEqual(FieldBatch.objects.count(), 1)

    def test_preview_tokens_reject_unpaired_surrogates_before_utf8_encoding(self):
        self.client.raise_request_exception = False
        for token in ('bad\ud800token', 'bad\x00token'):
            with self.subTest(token=ascii(token)), patch.object(fields, 'normalize') as engine:
                response = self.client.post(
                    self.base,
                    json.dumps(
                        {
                            'preview_token': token,
                            'expected_plan_hash': '0' * 64,
                        },
                        ensure_ascii=True,
                    ),
                    content_type='application/json',
                    HTTP_IDEMPOTENCY_KEY=str(uuid4()),
                )
                self.assertEqual(response.status_code, 400)
                engine.assert_not_called()
        self.assertEqual(FieldBatch.objects.count(), 0)

    def test_legacy_signed_preview_invalid_text_does_not_consume_original_key(self):
        self.client.raise_request_exception = False
        preview = self.preview().data
        for name in ('bad\ud800name', 'bad\x00name'):
            with self.subTest(name=ascii(name)):
                payload = signing.loads(preview['preview_token'], salt=b.PREVIEW_SALT)
                payload['name'] = name
                key, before = str(uuid4()), FieldBatch.objects.count()
                body = {
                    'preview_token': signing.dumps(payload, salt=b.PREVIEW_SALT, compress=False),
                    'expected_plan_hash': preview['plan_hash'],
                }
                with patch.object(fields, 'normalize', wraps=fields.normalize) as engine:
                    response = self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=key)
                self.assertEqual(response.status_code, 400)
                engine.assert_not_called()
                self.assertEqual(FieldBatch.objects.count(), before)
                self.create(preview, key=key)
                self.assertEqual(FieldBatch.objects.count(), before + 1)

    def custom_task(self, participants, size, owners=None):
        owners = owners or [self.worker, self.manager] + [
            User.objects.create_user(f'field-budget-{n}@example.test', None, username=f'field-budget-{n}')
            for n in range(max(0, participants - 2))
        ]
        rows = []
        xml = (
            '<View><Text name="doc" value="$text"/>'
            + ''.join(f'<TextArea name="f{n}" toName="doc" maxSubmissions="1"/>' for n in range(size))
            + '</View>'
        )
        for owner in owners[:participants]:
            row = self.make_source(worker=owner)
            if rows:
                row.assignment.task = rows[0].assignment.task
                row.assignment.save()
            data = {
                'project': {'id': self.project.pk, 'label_config': xml},
                'task': {'id': row.assignment.task_id, 'data': {'text': 'Bounded synthetic document'}},
                'annotation': {
                    'was_cancelled': False,
                    'result': [
                        {'type': 'textarea', 'from_name': f'f{n}', 'to_name': 'doc', 'value': {'text': ['x']}}
                        for n in range(size)
                    ],
                },
            }
            row.result_snapshot = data
            row.result_hash = _snapshot_hash(data)
            Submission.objects.filter(pk=row.pk).update(result_snapshot=data, result_hash=row.result_hash)
            rows.append(row)
        return rows

    def test_real_max_cost_and_whole_batch_one_more_task_reject_before_work(self):
        rows = self.custom_task(20, 20)
        ids = [rows[0].assignment.task_id]
        with patch.object(fields, 'normalize', wraps=fields.normalize) as norm, patch.object(
            fields, 'equal', wraps=fields.equal
        ) as eq:
            response = self.preview(ids)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((norm.call_count, eq.call_count), (800, 15200))
        self.assertEqual(
            response.data['processing_cost']['total'], {'normalized_values': 800, 'field_equalities': 15200}
        )
        extra = self.custom_task(2, 1)
        with patch.object(fields, 'normalize') as norm, patch.object(fields, 'equal') as eq:
            response = self.preview(ids + [extra[0].assignment.task_id])
        self.assertEqual(response.status_code, 422, response.data)
        norm.assert_not_called()
        eq.assert_not_called()

    def test_reachable_401_normalizations_is_rejected_without_any_normalization(self):
        rows = self.custom_task(20, 19)
        owners = [r.submitted_by for r in rows]
        other = self.custom_task(3, 7, owners)
        with patch.object(fields, 'normalize') as norm, patch.object(fields, 'equal') as eq:
            response = self.preview([rows[0].assignment.task_id, other[0].assignment.task_id])
        self.assertEqual(response.status_code, 422, response.data)
        norm.assert_not_called()
        eq.assert_not_called()

    def test_final_manifest_or_actual_render_limit_rolls_back_creation_with_real_work(self):
        preview = self.preview().data
        with patch.object(b, 'MAX_MANIFEST', 1), patch.object(fields, 'normalize', wraps=fields.normalize) as norm:
            response, body, key = self.create(preview, expected=422)
        self.assertGreater(norm.call_count, 0)
        self.assertEqual(FieldBatch.objects.count(), 0)
        created, _, _ = self.create(preview, key=key)
        self.assertEqual(created.status_code, 201)
        # A second intent must also roll back if the serialized output is oversized.
        next_preview = self.preview().data
        with patch.object(fields, 'MAX_OUTPUT', 1), patch.object(fields, 'normalize', wraps=fields.normalize) as norm:
            self.create(next_preview, expected=422)
        self.assertGreater(norm.call_count, 0)
        self.assertEqual(FieldBatch.objects.count(), 1)

    def test_render_tampering_discards_creation_and_keeps_original_key_unused(self):
        preview = self.preview().data
        original = Response.render

        def tamper(response):
            result = original(response)
            if response.status_code == 201:
                response.content = b'{"changed":true}'
            return result

        with patch.object(Response, 'render', tamper):
            _, body, key = self.create(preview, expected=409)
        self.assertEqual(FieldBatch.objects.count(), 0)
        self.create(preview, key=key)

    def test_database_unsafe_names_reject_before_work_and_valid_unicode_roundtrips(self):
        self.client.raise_request_exception = False
        for name in ('bad\x00name', 'bad\ud800name'):
            with patch.object(fields, 'normalize') as norm:
                response = self.client.post(
                    self.base + 'preview/',
                    json.dumps(
                        {'name': name, 'scope': {'kind': 'tasks', 'task_ids': [self.task_id]}}, ensure_ascii=True
                    ),
                    content_type='application/json',
                )
            self.assertEqual(response.status_code, 400)
            norm.assert_not_called()
        created, _, _ = self.create(self.preview(name='  字段 😀  ').data)
        self.assertEqual(created.data['name'], '字段 😀')

    def test_list_metadata_is_checked_each_pass_and_manifest_sql_size_guard_precedes_decode(self):
        created, _, _ = self.create()
        original = JSONField.from_db_value

        def decode(field, value, *args):
            if isinstance(value, str) and 'field-batches/v1' in value:
                self.fail('Oversized Manifest reached JSON decoding')
            return original(field, value, *args)

        with patch.object(b, 'MAX_MANIFEST_STORED', 1), patch.object(JSONField, 'from_db_value', decode):
            response = self.client.get(self.base)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(FieldBatch.objects.count(), 1)

    def test_many_unsubmitted_tasks_have_zero_calculation_cost(self):
        tasks = [Task.objects.create(project=self.project, data={}) for _ in range(100)]
        with patch.object(fields, 'normalize') as norm, patch.object(fields, 'equal') as eq:
            response = self.preview([t.pk for t in tasks])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['display_counts'], {'agreement': 0, 'disagreement': 0, 'unavailable': 100})
        self.assertEqual(response.data['processing_cost']['total'], b.ZERO)
        norm.assert_not_called()
        eq.assert_not_called()

    def test_original_key_metadata_keeps_frozen_engine_without_running_current_processing(self):
        created, body, key = self.create()
        with patch.object(fields, 'ENGINE', 'not-the-frozen-engine/v2'), patch.object(
            b, 'MAX_NORMALIZATIONS', 0
        ), patch.object(fields, 'normalize', side_effect=AssertionError('No recovery computation')):
            replay = self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data['engine'], created.data['engine'])
        self.assertEqual(replay.data['manifest_hash'], created.data['manifest_hash'])
        self.assertEqual(replay.data['processing_cost']['total'], b.ZERO)

    def test_rehashed_invalid_cohort_selection_and_boolean_scope_are_rejected(self):
        created, body, key = self.create()
        row = FieldBatch.objects.get(pk=created.data['id'])
        migration = import_module('classification_disagreements.migrations.0009_field_batches')
        editor = connection.schema_editor(atomic=False)
        migration.uninstall(None, editor)
        original = copy.deepcopy(row.manifest)
        try:
            for change in ('order', 'missing', 'boolean'):
                m = copy.deepcopy(original)
                if change == 'order':
                    m['tasks'][0]['cohort'].reverse()
                elif change == 'missing':
                    m['tasks'][0]['selection_reason'] = 'missing_submission'
                else:
                    m['project_id'] = True
                FieldBatch.objects.filter(pk=row.pk).update(manifest=m, manifest_hash=fields.digest(m))
                response = self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=key)
                self.assertEqual(response.status_code, 409, response.data)
        finally:
            FieldBatch.objects.filter(pk=row.pk).update(manifest=original, manifest_hash=fields.digest(original))
            migration.install(None, editor)
