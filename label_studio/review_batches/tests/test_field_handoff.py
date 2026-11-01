from dataclasses import replace
from unittest.mock import patch

from access_control.identity import LocalIdentityProvider
from access_control.project_operations import ProjectOperationPolicy
from classification_disagreements import field_api, fields
from classification_disagreements import field_arbitration as a
from classification_disagreements.models import FieldArbitrationDecision, FieldArbitrationTask, FieldBatch
from django.db import DatabaseError, transaction
from django.db.models import JSONField
from django.utils import timezone
from organizations.models import OrganizationMember
from rest_framework.response import Response
from review_batches import field_handoff as h
from tasks.models import ReviewDecision, Submission, Task


class ClassificationOnlyPolicy:
    def evaluate(self, project):
        return ProjectOperationPolicy(
            'classification-only/v1', 'active', frozenset({'read_work', 'organize_arbitration'})
        )


class FieldHandoffOnlyPolicy:
    def evaluate(self, project):
        return ProjectOperationPolicy(
            'existing-field-handoff/v1', 'active', frozenset({'read_work', 'organize_field_arbitration'})
        )


from uuid import uuid4

from django.test import override_settings
from rest_framework.test import APITestCase
from review_batches.models import FieldReviewArbitrationHandoff, ReviewBatch

from . import test_field_priority as priority_fixtures


class FieldHandoffFixtures:
    setUpTestData = classmethod(priority_fixtures.TestFieldPriority.setUpTestData.__func__)
    make_source = priority_fixtures.TestFieldPriority.make_source
    write = priority_fixtures.TestFieldPriority.write
    freeze_analysis = priority_fixtures.TestFieldPriority.freeze_analysis
    preview = priority_fixtures.TestFieldPriority.preview
    body = priority_fixtures.TestFieldPriority.body
    create = priority_fixtures.TestFieldPriority.create

    def setUp(self):
        priority_fixtures.TestFieldPriority.setUp(self)
        modes = self.settings(ANNOTATION_FIELD_ARBITRATION_MODES={f'{self.org.pk}:{self.project.pk}': 'fork_review'})
        modes.enable()
        self.addCleanup(modes.disable)
        self.args['rate'] = 100
        result = self.create()
        assert result.status_code == 201, result.data
        self.batch = ReviewBatch.objects.get(pk=result.data['id'])
        self.source_id = self.first.pk
        self.handoff_url = f'{self.base}{self.batch.pk}/sources/{self.source_id}/field-arbitration-handoffs/'
        self.context_url = self.handoff_url.replace('field-arbitration-handoffs/', 'field-arbitration-context/')

    def handoff_body(self, target=None):
        result = self.client.get(self.context_url)
        assert result.status_code == 200, getattr(result, 'data', result.content)
        assert result.data['eligible'], result.data
        return {
            'mode': 'link_existing' if target else 'dispatch',
            **{k: result.data[k] for k in ('expected_plan_hash', 'expected_input_set_hash')},
            'expected_viewer_hash': result.data['viewer_hash'],
            **(
                {'field_arbitration_id': target.pk, 'expected_arbitration_version': target.version}
                if target
                else {'assignee_id': self.independent.pk}
            ),
        }

    def handoff(self, body=None, key=None):
        return self.client.post(
            self.handoff_url, body or self.handoff_body(), format='json', HTTP_IDEMPOTENCY_KEY=str(key or uuid4())
        )

    def handoff_detail(self, identifier):
        return self.client.get(f'{self.base}{self.batch.pk}/field-arbitration-handoffs/{identifier}/')

    def refreeze(self):
        self.analysis = self.freeze_analysis()
        self.args['priority'].update(batch_id=self.analysis['id'], manifest_hash=self.analysis['manifest_hash'])
        r = self.create()
        assert r.status_code == 201, r.data
        self.batch = ReviewBatch.objects.get(pk=r.data['id'])
        self.handoff_url = f'{self.base}{self.batch.pk}/sources/{self.first.pk}/field-arbitration-handoffs/'
        self.context_url = self.handoff_url.replace('field-arbitration-handoffs/', 'field-arbitration-context/')

    def standalone_body(self, analysis=None):
        analysis = analysis or self.analysis
        return {
            'field_batch_id': analysis['id'],
            'task_id': self.first.assignment.task_id,
            'expected_manifest_hash': analysis['manifest_hash'],
            'assignee_id': self.independent.pk,
        }

    def standalone(self, value=None, key=None):
        return self.client.post(
            f'/api/projects/{self.project.pk}/field-arbitrations/',
            value or self.standalone_body(),
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(key or uuid4()),
        )

    def arbitrate(self, target, *, cancel=False, expected=201):
        body = {'expected_version': target.version, 'expected_input_set_hash': target.input_set_hash, 'reason': '冻结依据'}
        if not cancel:
            body['winner_submission_id'] = self.first.pk
        r = self.client.post(
            f'/api/projects/{self.project.pk}/field-arbitrations/{target.pk}/{"cancel" if cancel else "decision"}/',
            body,
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        )
        assert r.status_code == expected, r.data
        return r


