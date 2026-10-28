import json
from unittest.mock import patch
from uuid import uuid4

from classification_disagreements import ner, ner_api, ner_arbitration_api
from classification_disagreements import ner_arbitration as a
from classification_disagreements import ner_batches as b
from classification_disagreements.models import NerArbitrationDecision, NerArbitrationTask, NerBatch
from classification_disagreements.tests import test_ner_batches as fixtures
from django.db import DatabaseError, connection, transaction
from django.db.models import JSONField
from django.test import override_settings
from organizations.models import OrganizationMember
from projects.models import ProjectMember
from rest_framework.response import Response
from rest_framework.test import APITestCase
from tasks.models import ReviewDecision, Submission, TaskAssignment
from users.models import User


@override_settings(
    ANNOTATION_NER_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_NER_BATCHES_ENABLED=True,
    ANNOTATION_NER_ARBITRATION_ENABLED=True,
)
class TestNerArbitration(APITestCase):
    setUpTestData = classmethod(fixtures.TestNerBatches.setUpTestData.__func__)
    make_source = fixtures.TestNerBatches.make_source
    write = fixtures.TestNerBatches.write
    preview = fixtures.TestNerBatches.preview
    create = fixtures.TestNerBatches.create

    def setUp(self):
        fixtures.TestNerBatches.setUp(self)
        self.arb_base = f'/api/projects/{self.project.pk}/ner-arbitrations/'
        modes = self.settings(ANNOTATION_NER_ARBITRATION_MODES={f'{self.org.pk}:{self.project.pk}': 'fork_review'})
        modes.enable()
        self.addCleanup(modes.disable)
        self.independent = User.objects.create(email='ner-independent@test.local', active_organization=self.org)
        OrganizationMember.objects.create(organization=self.org, user=self.independent)
        ProjectMember.objects.create(project=self.project, user=self.independent, role='reviewer', enabled=True)
        self.batch = self.create()[0].data

    def dispatch_body(self):
        return {
            'ner_batch_id': self.batch['id'],
            'task_id': self.task_id,
            'expected_manifest_hash': self.batch['manifest_hash'],
            'assignee_id': self.independent.pk,
        }

    def dispatch(self, value=None, key=None, expected=201):
        key = str(key or uuid4())
        value = self.dispatch_body() if value is None else value
        result = self.client.post(self.arb_base, value, format='json', HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(result.status_code, expected, result.data)
        return result, value, key

    def command(self, row, action='decision', key=None, body=None, expected=201):
        value = {
            'expected_version': row['version'],
            'expected_input_set_hash': row['input_set_hash'],
            'reason': '依据 frozen source',
        }
        if action == 'decision':
            value['winner_submission_id'] = self.first.pk
        if body is not None:
            value = body
        key = str(key or uuid4())
        result = self.client.generic(
            'POST',
            f'{self.arb_base}{row["id"]}/{action}/',
            json.dumps(value),
            content_type='application/json',
            HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertEqual(result.status_code, expected, result.data)
        return result, value, key

    def detail(self, row, **query):
        return self.client.get(f'{self.arb_base}{row["id"]}/', query)

    def test_adopt_exact_revision_and_preserve_all_business_state(self):
        before = list(Submission.objects.order_by('pk').values())
        reviews = ReviewDecision.objects.count()
        with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as engine:
            row = self.dispatch()[0].data
        self.assertEqual(engine.call_count, 2)
        self.client.force_authenticate(self.independent)
        with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as engine:
            value = self.detail(row)
        self.assertEqual(value.status_code, 200, value.data)
        self.assertEqual(engine.call_count, 2)
        self.assertNotIn('origin_manifest_hash', value.data)
        self.assertNotIn('batch_manifest_hash', json.dumps(value.data))
        self.assertEqual(value.data['source_status'], 'verified')
        self.assertEqual(len(value.data['inputs']), 2)
        with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as engine:
            saved = self.command(row)[0]
        self.assertEqual(engine.call_count, 2)
        self.assertEqual(saved.data['winner_revision'], self.first.revision)
        self.assertEqual(self.detail(row).data['decision']['winner_submission_id'], self.first.pk)
        self.assertEqual(list(Submission.objects.order_by('pk').values()), before)
        self.assertEqual(ReviewDecision.objects.count(), reviews)
        self.assertEqual(NerArbitrationDecision.objects.count(), 1)

    def test_source_free_original_key_recovery_after_terminal_source_loss_budget_shrink(self):
        row, dispatch_body, dispatch_key = self.dispatch()
        self.client.force_authenticate(self.independent)
        saved, value, key = self.command(row.data)
        NerBatch.objects.filter(pk=self.batch['id']).delete()
        Submission.objects.filter(pk=self.first.pk).delete()
        with (
            patch.object(a, 'MAX_CALLS', 0),
            patch.object(ner_api, 'load', side_effect=AssertionError('No source load')),
        ):
            recovered = self.command(row.data, key=key, body=value, expected=200)[0]
            self.assertEqual(recovered.data['receipt'], saved.data['receipt'])
            self.client.force_authenticate(self.manager)
            replay = self.dispatch(dispatch_body, dispatch_key, expected=200)[0]
            self.assertEqual(replay.data['receipt']['status_at_commit'], 'pending')
            self.assertEqual(replay.data['current_status'], 'resolved')
        self.client.force_authenticate(self.independent)
        hidden = self.detail(row.data)
        self.assertEqual(hidden.status_code, 200, hidden.data)
        self.assertEqual(hidden.data['source_status'], 'unavailable')
        self.assertEqual(hidden.data['inputs'], [])
        self.assertEqual(hidden.data['frozen_counts']['input_count'], 2)
        self.assertIn('decision', hidden.data)

    def test_cancel_original_key_and_redispatch_unique_semantics(self):
        row, body, key = self.dispatch()
        self.dispatch(expected=409)
        cancelled, cancel_body, cancel_key = self.command(row.data, 'cancel')
        with patch.object(ner_api, 'load', side_effect=AssertionError('No hydration')):
            replay = self.command(row.data, 'cancel', cancel_key, cancel_body, expected=200)[0]
            self.assertEqual(replay.data['receipt'], cancelled.data['receipt'])
            old = self.dispatch(body, key, expected=200)[0]
            self.assertEqual(old.data['current_status'], 'cancelled')
        new = self.dispatch()[0]
        self.assertNotEqual(new.data['id'], row.data['id'])

    def test_authority_type_scope_and_spoofed_fields(self):
        body = self.dispatch_body()
        for update in (
            {'batch_id': self.batch['id']},
            {'actor_id': self.manager.pk},
            {'source': 'trusted'},
            {'assignee_id': self.reviewer.pk},
            {'assignee_id': self.manager.pk},
            {'task_id': 2**53},
        ):
            self.dispatch({**body, **update}, expected=409 if update.get('assignee_id') else 400)
        self.dispatch({**body, 'ner_batch_id': 2**53 - 1}, expected=404)
        self.client.force_authenticate(self.worker)
        self.dispatch(expected=404)
        self.client.force_authenticate(self.manager)
        row = self.dispatch()[0].data
        self.command(row, expected=404)
        self.client.force_authenticate(self.reviewer)
        self.assertEqual(self.client.get(self.arb_base).data['count'], 0)
        self.assertEqual(self.detail(row).status_code, 404)
        self.client.force_authenticate(self.independent)
        ProjectMember.objects.filter(project=self.project, user=self.independent).update(enabled=False)
        self.assertIn(self.detail(row).status_code, (403, 404))
        self.command(row, expected=404)

    def test_independent_flags_core_boundary_and_metadata_cost(self):
        with self.settings(ANNOTATION_NER_ARBITRATION_ENABLED=False):
            self.assertEqual(self.client.get(self.arb_base + 'capability/').status_code, 404)
        with self.settings(ANNOTATION_NER_ARBITRATION_MODES={f'{self.org.pk}:{self.project.pk}': 'core_only_review'}):
            self.dispatch(expected=404)
        with (
            patch.object(ner_api, 'load', side_effect=AssertionError('No snapshots')),
            patch.object(ner, 'Evaluator', side_effect=AssertionError('No scorer')),
        ):
            self.assertEqual(self.client.get(self.arb_base + 'capability/').status_code, 200)
            self.assertEqual(self.client.get(self.arb_base + 'assignees/', self.dispatch_body()).status_code, 400)
            params = {k: v for k, v in self.dispatch_body().items() if k != 'assignee_id'}
            self.assertEqual(self.client.get(self.arb_base + 'assignees/', params).status_code, 200)
            self.assertEqual(self.client.get(self.arb_base).status_code, 200)

    def test_complete_target_budget_preflight_no_sources_and_unchanged_key(self):
        key = str(uuid4())
        with (
            patch.object(a, 'MAX_CALLS', 1),
            patch.object(ner_api, 'load', side_effect=AssertionError('No hydration')),
        ):
            self.dispatch(key=key, expected=422)
        self.assertEqual(NerArbitrationTask.objects.count(), 0)
        row = self.dispatch(key=key)[0].data
        with (
            patch.object(a, 'MAX_CALLS', 1),
            patch.object(ner_api, 'load', side_effect=AssertionError('No hydration')),
        ):
            self.command(row, 'cancel')

    def test_cas_terminal_foreign_winner_reason_and_key_conflicts(self):
        row = self.dispatch()[0].data
        self.client.force_authenticate(self.independent)
        _, value, key = self.command(
            row,
            body={
                'expected_version': 2,
                'expected_input_set_hash': row['input_set_hash'],
                'winner_submission_id': self.first.pk,
                'reason': 'CAS',
            },
            expected=409,
        )
        for reason in ('', ' ', '\x00', '\ud800', 'a' * 2001):
            self.command(row, body={**value, 'expected_version': 1, 'reason': reason}, expected=400)
        self.command(row, body={**value, 'expected_version': 1, 'winner_submission_id': 2**53 - 1}, expected=400)
        saved, good, original = self.command(row)
        self.command(row, key=original, body={**good, 'reason': good['reason'] + ' '}, expected=409)
        self.command(row, expected=409)
        self.command(row, key=original, body=good, expected=200)
        self.client.force_authenticate(self.manager)
        self.command(row, 'cancel', expected=409)

    def test_frozen_replay_new_revision_and_full_cohort_integrity(self):
        row = self.dispatch()[0].data
        old = self.detail(row).data
        newer = self.make_source(assignment=self.first.assignment, revision=2)
        self.write(newer, [])
        Submission.objects.filter(pk=self.first.pk).update(status=Submission.Status.SUPERSEDED)
        self.assertEqual(self.detail(row).data['comparison'], old['comparison'])
        self.write(self.second, [])
        unavailable = self.detail(row).data
        self.assertIsNone(unavailable['comparison'])
        self.assertEqual(unavailable['inputs'], [])
        self.client.force_authenticate(self.independent)
        self.command(row, expected=409)

    def test_last_counter_and_render_mutations_reject_and_rollback(self):
        render = Response.render

        def corrupt(response):
            value = render(response)
            if response.status_code == 201 and response.data.get('action') == 'create':
                self.write(self.second, [])
            return value

        with patch.object(Response, 'render', corrupt):
            self.dispatch(expected=409)
        self.assertEqual(NerArbitrationTask.objects.count(), 0)
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        row = self.dispatch()[0].data
        original = ner.check_engine
        calls = []

        def mutate(value):
            original(value)
            calls.append(1)
            if len(calls) == 2:
                self.write(self.second, [])

        with patch.object(ner, 'check_engine', mutate):
            self.assertEqual(self.detail(row).status_code, 409)

    def test_sql_immutable_fields_and_terminal_guard(self):
        row = self.dispatch()[0].data
        for update in (
            {'creator_source': 'other'},
            {'origin_hash': '0' * 64},
            {'status': 'resolved', 'version': 2},
            {'assignee_id_frozen': self.manager.pk},
            {'participant_ids': []},
        ):
            with self.assertRaises(DatabaseError), transaction.atomic():
                NerArbitrationTask.objects.filter(pk=row['id']).update(**update)
        self.client.force_authenticate(self.independent)
        self.command(row)
        with self.assertRaises(DatabaseError), transaction.atomic():
            NerArbitrationDecision.objects.update(reason='rewritten')

    def test_twenty_people_exact_call_cost_and_target_budget(self):
        for index in range(18):
            actor = User.objects.create_user(f'ner-arb-limit-{index}@example.test', None)
            row = self.make_source(worker=actor)
            row.assignment.task = self.first.assignment.task
            row.assignment.save()
            self.write(row, [(0, 5, 'PER')])
        self.batch = self.create()[0].data
        with (
            patch.object(ner_api, 'load', wraps=ner_api.load) as load,
            patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as engine,
        ):
            row = self.dispatch()[0].data
        self.assertEqual((load.call_count, engine.call_count), (2, 380))
        self.client.force_authenticate(self.independent)
        with (
            patch.object(ner_api, 'load', wraps=ner_api.load) as load,
            patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as engine,
        ):
            self.command(row)
        self.assertEqual((load.call_count, engine.call_count), (2, 380))

    def test_double_empty_pair_cost_and_whole_empty_revision_adoption(self):
        self.write(self.first, [])
        third = self.make_source(worker=self.manager)
        third.assignment.task = self.first.assignment.task
        third.assignment.save()
        self.write(third, [])
        self.batch = self.create()[0].data
        with patch.object(ner, 'Evaluator', wraps=ner.Evaluator) as engine:
            row = self.dispatch()[0].data
        self.assertEqual(engine.call_count, 4)  # Q=2, the empty/empty pair costs zero.
        self.client.force_authenticate(self.independent)
        self.command(row)
        self.assertEqual(NerArbitrationDecision.objects.get().winner['submission_id'], self.first.pk)

    def test_all_empty_missing_and_unavailable_inputs_are_not_dispatchable(self):
        for change in ('empty', 'missing', 'malformed'):
            self.write(self.first, [(0, 5, 'PER')])
            self.write(self.second, [(0, 4, 'PER')])
            missing = None
            if change == 'empty':
                self.write(self.first, [])
                self.write(self.second, [])
            elif change == 'missing':
                missing = TaskAssignment.objects.create(
                    project=self.project, task=self.first.assignment.task, assignee=self.manager
                )
            else:
                self.write(self.first, [], lambda v: v['annotation'].pop('result'))
            self.batch = self.create()[0].data
            with patch.object(ner_api, 'load', side_effect=AssertionError('Ineligible metadata first')):
                self.dispatch(expected=409)
            if missing:
                missing.delete()
        self.assertFalse(NerArbitrationTask.objects.exists())

    def test_semantic_dedup_ignores_batch_metadata_but_tracks_new_revision(self):
        row = self.dispatch()[0].data
        self.batch = self.create()[0].data
        self.dispatch(expected=409)
        self.command(row, 'cancel')
        newer = self.make_source(assignment=self.first.assignment, revision=2)
        self.write(newer, [])
        self.batch = self.create()[0].data
        fresh = self.dispatch()[0].data
        self.assertNotEqual(fresh['input_set_hash'], row['input_set_hash'])

    def test_unrelated_batch_sources_are_never_hydrated_or_budgeted(self):
        other = fixtures.TestNerBatches.extra_task(self, owners=(self.worker, self.independent))
        self.batch = self.create(self.preview([self.task_id, other[0].assignment.task_id]).data)[0].data
        self.write(other[0], [], lambda v: v.update(padding='x' * 8192))
        original = ner_api.load

        def only_target(query, **kwargs):
            self.assertEqual(set(query.values_list('pk', flat=True)), {self.first.pk, self.second.pk})
            return original(query, **kwargs)

        with patch.object(ner_api, 'load', only_target), patch.object(ner, 'MAX_SNAPSHOT_BYTES', 8192):
            row = self.dispatch()[0].data
            self.client.force_authenticate(self.independent)
            self.assertEqual(self.detail(row).status_code, 200)

    def test_budget_boundaries_precede_scorer_and_leave_original_key_reusable(self):
        _, prepared = b.build_plan(self.project, [self.task_id])
        canonical = sum(ner.bounded(r.result_snapshot) for r in (self.first, self.second))
        column = connection.ops.quote_name(Submission._meta.db_table) + '.result_snapshot'
        expression = (
            f'OCTET_LENGTH(CAST({column} AS TEXT))'
            if connection.vendor == 'postgresql'
            else f'LENGTH(CAST({column} AS BLOB))'
        )
        from django.db.models.expressions import RawSQL

        stored = sum(
            Submission.objects.filter(pk__in=[self.first.pk, self.second.pk])
            .annotate(size=RawSQL(expression, []))
            .values_list('size', flat=True)
        )
        for name, exact in (
            ('MAX_WORK', prepared[self.task_id]['work_units']),
            ('MAX_SNAPSHOT_BYTES', canonical),
            ('MAX_STORED_BYTES', stored),
        ):
            key = str(uuid4())
            with patch.object(ner, name, exact - 1), patch.object(ner, 'Evaluator') as engine:
                self.dispatch(key=key, expected=422)
            engine.assert_not_called()
            with patch.object(ner, name, exact):
                row = self.dispatch(key=key)[0].data
            self.command(row, 'cancel')
        with patch.object(ner, 'MAX_OUTPUT', 128), patch.object(ner, 'Evaluator') as engine:
            self.dispatch(expected=422)
        engine.assert_not_called()

    def test_operation_withdrawal_blocks_reads_and_recovery_but_allows_security_cancel(self):
        from access_control.project_operations import READ_WORK, LegacyProjectOperationProvider, ProjectOperationPolicy

        row, value, key = self.dispatch()
        for operations in (frozenset(), frozenset({READ_WORK, 'organize_arbitration', 'arbitrate', 'review'})):
            with patch.object(
                LegacyProjectOperationProvider,
                'evaluate',
                return_value=ProjectOperationPolicy('withdrawn-v1', 'paused', operations),
            ):
                self.dispatch(value, key, expected=403)
                self.assertFalse(self.client.get(self.arb_base + 'capability/').data['can_create'])
        with (
            patch.object(
                LegacyProjectOperationProvider,
                'evaluate',
                return_value=ProjectOperationPolicy('withdrawn-v1', 'paused', frozenset()),
            ),
            patch.object(ner_api, 'load', side_effect=AssertionError('No source load')),
        ):
            self.assertEqual(self.detail(row.data).status_code, 403)
            self.command(row.data, 'cancel')

    def test_original_receipt_binds_principal_and_identity_source(self):
        from dataclasses import replace

        row, value, key = self.dispatch()
        resolve = ner_arbitration_api.resolve_actor
        for field in ('source', 'principal_id'):

            def changed(request):
                principal, actor = resolve(request)
                return replace(principal, **{field: 'isolated-other'}), actor

            with patch.object(ner_arbitration_api, 'resolve_actor', changed):
                self.dispatch(value, key, expected=409)
        self.client.force_authenticate(self.independent)
        saved, value, key = self.command(row.data)
        with patch.object(ner_arbitration_api, 'resolve_actor', changed):
            self.command(row.data, key=key, body=value, expected=409)

    def test_metadata_json_storage_gate_precedes_decode(self):
        row = NerArbitrationTask.objects.get(pk=self.dispatch()[0].data['id'])
        row.pk = None
        row.idempotency_key = uuid4()
        row.input_set_hash = 'f' * 64
        row.origin = {'padding': 'x' * (a.JSON_LIMITS['origin'][1] + 1)}
        row.save(force_insert=True)
        decoder = JSONField.from_db_value

        def guarded_decode(field, value, *args):
            if isinstance(value, str) and len(value) > a.JSON_LIMITS['origin'][1]:
                raise AssertionError('Oversized ledger decoded')
            return decoder(field, value, *args)

        with (
            patch.object(JSONField, 'from_db_value', guarded_decode),
            patch.object(ner_api, 'load', side_effect=AssertionError('Metadata only')),
        ):
            self.assertEqual(self.client.get(self.arb_base).status_code, 409)

    def test_decision_database_guard_rejects_foreign_input_and_winner(self):
        row = NerArbitrationTask.objects.get(pk=self.dispatch()[0].data['id'])
        args = dict(
            project=self.project,
            arbitration=row,
            actor=self.independent,
            actor_id_frozen=self.independent.pk,
            principal='test',
            source='label_studio_local',
            input_set_hash=row.input_set_hash,
            rule=a.RULE,
            winner=row.origin['item']['cohort'][0]['source'],
            reason='reason',
            idempotency_key=uuid4(),
            request_hash='a' * 64,
            payload_hash='b' * 64,
        )
        for change in (
            {'input_set_hash': '0' * 64},
            {'actor_id_frozen': self.manager.pk},
            {'winner': {**args['winner'], 'revision': 999}},
            {'rule': 'adopt-exact-submission/v1'},
        ):
            with self.assertRaises(DatabaseError), transaction.atomic():
                NerArbitrationDecision.objects.create(**{**args, **change})

    def test_nullable_links_never_replace_frozen_identity_and_project_cascade_is_explicit(self):
        row = self.dispatch()[0].data
        NerArbitrationTask.objects.filter(pk=row['id']).update(created_by=None, assignee=None)
        with self.assertRaises(DatabaseError), transaction.atomic():
            NerArbitrationTask.objects.filter(pk=row['id']).update(assignee=self.manager)
        self.client.force_authenticate(self.independent)
        self.command(row)
        self.independent.delete()
        task, decision = NerArbitrationTask.objects.get(), NerArbitrationDecision.objects.get()
        self.assertEqual((task.assignee_id_frozen, decision.actor_id_frozen), (row['assignee_id'], row['assignee_id']))
        self.assertIsNone(decision.actor_id)
        self.client.force_authenticate(self.manager)
        self.project.delete()
        self.assertFalse(NerArbitrationTask.objects.exists())
        self.assertFalse(NerArbitrationDecision.objects.exists())

    def test_dispatch_original_key_survives_assignee_deletion_but_current_actor_recovery_does_not(self):
        row, value, key = self.dispatch()
        self.independent.delete()
        with patch.object(ner_api, 'load', side_effect=AssertionError('Source-free recovery')):
            replay = self.dispatch(value, key, expected=200)[0]
        self.assertEqual(replay.data['receipt'], row.data['receipt'])
        admin = User.objects.create_user('ner-arb-session-admin@example.test', None, is_staff=True, is_superuser=True)
        User.objects.filter(pk=self.manager.pk).update(is_active=False, session_actor=admin)
        result = self.client.post(self.arb_base, value, format='json', HTTP_IDEMPOTENCY_KEY=key)
        self.assertIn(result.status_code, (401, 403, 404))

    def test_mutation_during_write_render_rolls_back_and_original_key_remains_available(self):
        render, key = Response.render, str(uuid4())

        def revoke(response):
            value = render(response)
            if response.status_code == 201 and response.data.get('action') == 'create':
                ProjectMember.objects.filter(project=self.project, user=self.independent).update(enabled=False)
            return value

        with patch.object(Response, 'render', revoke):
            self.dispatch(key=key, expected=409)
        self.assertFalse(NerArbitrationTask.objects.exists())
        self.assertTrue(ProjectMember.objects.get(project=self.project, user=self.independent).enabled)
        row = self.dispatch(key=key)[0].data
        self.client.force_authenticate(self.independent)

        def rebind(response):
            value = render(response)
            if response.status_code == 201 and response.data.get('action') == 'decision':
                TaskAssignment.objects.filter(pk=self.first.assignment_id).update(assignee=self.manager)
            return value

        with patch.object(Response, 'render', rebind):
            self.command(row, key=key, expected=409)
        self.assertFalse(NerArbitrationDecision.objects.exists())
        self.command(row, key=key)
