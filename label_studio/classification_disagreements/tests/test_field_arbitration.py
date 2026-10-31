import json
from unittest.mock import patch
from uuid import uuid4

from classification_disagreements import field_api, field_arbitration_api, fields
from classification_disagreements import field_arbitration as a
from classification_disagreements.models import FieldArbitrationDecision, FieldArbitrationTask, FieldBatch
from classification_disagreements.tests import test_field_batches as fixtures
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
    ANNOTATION_FIELD_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_FIELD_BATCHES_ENABLED=True,
    ANNOTATION_FIELD_ARBITRATION_ENABLED=True,
)
class TestFieldArbitration(APITestCase):
    setUpTestData = classmethod(fixtures.TestFieldBatches.setUpTestData.__func__)
    make_source = fixtures.TestFieldBatches.make_source
    write = fixtures.TestFieldBatches.write
    preview = fixtures.TestFieldBatches.preview
    create = fixtures.TestFieldBatches.create

    def setUp(self):
        fixtures.TestFieldBatches.setUp(self)
        self.arb_base = f'/api/projects/{self.project.pk}/field-arbitrations/'
        modes = self.settings(ANNOTATION_FIELD_ARBITRATION_MODES={f'{self.org.pk}:{self.project.pk}': 'fork_review'})
        modes.enable()
        self.addCleanup(modes.disable)
        self.independent = User.objects.create(email='fields-independent@test.local', active_organization=self.org)
        OrganizationMember.objects.create(organization=self.org, user=self.independent)
        ProjectMember.objects.create(project=self.project, user=self.independent, role='reviewer', enabled=True)
        self.batch = self.create()[0].data

    def dispatch_body(self):
        return {
            'field_batch_id': self.batch['id'],
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

    def test_source_free_original_key_recovery_after_terminal_source_loss_budget_shrink(self):
        row, dispatch_body, dispatch_key = self.dispatch()
        self.client.force_authenticate(self.independent)
        saved, value, key = self.command(row.data)
        FieldBatch.objects.filter(pk=self.batch['id']).delete()
        Submission.objects.filter(pk=self.first.pk).delete()
        with (
            patch.object(a, 'MAX_EQUALITIES', 0),
            patch.object(field_api, 'load', side_effect=AssertionError('No source load')),
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
        with patch.object(field_api, 'load', side_effect=AssertionError('No hydration')):
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
        self.dispatch({**body, 'field_batch_id': 2**53 - 1}, expected=404)
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
        with self.settings(ANNOTATION_FIELD_ARBITRATION_ENABLED=False):
            self.assertEqual(self.client.get(self.arb_base + 'capability/').status_code, 404)
        with self.settings(
            ANNOTATION_FIELD_ARBITRATION_MODES={f'{self.org.pk}:{self.project.pk}': 'core_only_review'}
        ):
            self.dispatch(expected=404)
        with (
            patch.object(field_api, 'load', side_effect=AssertionError('No snapshots')),
            patch.object(fields, 'normalize', side_effect=AssertionError('No scorer')),
        ):
            self.assertEqual(self.client.get(self.arb_base + 'capability/').status_code, 200)
            self.assertEqual(self.client.get(self.arb_base + 'assignees/', self.dispatch_body()).status_code, 400)
            params = {k: v for k, v in self.dispatch_body().items() if k != 'assignee_id'}
            self.assertEqual(self.client.get(self.arb_base + 'assignees/', params).status_code, 200)
            self.assertEqual(self.client.get(self.arb_base).status_code, 200)

    def test_complete_target_budget_preflight_no_sources_and_unchanged_key(self):
        key = str(uuid4())
        with (
            patch.object(a, 'MAX_EQUALITIES', 1),
            patch.object(field_api, 'load', side_effect=AssertionError('No hydration')),
        ):
            self.dispatch(key=key, expected=422)
        self.assertEqual(FieldArbitrationTask.objects.count(), 0)
        row = self.dispatch(key=key)[0].data
        with (
            patch.object(a, 'MAX_EQUALITIES', 1),
            patch.object(field_api, 'load', side_effect=AssertionError('No hydration')),
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
        self.write(newer, ('', '', ''))
        Submission.objects.filter(pk=self.first.pk).update(status=Submission.Status.SUPERSEDED)
        self.assertEqual(self.detail(row).data['comparison'], old['comparison'])
        self.write(self.second, ('', '', ''))
        unavailable = self.detail(row).data
        self.assertIsNone(unavailable['comparison'])
        self.assertEqual(unavailable['inputs'], [])
        self.client.force_authenticate(self.independent)
        self.command(row, expected=409)

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
                FieldArbitrationTask.objects.filter(pk=row['id']).update(**update)
        self.client.force_authenticate(self.independent)
        self.command(row)
        with self.assertRaises(DatabaseError), transaction.atomic():
            FieldArbitrationDecision.objects.update(reason='rewritten')

    def test_semantic_dedup_ignores_batch_metadata_but_tracks_new_revision(self):
        row = self.dispatch()[0].data
        self.batch = self.create()[0].data
        self.dispatch(expected=409)
        self.command(row, 'cancel')
        newer = self.make_source(assignment=self.first.assignment, revision=2)
        self.write(newer, ('', '', ''))
        self.batch = self.create()[0].data
        fresh = self.dispatch()[0].data
        self.assertNotEqual(fresh['input_set_hash'], row['input_set_hash'])

    def test_unrelated_batch_sources_are_never_hydrated_or_budgeted(self):
        other = fixtures.TestFieldBatches.extra_task(self, owners=(self.worker, self.independent))
        self.batch = self.create(self.preview([self.task_id, other[0].assignment.task_id]).data)[0].data
        self.write(other[0], ('', '', ''), lambda v: v.update(padding='x' * 8192))
        original = field_api.load

        def only_target(query, **kwargs):
            self.assertEqual(set(query.values_list('pk', flat=True)), {self.first.pk, self.second.pk})
            return original(query, **kwargs)

        with patch.object(field_api, 'load', only_target), patch.object(fields, 'MAX_SNAPSHOT_BYTES', 8192):
            row = self.dispatch()[0].data
            self.client.force_authenticate(self.independent)
            self.assertEqual(self.detail(row).status_code, 200)

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
            patch.object(field_api, 'load', side_effect=AssertionError('No source load')),
        ):
            self.assertEqual(self.detail(row.data).status_code, 403)
            self.command(row.data, 'cancel')

    def test_original_receipt_binds_principal_and_identity_source(self):
        from dataclasses import replace

        row, value, key = self.dispatch()
        resolve = field_arbitration_api.resolve_actor
        for field in ('source', 'principal_id'):

            def changed(request):
                principal, actor = resolve(request)
                return replace(principal, **{field: 'isolated-other'}), actor

            with patch.object(field_arbitration_api, 'resolve_actor', changed):
                self.dispatch(value, key, expected=409)
        self.client.force_authenticate(self.independent)
        saved, value, key = self.command(row.data)
        with patch.object(field_arbitration_api, 'resolve_actor', changed):
            self.command(row.data, key=key, body=value, expected=409)

    def test_metadata_json_storage_gate_precedes_decode(self):
        row = FieldArbitrationTask.objects.get(pk=self.dispatch()[0].data['id'])
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
            patch.object(field_api, 'load', side_effect=AssertionError('Metadata only')),
        ):
            self.assertEqual(self.client.get(self.arb_base).status_code, 409)

    def test_decision_database_guard_rejects_foreign_input_and_winner(self):
        row = FieldArbitrationTask.objects.get(pk=self.dispatch()[0].data['id'])
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
                FieldArbitrationDecision.objects.create(**{**args, **change})

    def test_nullable_links_never_replace_frozen_identity_and_project_cascade_is_explicit(self):
        row = self.dispatch()[0].data
        FieldArbitrationTask.objects.filter(pk=row['id']).update(created_by=None, assignee=None)
        with self.assertRaises(DatabaseError), transaction.atomic():
            FieldArbitrationTask.objects.filter(pk=row['id']).update(assignee=self.manager)
        self.client.force_authenticate(self.independent)
        self.command(row)
        self.independent.delete()
        task, decision = FieldArbitrationTask.objects.get(), FieldArbitrationDecision.objects.get()
        self.assertEqual((task.assignee_id_frozen, decision.actor_id_frozen), (row['assignee_id'], row['assignee_id']))
        self.assertIsNone(decision.actor_id)
        self.client.force_authenticate(self.manager)
        self.project.delete()
        self.assertFalse(FieldArbitrationTask.objects.exists())
        self.assertFalse(FieldArbitrationDecision.objects.exists())

    def test_dispatch_original_key_survives_assignee_deletion_but_current_actor_recovery_does_not(self):
        row, value, key = self.dispatch()
        self.independent.delete()
        with patch.object(field_api, 'load', side_effect=AssertionError('Source-free recovery')):
            replay = self.dispatch(value, key, expected=200)[0]
        self.assertEqual(replay.data['receipt'], row.data['receipt'])
        admin = User.objects.create_user(
            'fields-arb-session-admin@example.test', None, is_staff=True, is_superuser=True
        )
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
        self.assertFalse(FieldArbitrationTask.objects.exists())
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
        self.assertFalse(FieldArbitrationDecision.objects.exists())
        self.command(row, key=key)

    def test_real_api_tokens_keep_roles_scope_and_current_revocation(self):
        row = self.dispatch()[0].data
        self.org.jwt.legacy_api_tokens_enabled = True
        self.org.jwt.save(update_fields=['legacy_api_tokens_enabled'])
        self.client.force_authenticate(None)
        for actor in (self.worker, self.reviewer):
            self.client.credentials(HTTP_AUTHORIZATION=f'Token {actor.get_token().key}')
            self.assertIn(self.detail(row).status_code, (403, 404))
            listing = self.client.get(self.arb_base)
            if actor == self.reviewer:
                self.assertEqual(listing.status_code, 200, listing.data)
                self.assertEqual(listing.data['count'], 0)
            else:
                self.assertIn(listing.status_code, (403, 404))
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.manager.get_token().key}')
        self.command(row, expected=404)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.independent.get_token().key}')
        self.assertEqual(self.detail(row).status_code, 200)
        _, body, key = self.command(row)
        ProjectMember.objects.filter(project=self.project, user=self.independent).update(enabled=False)
        with patch.object(field_api, 'load', side_effect=AssertionError('Revoked token cannot hydrate')):
            self.assertIn(self.detail(row).status_code, (403, 404))
            self.command(row, key=key, body=body, expected=404)

    def test_field_exact_adoption_truthful_two_pass_cost_and_no_business_writes(self):
        before = list(Submission.objects.order_by('pk').values())
        reviews = ReviewDecision.objects.count()
        for action in ('create', 'detail', 'decision'):
            with (
                patch.object(field_api, 'load', wraps=field_api.load) as load,
                patch.object(fields, 'normalize', wraps=fields.normalize) as normalize,
                patch.object(fields, 'equal', wraps=fields.equal) as equal,
            ):
                if action == 'create':
                    row = self.dispatch()[0].data
                    result = row
                    self.client.force_authenticate(self.independent)
                elif action == 'detail':
                    result = self.detail(row).data
                    self.assertNotIn('origin_manifest_hash', result)
                    self.assertEqual(result['comparison']['pairs'][0]['normalized_rate'], fields.fraction(2, 3))
                else:
                    result = self.command(row)[0].data
            self.assertEqual((load.call_count, normalize.call_count, equal.call_count), (2, 12, 12))
            self.assertEqual(result['processing_cost']['total'], {'normalized_values': 12, 'field_equalities': 12})
            self.assertEqual(result['processing_cost']['scorer_calls'], 0)
        self.assertEqual(FieldArbitrationDecision.objects.get().winner['submission_id'], self.first.pk)
        self.assertEqual(list(Submission.objects.order_by('pk').values()), before)
        self.assertEqual(ReviewDecision.objects.count(), reviews)

    def test_last_equality_source_mutation_discards_answers(self):
        row = self.dispatch()[0].data
        original, calls = fields.equal, []

        def changed(*args):
            value = original(*args)
            calls.append(1)
            if len(calls) == 12:
                self.write(self.second, ('', '', ''))
            return value

        with patch.object(fields, 'equal', changed):
            result = self.detail(row)
        self.assertEqual(len(calls), 12)
        self.assertEqual(result.status_code, 409)
        self.assertNotIn('comparison', result.data)

    def test_format_only_empty_winner_and_missing_cohort_boundaries(self):
        self.write(self.second, ('张三', '上海\n浦东', '001'))
        self.batch = self.create()[0].data
        row = self.dispatch()[0].data
        self.assertTrue(self.detail(row).data['frozen_counts']['has_format_difference'])
        self.command(row, 'cancel')
        self.write(self.first, ('', '', ''))
        third = self.make_source(worker=self.manager)
        third.assignment.task = self.first.assignment.task
        third.assignment.save()
        self.write(third, ('', '', ''))
        self.batch = self.create()[0].data
        row = self.dispatch()[0].data
        self.client.force_authenticate(self.independent)
        self.command(row)
        self.assertEqual(FieldArbitrationDecision.objects.get().winner['submission_id'], self.first.pk)
        self.client.force_authenticate(self.manager)
        self.write(self.second, ('', '', ''))
        self.batch = self.create()[0].data
        with patch.object(field_api, 'load', side_effect=AssertionError('No ineligible hydration')):
            self.dispatch(expected=409)
        self.write(self.second, ('a', 'b', 'c'))
        self.write(self.first, ('a', 'b'))
        self.batch = self.create()[0].data
        self.dispatch(expected=409)

    def test_twenty_people_twenty_fields_exact_maximum(self):
        rows = [self.first, self.second]
        for index in range(18):
            actor = User.objects.create_user(f'field-max-{index}@example.test', None)
            row = self.make_source(worker=actor)
            row.assignment.task = self.first.assignment.task
            row.assignment.save()
            rows.append(row)
        for index, row in enumerate(rows):

            def wide(value, index=index):
                names = [f'f{i}' for i in range(20)]
                value['project']['label_config'] = (
                    '<View><Text name="doc" value="$text"/>'
                    + ''.join(f'<TextArea name="{n}" toName="doc" maxSubmissions="1"/>' for n in names)
                    + '</View>'
                )
                value['annotation']['result'] = [
                    {
                        'id': str(i),
                        'type': 'textarea',
                        'from_name': n,
                        'to_name': 'doc',
                        'value': {'text': [f'{index}:{i}']},
                    }
                    for i, n in enumerate(names)
                ]

            self.write(row, mutate=wide)
        self.batch = self.create()[0].data
        for phase in ('create', 'decision'):
            with (
                patch.object(field_api, 'load', wraps=field_api.load) as load,
                patch.object(fields, 'normalize', wraps=fields.normalize) as n,
                patch.object(fields, 'equal', wraps=fields.equal) as e,
            ):
                result = self.dispatch()[0] if phase == 'create' else self.command(row)[0]
            self.assertEqual((load.call_count, n.call_count, e.call_count), (2, 800, 15200))
            self.assertEqual(
                result.data['processing_cost']['total'], {'normalized_values': 800, 'field_equalities': 15200}
            )
            row = result.data
            self.client.force_authenticate(self.independent)

    def test_source_exact_restore_and_no_successor_fallback(self):
        row = self.dispatch()[0].data
        original = self.detail(row).data
        stored = Submission.objects.filter(pk=self.first.pk).values().get()
        self.first.delete()
        missing = self.detail(row).data
        self.assertIsNone(missing['comparison'])
        self.assertEqual(missing['processing_cost']['hydration_passes'], 2)
        self.assertEqual(missing['processing_cost']['total'], {'normalized_values': 0, 'field_equalities': 0})
        Submission.objects.create(**stored)
        restored = self.detail(row).data
        for key in ('comparison', 'inputs', 'task_evidence_hash', 'input_set_hash', 'calculated_at'):
            self.assertEqual(restored[key], original[key])

    def test_output_limit_rolls_back_but_calculation_is_not_zero(self):
        key = str(uuid4())
        with patch.object(fields, 'MAX_OUTPUT', 128), patch.object(fields, 'normalize', wraps=fields.normalize) as n:
            self.dispatch(key=key, expected=422)
        self.assertGreater(n.call_count, 0)
        self.assertFalse(FieldArbitrationTask.objects.exists())
        self.dispatch(key=key)

    def test_origin_ids_reject_equal_boolean_or_float_values(self):
        import copy

        row = self.dispatch()[0].data
        self.command(row, 'cancel')
        original = FieldArbitrationTask.objects.get(pk=row['id'])
        for name in ('project_id', 'organization_id', 'field_batch_id'):
            clone = copy.copy(original)
            clone.pk = None
            clone.status, clone.version = 'pending', 1
            clone.cancel_key, clone.cancel_request_hash, clone.cancellation = None, '', None
            clone.idempotency_key = uuid4()
            clone.origin = copy.deepcopy(original.origin)
            clone.origin[name] = float(clone.origin[name])
            clone.origin_hash = fields.digest(clone.origin)
            clone.save(force_insert=True)
            response = self.client.get(f'{self.arb_base}{clone.pk}/')
            self.assertEqual(response.status_code, 409, response.data)
            clone.delete()

    def test_cross_organization_and_existing_foreign_winner_are_denied(self):
        from organizations.models import Organization
        from projects.models import Project

        row = self.dispatch()[0].data
        owner = User.objects.create_user('foreign-field-owner@synthetic.invalid', None)
        organization = Organization.create_organization(created_by=owner, title='Other field scope')
        other = Project.objects.create(created_by=owner, organization=organization)
        foreign = f'/api/projects/{other.pk}/field-arbitrations/'
        self.assertEqual(self.client.get(foreign).status_code, 404)
        self.assertEqual(self.client.get(f'{foreign}{row["id"]}/').status_code, 404)
        self.assertEqual(
            self.client.post(
                foreign, self.dispatch_body(), format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
            ).status_code,
            404,
        )
        outsider = self.make_source()
        self.client.force_authenticate(self.independent)
        bad = {
            'expected_version': 1,
            'expected_input_set_hash': row['input_set_hash'],
            'winner_submission_id': outsider.pk,
            'reason': 'Foreign existing revision',
        }
        with patch.object(field_api, 'load', side_effect=AssertionError('Invalid winner cannot hydrate')):
            self.command(row, body=bad, expected=400)

    def test_input_hash_cas_rejects_before_work_and_leaves_key_reusable(self):
        row = self.dispatch()[0].data
        self.client.force_authenticate(self.independent)
        key = str(uuid4())
        value = {
            'expected_version': 1,
            'expected_input_set_hash': 'f' * 64,
            'winner_submission_id': self.first.pk,
            'reason': 'Exact input',
        }
        with patch.object(fields, 'normalize') as normalize:
            self.command(row, key=key, body=value, expected=409)
        normalize.assert_not_called()
        self.assertFalse(FieldArbitrationDecision.objects.exists())
        value['expected_input_set_hash'] = row['input_set_hash']
        self.command(row, key=key, body=value)

    def test_source_sql_and_canonical_limits_before_normalization(self):
        canonical = sum(fields.bounded(r.result_snapshot) for r in (self.first, self.second))
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
        for name, exact in (('MAX_SNAPSHOT_BYTES', canonical), ('MAX_STORED_BYTES', stored)):
            key = str(uuid4())
            with patch.object(fields, name, exact - 1), patch.object(fields, 'normalize') as n:
                self.dispatch(key=key, expected=422)
            n.assert_not_called()
            with patch.object(fields, name, exact):
                row = self.dispatch(key=key)[0].data
            self.command(row, 'cancel')