@override_settings(
    ANNOTATION_REVIEW_BATCHES_ENABLED=True,
    ANNOTATION_FIELD_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_FIELD_BATCHES_ENABLED=True,
    ANNOTATION_FIELD_REVIEW_PRIORITY_ENABLED=True,
    ANNOTATION_FIELD_ARBITRATION_ENABLED=True,
    ANNOTATION_FIELD_REVIEW_ARBITRATION_HANDOFF_ENABLED=True,
)
class TestFieldHandoff(FieldHandoffFixtures, APITestCase):
    def test_enabled_context_is_exact_frozen_field_conflict(self):
        result = self.client.get(self.context_url)
        assert result.status_code == 200, result.data
        assert result.data['kind'] == 'field'
        assert result.data['eligible'] is True
        assert result.data['expected_plan_hash'] == self.batch.plan_hash

    def test_dispatch_exact_cost_and_no_review_mutation(self):
        value, key = self.handoff_body(), uuid4()
        before = list(Submission.objects.order_by('pk').values())
        with patch.object(fields, 'normalize', wraps=fields.normalize) as counter:
            result = self.handoff(value, key)
        assert result.status_code == 201, result.data
        assert counter.call_count == 12
        row = FieldReviewArbitrationHandoff.objects.get()
        child = FieldArbitrationTask.objects.get()
        assert row.field_arbitration_id == child.pk and child.idempotency_key != key
        assert row.origin['review']['source_pin']['assignee_id'] == self.first.assignment.assignee_id
        assert 'assignee_id' not in self.batch.plan['candidates'][0]
        assert result.data['receipt']['key'] == str(key)
        assert not ReviewDecision.objects.exists()
        assert list(Submission.objects.order_by('pk').values()) == before

    def test_receipt_recovery_before_missing_sources_and_tight_processing_caps(self):
        body, key = self.handoff_body(), uuid4()
        saved = self.handoff(body, key)
        assert saved.status_code == 201, saved.data
        FieldBatch.objects.all().delete()
        ReviewBatch.objects.all().delete()
        Submission.objects.filter(pk=self.first.pk).delete()
        with patch.object(h, 'MAX_METADATA', 0), patch.object(a, 'MAX_NORMALIZATIONS', 0), patch.object(
            field_api, 'load', side_effect=AssertionError('receipt must not hydrate sources')
        ), patch.object(fields, 'normalize', side_effect=AssertionError('receipt must not score')):
            recovered = self.handoff(body, key)
        assert recovered.status_code == 200, recovered.data
        assert recovered.data['receipt'] == saved.data['receipt']

    def test_history_is_metadata_not_current_integrity_proof(self):
        result = self.handoff()
        assert result.status_code == 201, result.data
        with patch.object(field_api, 'load', side_effect=AssertionError('no history snapshots')), patch.object(
            fields, 'normalize', side_effect=AssertionError('no history scoring')
        ):
            history = self.client.get(self.handoff_url)
            detail = self.handoff_detail(result.data['id'])
        assert history.status_code == detail.status_code == 200, (history.data, detail.data)
        assert detail.data['source_integrity'] == 'not_checked' and detail.data['decision'] is None
        assert history.data['count'] == 1

    def test_strict_type_viewer_and_route_bound_command(self):
        value, key = self.handoff_body(), uuid4()
        for update in ({'actor_id': self.manager.pk}, {'expected_viewer_hash': '0' * 64}, {'arbitration_id': 1}):
            result = self.handoff({**value, **update}, key)
            assert result.status_code in (400, 409), result.data
        result = self.handoff(value, key)
        assert result.status_code == 201, result.data
        other = self.client.post(
            self.handoff_url.replace(f'/sources/{self.source_id}/', f'/sources/{self.second.pk}/'),
            value,
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(key),
        )
        assert other.status_code == 409, other.data

    def test_link_different_origin_retains_both_provenances_and_exact_cost(self):
        other = self.freeze_analysis()
        result = self.standalone(self.standalone_body(other))
        assert result.status_code == 201, result.data
        target = FieldArbitrationTask.objects.get()
        assert other['id'] != self.analysis['id']
        body = self.handoff_body(target)
        with patch.object(fields, 'normalize', wraps=fields.normalize) as counter:
            result = self.handoff(body)
        assert result.status_code == 201, result.data
        assert counter.call_count == 12 and FieldArbitrationTask.objects.count() == 1
        record = FieldReviewArbitrationHandoff.objects.get()
        assert record.origin['analysis']['batch_id'] == self.analysis['id']
        assert record.origin['target']['batch_id'] == other['id']
        assert self.handoff(body).status_code == 409

    def test_resolved_link_does_not_approve_and_cancel_does_not_redispatch_old_key(self):
        result = self.standalone()
        assert result.status_code == 201, result.data
        target = FieldArbitrationTask.objects.get()
        self.client.force_authenticate(self.independent)
        self.arbitrate(target)
        self.client.force_authenticate(self.manager)
        target.refresh_from_db()
        result = self.handoff(self.handoff_body(target))
        assert result.status_code == 201, result.data
        assert result.data['receipt']['arbitration_status_at_commit'] == 'resolved'
        assert not ReviewDecision.objects.exists()
        self.first.refresh_from_db()
        assert self.first.status == 'pending'

    def test_cancelled_target_requires_new_explicit_parent_command(self):
        value, key = self.handoff_body(), uuid4()
        result = self.handoff(value, key)
        assert result.status_code == 201, result.data
        target = FieldArbitrationTask.objects.get()
        self.arbitrate(target, cancel=True)
        replay = self.handoff(value, key)
        assert replay.status_code == 200 and replay.data['receipt'] == result.data['receipt']
        assert FieldArbitrationTask.objects.count() == 1
        new = self.handoff(value)
        assert new.status_code == 201, new.data
        assert new.data['arbitration_id'] != result.data['arbitration_id']

    def test_target_original_batch_loss_cannot_use_review_batch_as_substitute(self):
        other = self.freeze_analysis()
        r = self.standalone(self.standalone_body(other))
        assert r.status_code == 201, r.data
        target = FieldArbitrationTask.objects.get()
        value = self.handoff_body(target)
        FieldBatch.objects.filter(pk=other['id']).delete()
        assert self.handoff(value).status_code in (404, 409)
        assert not FieldReviewArbitrationHandoff.objects.exists()

    def test_independent_viewer_intersection_before_counts_and_no_answers(self):
        result = self.handoff()
        assert result.status_code == 201, result.data
        self.client.force_authenticate(self.reviewer)
        with patch.object(field_api, 'load', side_effect=AssertionError('Self cohort must not load')):
            hidden = self.client.get(self.handoff_url)
        assert hidden.status_code == 200 and hidden.data['count'] == 0
        assert self.handoff_detail(result.data['id']).status_code == 404
        assert self.client.get(self.context_url).status_code == 404
        self.client.force_authenticate(self.independent)
        visible = self.client.get(self.handoff_url)
        assert visible.status_code == 200 and visible.data['count'] == 1
        self.client.force_authenticate(self.worker)
        assert self.client.get(self.handoff_url).status_code in (403, 404)

    def test_classification_operations_and_core_or_missing_maps_never_grant_field_write(self):
        value, key = self.handoff_body(), uuid4()
        with self.settings(ANNOTATION_PROJECT_OPERATION_PROVIDER=__name__ + '.ClassificationOnlyPolicy'):
            assert self.client.get(self.context_url).data['can_handoff'] is False
            assert self.handoff(value, key).status_code == 403
        for setting, val in (
            ('ANNOTATION_FIELD_REVIEW_ARBITRATION_HANDOFF_ENABLED', False),
            ('ANNOTATION_FIELD_REVIEW_PRIORITY_ENABLED', False),
            ('ANNOTATION_REVIEW_BATCH_MODES', {f'{self.org.pk}:{self.project.pk}': 'core_only_internal'}),
            ('ANNOTATION_FIELD_ARBITRATION_MODES', {}),
            ('ANNOTATION_FIELD_DISAGREEMENT_MODES', {f'{self.org.pk}:{self.project.pk}': 'unknown'}),
        ):
            with self.settings(**{setting: val}):
                assert self.client.get(self.context_url).status_code in (403, 404)
                assert self.client.get(self.handoff_url).status_code in (403, 404)
                assert self.handoff(value, key).status_code in (403, 404)
        assert not FieldArbitrationTask.objects.exists()

    def test_original_owner_provider_source_and_body_are_bound(self):
        value, key = self.handoff_body(), uuid4()
        result = self.handoff(value, key)
        assert result.status_code == 201, result.data
        original = LocalIdentityProvider.resolve

        def different_source(provider, request):
            return replace(original(provider, request), source='another-provider')

        with patch.object(LocalIdentityProvider, 'resolve', different_source):
            assert self.handoff(value, key).status_code == 409
        assert self.handoff({**value, 'expected_plan_hash': '0' * 64}, key).status_code == 409

    def test_real_token_and_revocation_apply_to_recovery_and_reads(self):
        self.org.jwt.legacy_api_tokens_enabled = True
        self.org.jwt.save(update_fields=['legacy_api_tokens_enabled'])
        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.manager.get_token().key}')
        value, key = self.handoff_body(), uuid4()
        result = self.handoff(value, key)
        assert result.status_code == 201, result.data
        OrganizationMember.objects.filter(organization=self.org, user=self.manager).update(deleted_at=timezone.now())
        with patch.object(field_api, 'load', side_effect=AssertionError('Revoked token must not hydrate')):
            assert self.handoff(value, key).status_code in (403, 404)
            assert self.handoff_detail(result.data['id']).status_code in (403, 404)

    def test_render_revocation_source_and_response_change_rollback_both_rows(self):
        value, key = self.handoff_body(), uuid4()
        render = Response.render
        for change in ('source', 'identity', 'representation'):

            def changed(response):
                result = render(response)
                if response.status_code == 201 and response.data.get('schema') == h.SCHEMA:
                    if change == 'source':
                        Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64)
                    elif change == 'identity':
                        OrganizationMember.objects.filter(organization=self.org, user=self.manager).update(
                            deleted_at=timezone.now()
                        )
                    else:
                        response.data['arbitration_id'] += 1
                return result

            with patch.object(Response, 'render', changed):
                assert self.handoff(value, key).status_code in (403, 404, 409)
            assert not FieldArbitrationTask.objects.exists() and not FieldReviewArbitrationHandoff.objects.exists()
        assert self.handoff(value, key).status_code == 201

    def test_last_counter_source_change_is_detected_after_scoring(self):
        value = self.handoff_body()
        original, calls = a.check_engine, []

        def changed(prepared):
            original(prepared)
            calls.append(1)
            if len(calls) == 1:
                Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64)

        with patch.object(a, 'check_engine', changed):
            assert self.handoff(value).status_code == 409
        assert not FieldArbitrationTask.objects.exists() and not FieldReviewArbitrationHandoff.objects.exists()

    def test_budget_failures_never_commit_and_exact_snapshot_boundary_succeeds(self):
        value = self.handoff_body()
        size = sum(fields.bounded(x.result_snapshot, 8 * 1024 * 1024) for x in (self.first, self.second))
        for module, field, limit in (
            (a, 'MAX_NORMALIZATIONS', 1),
            (a, 'MAX_PARTICIPANTS', 1),
            (fields, 'MAX_SNAPSHOT_BYTES', size - 1),
            (h, 'MAX_METADATA', 1),
            (h, 'MAX_STORED_METADATA', 1),
        ):
            with patch.object(module, field, limit):
                r = self.handoff(value)
                assert r.status_code == 422, r.data
            assert not FieldArbitrationTask.objects.exists() and not FieldReviewArbitrationHandoff.objects.exists()
        with patch.object(fields, 'MAX_SNAPSHOT_BYTES', size):
            r = self.handoff(value)
        assert r.status_code == 201, r.data

    def test_sql_immutability_nullable_links_and_no_rebinding(self):
        r = self.handoff()
        assert r.status_code == 201, r.data
        row = FieldReviewArbitrationHandoff.objects.get()
        for field, value in {
            'id': row.pk + 100,
            'creator_source': 'forged',
            'origin': {},
            'origin_hash': '0' * 64,
            'request_hash': '0' * 64,
            'submission_id_frozen': row.submission_id_frozen + 1,
        }.items():
            with self.assertRaises(DatabaseError), transaction.atomic():
                FieldReviewArbitrationHandoff.objects.filter(pk=row.pk).update(**{field: value})
        FieldReviewArbitrationHandoff.objects.filter(pk=row.pk).update(field_arbitration=None)
        with self.assertRaises(DatabaseError), transaction.atomic():
            FieldReviewArbitrationHandoff.objects.filter(pk=row.pk).update(
                field_arbitration_id=row.arbitration_id_frozen
            )
        assert not self.handoff_detail(row.pk).data['available']

    def test_genuine_empty_revision_and_double_empty_pair_cost(self):
        self.write(self.first, ('', '', ''))
        third = self.make_source(worker=self.manager)
        third.assignment.task = self.first.assignment.task
        third.assignment.save()
        self.write(third, ('', '', ''))
        self.refreeze()
        value = self.handoff_body()
        with patch.object(fields, 'normalize', wraps=fields.normalize) as counter:
            r = self.handoff(value)
        assert r.status_code == 201 and counter.call_count == 18, r.data
        target = FieldArbitrationTask.objects.get()
        self.client.force_authenticate(self.independent)
        saved = self.arbitrate(target)
        assert saved.data['winner_submission_id'] == self.first.pk
        assert not ReviewDecision.objects.exists()

    def test_latest_or_reviewed_source_denied_before_snapshots(self):
        value = self.handoff_body()
        self.make_source(worker=self.worker, revision=2, assignment=self.first.assignment)
        with patch.object(field_api, 'load', side_effect=AssertionError('No stale source hydration')):
            r = self.handoff(value)
            context = self.client.get(self.context_url)
        assert r.status_code == 409 and context.status_code == 200 and not context.data['eligible']
        assert not FieldArbitrationTask.objects.exists()

    def test_plain_batch_is_not_upgraded_or_hydrated_as_field(self):
        value = self.handoff_body()
        args = {'name': 'Legacy plain review', 'rate': 100, 'seed': 'legacy'}
        preview = self.client.post(self.base + 'preview/', args, format='json')
        assert preview.status_code == 200, preview.data
        result = self.client.post(
            self.base,
            {**args, 'expected_plan_hash': preview.data['plan_hash']},
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        )
        assert result.status_code == 201, result.data
        path = self.handoff_url.replace(f'/{self.batch.pk}/sources/', f'/{result.data["id"]}/sources/')
        with patch.object(field_api, 'load', side_effect=AssertionError('No legacy answer hydration')):
            context = self.client.get(path.replace('field-arbitration-handoffs/', 'field-arbitration-context/'))
            assert context.status_code == 200 and context.data['eligible'] is False, context.data
            assert context.data['processing_cost']['hydration_passes'] == 0
            r = self.client.post(path, value, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        assert r.status_code == 409 and not FieldArbitrationTask.objects.exists()

    def test_history_page_sql_total_rejects_before_decoding_any_proof(self):
        r = self.handoff()
        assert r.status_code == 201, r.data
        size = h.rows(self.project).values_list('_size_origin', flat=True).get()
        original = JSONField.from_db_value

        def decode(field, value, *args):
            if isinstance(value, str) and h.SCHEMA in value:
                raise AssertionError('Over-budget page proof was decoded')
            return original(field, value, *args)

        with patch.object(h, 'MAX_STORED_METADATA', size - 1), patch.object(JSONField, 'from_db_value', decode):
            result = self.client.get(self.handoff_url)
        assert result.status_code == 422, result.data

    def test_history_target_origin_participants_and_cancellation_share_sql_gate(self):
        r = self.handoff()
        assert r.status_code == 201, r.data
        target = FieldArbitrationTask.objects.get()
        self.arbitrate(target, cancel=True)
        proof_size = h.rows(self.project).values_list('_size_origin', flat=True).get()
        target_size = a.rows(self.project).values_list('_size_origin', flat=True).get()
        original = JSONField.from_db_value

        def decode(field, value, *args):
            if isinstance(value, str) and a.SCHEMA in value:
                raise AssertionError('Target JSON decoded before its whole-row SQL gate')
            return original(field, value, *args)

        with patch.object(h, 'MAX_STORED_METADATA', proof_size + target_size), patch.object(
            JSONField, 'from_db_value', decode
        ):
            result = self.client.get(self.handoff_url)
        assert result.status_code == 422, result.data

    def test_exact_history_metadata_total_and_one_below(self):
        r = self.handoff()
        assert r.status_code == 201, r.data
        measured = []
        original = h.Metadata.add

        def add(budget, *args):
            result = original(budget, *args)
            measured.append((budget.canonical, budget.stored))
            return result

        with patch.object(h.Metadata, 'add', add):
            assert self.client.get(self.handoff_url).status_code == 200
        canonical, stored = max(measured)
        with patch.object(h, 'MAX_METADATA', canonical), patch.object(h, 'MAX_STORED_METADATA', stored):
            result = self.client.get(self.handoff_url)
            assert result.status_code == 200, result.data
        with patch.object(h, 'MAX_METADATA', canonical - 1):
            assert self.client.get(self.handoff_url).status_code == 422
        with patch.object(h, 'MAX_STORED_METADATA', stored - 1):
            assert self.client.get(self.handoff_url).status_code == 422

    def test_history_does_not_project_current_status_from_a_task_moved_out_of_scope(self):
        result = self.handoff()
        assert result.status_code == 201, result.data
        Task.objects.filter(pk=self.first.assignment.task_id).update(project=self.other)
        with patch.object(field_api, 'load', side_effect=AssertionError('History must not hydrate moved task')):
            detail = self.handoff_detail(result.data['id'])
        assert detail.status_code == 200, detail.data
        assert detail.data['source_status'] == 'unavailable'
        assert detail.data['source_integrity'] == 'not_checked'
        assert detail.data['decision'] is None

    def resolved_history(self):
        result = self.handoff()
        assert result.status_code == 201, result.data
        target = FieldArbitrationTask.objects.get()
        self.client.force_authenticate(self.independent)
        self.arbitrate(target)
        self.client.force_authenticate(self.manager)
        return FieldArbitrationDecision.objects.get()

    def test_resolved_history_counts_decision_metadata_in_canonical_budget(self):
        decision = self.resolved_history()
        measured = []
        original = h.Metadata.add

        def add(budget, *args):
            result = original(budget, *args)
            measured.append((budget.canonical, budget.stored))
            return result

        with patch.object(h.Metadata, 'add', add):
            assert self.client.get(self.handoff_url).status_code == 200
        canonical, stored = max(measured)
        # The same bound must include the winner and reason that checked() reads,
        # even though this metadata-only response does not emit either field.
        expected = sum(
            fields.bounded(value, 4 * 1024 * 1024)
            for value in (
                FieldReviewArbitrationHandoff.objects.get().origin,
                FieldArbitrationTask.objects.get().origin,
                FieldArbitrationTask.objects.get().participant_ids,
                None,
                decision.winner,
                decision.reason,
            )
        )
        assert canonical == expected
        with patch.object(h, 'MAX_METADATA', canonical), patch.object(h, 'MAX_STORED_METADATA', stored):
            assert self.client.get(self.handoff_url).status_code == 200
        with patch.object(h, 'MAX_METADATA', canonical - 1):
            assert self.client.get(self.handoff_url).status_code == 422

    def test_resolved_history_sql_total_rejects_before_decoding_decision(self):
        self.resolved_history()
        proof = h.rows(self.project).values_list('_size_origin', flat=True).get()
        target = a.rows(self.project).values('_size_origin', '_size_participant_ids', '_size_cancellation').get()
        decision = a.decisions(self.project).values('_size_winner', '_size_reason').get()
        stored = proof + sum(value or 0 for value in target.values()) + sum(decision.values())
        original = JSONField.from_db_value

        def decode(field, value, *args):
            if isinstance(value, str) and 'submission_id' in value and 'schema' not in value:
                raise AssertionError('Decision decoded before cumulative SQL budget')
            return original(field, value, *args)

        with patch.object(h, 'MAX_STORED_METADATA', stored - 1), patch.object(JSONField, 'from_db_value', decode):
            result = self.client.get(self.handoff_url)
        assert result.status_code == 422, result.data
