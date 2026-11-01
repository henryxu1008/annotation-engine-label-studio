"""Field-specific contracts beyond the reused typed handoff regressions."""

import copy
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from access_control.identity import LocalIdentityProvider
from classification_disagreements import field_api, field_batches, fields
from classification_disagreements import field_arbitration as a
from classification_disagreements.models import FieldArbitrationTask, FieldBatch
from django.db import DatabaseError, transaction
from django.db.models import JSONField
from django.test import override_settings
from projects.models import ProjectMember
from rest_framework.response import Response
from rest_framework.test import APITestCase
from review_batches import field_handoff as h
from review_batches.models import FieldReviewArbitrationHandoff
from tasks.models import Submission
from tasks.submissions import _snapshot_hash, review_submission
from users.models import User

from .test_field_handoff import FieldHandoffFixtures


@override_settings(
    ANNOTATION_REVIEW_BATCHES_ENABLED=True,
    ANNOTATION_FIELD_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_FIELD_BATCHES_ENABLED=True,
    ANNOTATION_FIELD_REVIEW_PRIORITY_ENABLED=True,
    ANNOTATION_FIELD_ARBITRATION_ENABLED=True,
    ANNOTATION_FIELD_REVIEW_ARBITRATION_HANDOFF_ENABLED=True,
)
class TestFieldHandoffAcceptance(FieldHandoffFixtures, APITestCase):
    def provenance(self, identifier):
        return self.client.get(f'{self.base}{self.batch.pk}/field-arbitration-handoffs/{identifier}/provenance/')

    def linked(self):
        other = self.freeze_analysis()
        result = self.standalone(self.standalone_body(other))
        assert result.status_code == 201, result.data
        target = FieldArbitrationTask.objects.get()
        response = self.handoff(self.handoff_body(target))
        assert response.status_code == 201, response.data
        return response, other, target

    def test_read_and_write_existing_evidence_do_not_borrow_planning_or_adoption_rights(self):
        value = self.handoff_body()
        provider = 'review_batches.tests.test_field_handoff.FieldHandoffOnlyPolicy'
        with self.settings(ANNOTATION_PROJECT_OPERATION_PROVIDER=provider):
            result = self.handoff(value)
            assert result.status_code == 201, result.data
            assert self.provenance(result.data['id']).status_code == 200
            row = FieldArbitrationTask.objects.get()
            self.arbitrate(row, expected=404)

    def test_every_required_gate_denies_before_snapshot_decode(self):
        value = self.handoff_body()
        for name in (
            'ANNOTATION_REVIEW_BATCHES_ENABLED',
            'ANNOTATION_FIELD_DISAGREEMENTS_ENABLED',
            'ANNOTATION_FIELD_BATCHES_ENABLED',
            'ANNOTATION_FIELD_REVIEW_PRIORITY_ENABLED',
            'ANNOTATION_FIELD_ARBITRATION_ENABLED',
            'ANNOTATION_FIELD_REVIEW_ARBITRATION_HANDOFF_ENABLED',
        ):
            with self.settings(**{name: False}), patch.object(field_api, 'load', side_effect=AssertionError(name)):
                assert self.handoff(value).status_code in (403, 404)
                assert self.client.get(self.handoff_url).status_code in (403, 404)

    def test_ineligible_preflight_has_zero_real_work_and_available_context_has_two_passes(self):
        self.write(self.second)
        self.refreeze()
        with patch.object(field_api, 'load', side_effect=AssertionError('No ineligible snapshot reads')), patch.object(
            fields, 'normalize', side_effect=AssertionError('No ineligible normalization')
        ), patch.object(fields, 'equal', side_effect=AssertionError('No ineligible equality')):
            response = self.client.get(self.context_url)
        assert response.status_code == 200, response.data
        assert not response.data['eligible']
        assert response.data['processing_cost'] == a.processing({})

    def test_context_mid_hydration_source_loss_is_an_error_with_observable_partial_work(self):
        Submission.objects.filter(pk=self.second.pk).delete()
        with patch.object(field_api, 'load', wraps=field_api.load) as hydrate, patch.object(
            fields, 'normalize', wraps=fields.normalize
        ) as normalize:
            response = self.client.get(self.context_url)
        assert response.status_code == 409, response.data
        assert hydrate.call_count == 1 and normalize.call_count == 0
        assert 'eligible' not in response.data

    def test_format_only_conflict_and_complete_nine_field_pin_remain_authoritative(self):
        self.write(self.first, (' x\r\ny ', ' city ', ' 001 '))
        self.write(self.second, ('x\ny', 'city', '001'))
        self.refreeze()
        response = self.handoff()
        assert response.status_code == 201, response.data
        row = FieldReviewArbitrationHandoff.objects.get()
        assert set(row.origin['review']['source_pin']) == set(field_api.PIN_IDS + field_api.PIN_HASHES)
        result = FieldBatch.objects.get(pk=self.analysis['id']).manifest['tasks'][0]['result']
        assert result['has_format_difference'] and not result['has_value_difference']
        assert row.origin['analysis']['queue_rule'] == 'field-raw-difference-queue/v1'

    def test_history_provenance_is_metadata_only_but_validates_both_original_batches(self):
        response, other, target = self.linked()
        with patch.object(field_api, 'load', side_effect=AssertionError('No provenance snapshots')), patch.object(
            fields, 'normalize', side_effect=AssertionError('No provenance score')
        ):
            proof = self.provenance(response.data['id'])
            assert proof.status_code == 200, proof.data
            assert proof.data['provenance_status'] == 'verified'
            assert proof.data['snapshot_integrity'] == 'not_checked'
            assert proof.data['target']['task_evidence_hash'] == fields.digest(
                {'schema': a.SCHEMA, 'item': target.origin['item']}
            )
            FieldBatch.objects.filter(pk=self.analysis['id']).delete()
            lost = self.provenance(response.data['id'])
        assert lost.status_code == 200 and lost.data['provenance_status'] == 'unavailable', lost.data
        assert lost.data['target'] is None
        # B is still independently valid; it cannot repair A for this handoff.
        standalone = self.client.get(f'/api/projects/{self.project.pk}/field-arbitrations/{target.pk}/')
        assert standalone.status_code == 200 and standalone.data['source_status'] == 'verified'
        assert FieldBatch.objects.filter(pk=other['id']).exists()

    def test_provenance_does_not_apply_fresh_latest_pending_to_legal_history(self):
        response = self.handoff()
        assert response.status_code == 201, response.data
        review_submission(submission=self.first, reviewer=self.independent, decision='approved')
        self.make_source(worker=self.worker, revision=2, assignment=self.first.assignment)
        with patch.object(field_api, 'load', side_effect=AssertionError('No history snapshots')):
            proof = self.provenance(response.data['id'])
        assert proof.status_code == 200 and proof.data['provenance_status'] == 'verified', proof.data
        fresh = self.client.get(self.context_url)
        assert fresh.status_code == 200 and not fresh.data['eligible']

    def test_provenance_does_not_replace_lost_target_origin_with_intact_review_origin(self):
        response, other, _ = self.linked()
        FieldBatch.objects.filter(pk=other['id']).delete()
        assert FieldBatch.objects.filter(pk=self.analysis['id']).exists()
        proof = self.provenance(response.data['id'])
        assert proof.status_code == 200 and proof.data['provenance_status'] == 'unavailable', proof.data

    def test_reviewer_history_and_provenance_do_not_expand_sensitive_metadata(self):
        response = self.handoff()
        assert response.status_code == 201, response.data
        self.client.force_authenticate(self.independent)
        for value in (self.handoff_detail(response.data['id']), self.provenance(response.data['id'])):
            assert value.status_code == 200, value.data
            text = str(value.data)
            for forbidden in (
                'origin_hash',
                'manifest_hash',
                'participant_ids',
                'principal',
                'creator_source',
                'cohort',
            ):
                assert forbidden not in text
            assert value.data['target'] is not None

    def test_original_key_recovery_never_instantiates_fresh_metadata_or_provenance(self):
        value, key = self.handoff_body(), uuid4()
        created = self.handoff(value, key)
        assert created.status_code == 201, created.data
        with patch.object(h, 'Metadata', side_effect=AssertionError('No fresh metadata')), patch.object(
            h, 'provenance', side_effect=AssertionError('No live provenance')
        ), patch.object(h, 'target', side_effect=AssertionError('No live target')):
            result = self.handoff(value, key)
        assert result.status_code == 200 and result.data['receipt'] == created.data['receipt']
        assert result.data['processing_cost'] == a.processing({})

    def test_same_owner_old_viewer_recovers_before_fresh_viewer_check(self):
        value, key = self.handoff_body(), uuid4()
        created = self.handoff(value, key)
        assert created.status_code == 201, created.data
        with self.settings(SECRET_KEY='rotated-synthetic-key'):
            recovered = self.handoff(value, key)
            fresh = self.handoff(value)
        assert recovered.status_code == 200, recovered.data
        assert recovered.data['receipt'] == created.data['receipt']
        assert recovered.data['viewer_hash'] != created.data['viewer_hash']
        assert fresh.status_code == 409

    def test_stable_owner_capability_survives_only_viewer_salt_rotation(self):
        path = f'/api/projects/{self.project.pk}/field-arbitrations/capability/'
        first = self.client.get(path)
        assert first.status_code == 200 and first.data['can_handoff'] is True
        with self.settings(SECRET_KEY='rotated-synthetic-key'), patch.object(
            field_api, 'load', side_effect=AssertionError('Capability must not hydrate')
        ):
            same = self.client.get(path)
        assert same.data['handoff_owner_hash'] == first.data['handoff_owner_hash']
        assert same.data['viewer_hash'] != first.data['viewer_hash']
        original = LocalIdentityProvider.resolve

        def different_source(provider, request):
            return replace(original(provider, request), source='another-provider')

        with patch.object(LocalIdentityProvider, 'resolve', different_source):
            other = self.client.get(path)
        assert other.status_code == 200
        assert other.data['handoff_owner_hash'] != first.data['handoff_owner_hash']
        with self.settings(ANNOTATION_FIELD_REVIEW_ARBITRATION_HANDOFF_ENABLED=False):
            disabled = self.client.get(path)
        assert 'handoff_owner_hash' not in disabled.data and 'can_handoff' not in disabled.data

    def test_fixed_local_receipt_rejects_rehashed_bad_ids_pins_versions_and_bindings(self):
        result = self.handoff()
        assert result.status_code == 201, result.data
        for section, key, value in (
            (None, 'project_id', True),
            (None, 'organization_id', float(self.org.pk)),
            ('analysis', 'task_id', True),
            ('analysis', 'queue_rule', 'invented/v1'),
            ('target', 'id', float(result.data['arbitration_id'])),
            ('target', 'status_at_link', 'cancelled'),
        ):
            row = FieldReviewArbitrationHandoff.objects.get()
            (row.origin if section is None else row.origin[section])[key] = value
            row.origin_hash = fields.digest(row.origin)
            with self.assertRaises(fields.Error):
                h.checked(row, self.project)
        for name in field_api.PIN_IDS + field_api.PIN_HASHES:
            row = FieldReviewArbitrationHandoff.objects.get()
            row.origin['review']['source_pin'][name] = True if name in field_api.PIN_IDS else 'bad'
            row.origin_hash = fields.digest(row.origin)
            with self.assertRaises(fields.Error):
                h.checked(row, self.project)

    def test_typed_body_rejects_actor_boolean_ids_extra_keys_and_unknown_queries_before_snapshots(self):
        value = self.handoff_body()
        for update in (
            {'assignee_id': True},
            {'assignee_id': float(self.independent.pk)},
            {'assignee_id': str(self.independent.pk)},
            {'assignee_id': 2**53},
            {'winner_submission_id': self.first.pk},
            {'provider': 'forged'},
            {'mode': 'auto'},
        ):
            with patch.object(field_api, 'load', side_effect=AssertionError('Invalid input must not hydrate')):
                assert self.handoff({**value, **update}).status_code == 400
        assert self.client.get(self.context_url + '?actor=1').status_code == 400
        assert self.client.get(self.handoff_url + '?page=1&page=2').status_code == 400

    def test_cached_manifest_is_not_redecoded_by_verified_helper_or_final_replay(self):
        value = self.handoff_body()
        reads = []
        original = JSONField.from_db_value

        def decode(field, value, *args):
            result = original(field, value, *args)
            if isinstance(result, dict) and result.get('schema') == field_batches.SCHEMA:
                reads.append(result['name'])
            return result

        with patch.object(JSONField, 'from_db_value', decode):
            response = self.handoff(value)
        assert response.status_code == 201, response.data
        assert len(reads) == 2  # One full Manifest in each materialization, no helper reload.

    def test_twenty_people_twenty_fields_real_800_normalizations_15200_equalities(self):
        names = [f'f{i}' for i in range(20)]
        xml = (
            '<View><Text name="doc" value="$text"/>'
            + ''.join(f'<TextArea name="{name}" toName="doc" maxSubmissions="1"/>' for name in names)
            + '</View>'
        )
        self.project.label_config = xml
        self.project.save()
        rows = [self.first, self.second]
        for index in range(18):
            actor = User.objects.create_user(
                f'field-handoff-boundary-{index}@example.test', active_organization=self.org
            )
            self.org.add_user(actor)
            ProjectMember.objects.create(project=self.project, user=actor, role='annotator')
            row = self.make_source(worker=actor)
            row.assignment.task = self.first.assignment.task
            row.assignment.save()
            rows.append(row)
        for index, row in enumerate(rows):
            snapshot = copy.deepcopy(self.first.result_snapshot)
            snapshot['project']['label_config'] = xml
            snapshot['annotation']['result'] = [
                {
                    'id': name,
                    'type': 'textarea',
                    'from_name': name,
                    'to_name': 'doc',
                    'value': {'text': ['different' if index == 1 and name == 'f0' else 'same']},
                }
                for name in names
            ]
            row.result_snapshot, row.result_hash = snapshot, _snapshot_hash(snapshot)
            Submission.objects.filter(pk=row.pk).update(result_snapshot=snapshot, result_hash=row.result_hash)
        self.refreeze()
        value, key = self.handoff_body(), uuid4()
        with patch.object(fields, 'normalize', wraps=fields.normalize) as normalize, patch.object(
            fields, 'equal', wraps=fields.equal
        ) as equality, patch.object(field_api, 'load', wraps=field_api.load) as hydrate:
            response = self.handoff(value, key)
        assert response.status_code == 201, response.data
        assert (normalize.call_count, equality.call_count, hydrate.call_count) == (800, 15200, 2)
        assert response.data['processing_cost']['total'] == {'normalized_values': 800, 'field_equalities': 15200}
        with patch.object(a, 'MAX_NORMALIZATIONS', 399):
            assert self.handoff(value).status_code == 422
            assert self.handoff(value, key).status_code == 200

    def test_rendered_output_overflow_rolls_back_both_rows_after_real_work(self):
        value, key = self.handoff_body(), uuid4()
        render = Response.render

        def oversized(response):
            if response.status_code == 201 and response.data.get('schema') == h.SCHEMA:
                response.data['padding'] = 'x' * (1024 * 1024)
            return render(response)

        with patch.object(Response, 'render', oversized), patch.object(
            fields, 'normalize', wraps=fields.normalize
        ) as normalize:
            response = self.handoff(value, key)
        assert response.status_code == 422, response.data
        assert normalize.call_count == 6  # The first pass already happened; never claim zero.
        assert not FieldArbitrationTask.objects.exists() and not FieldReviewArbitrationHandoff.objects.exists()
        assert self.handoff(value, key).status_code == 201

    def small_cohort_cost(self, inputs, count):
        names = [f'field{i}' for i in range(count)]
        xml = (
            '<View><Text name="doc" value="$text"/>'
            + ''.join(f'<TextArea name="{name}" toName="doc" maxSubmissions="1"/>' for name in names)
            + '</View>'
        )
        self.project.label_config = xml
        self.project.save()
        rows = [self.first, self.second]
        if inputs == 3:
            actor = User.objects.create_user('field-small-third@example.test', active_organization=self.org)
            self.org.add_user(actor)
            ProjectMember.objects.create(project=self.project, user=actor, role='annotator')
            third = self.make_source(worker=actor)
            third.assignment.task = self.first.assignment.task
            third.assignment.save()
            rows.append(third)
        for index, row in enumerate(rows):
            snapshot = copy.deepcopy(self.first.result_snapshot)
            snapshot['project']['label_config'] = xml
            snapshot['annotation']['result'] = [
                {
                    'id': name,
                    'type': 'textarea',
                    'from_name': name,
                    'to_name': 'doc',
                    'value': {'text': [f'value-{index}' if name == names[0] else 'same']},
                }
                for name in names
            ]
            row.result_snapshot, row.result_hash = snapshot, _snapshot_hash(snapshot)
            Submission.objects.filter(pk=row.pk).update(result_snapshot=snapshot, result_hash=row.result_hash)
        self.refreeze()
        for operation in ('context', 'dispatch'):
            body = self.handoff_body() if operation == 'dispatch' else None
            with patch.object(fields, 'normalize', wraps=fields.normalize) as normalize, patch.object(
                fields, 'equal', wraps=fields.equal
            ) as equal, patch.object(field_api, 'load', wraps=field_api.load) as hydrate:
                response = self.handoff(body) if body else self.client.get(self.context_url)
            assert response.status_code == (201 if body else 200), response.data
            expected = {'normalized_values': 2 * inputs * count, 'field_equalities': 2 * inputs * (inputs - 1) * count}
            assert response.data['processing_cost']['total'] == expected
            assert (normalize.call_count, equal.call_count, hydrate.call_count) == (
                expected['normalized_values'],
                expected['field_equalities'],
                2,
            )

    def test_two_people_one_field_actual_context_and_dispatch_work(self):
        self.small_cohort_cost(2, 1)

    def test_three_people_two_fields_actual_context_and_dispatch_work(self):
        self.small_cohort_cost(3, 2)

    def test_every_frozen_ledger_column_and_nullable_rebinding_is_guarded_in_sql(self):
        response = self.handoff()
        assert response.status_code == 201, response.data
        row = FieldReviewArbitrationHandoff.objects.get()
        values = {
            'id': row.pk + 100,
            'project_id': self.other.pk,
            'review_batch_id_frozen': row.review_batch_id_frozen + 1,
            'submission_id_frozen': row.submission_id_frozen + 1,
            'arbitration_id_frozen': row.arbitration_id_frozen + 1,
            'actor_id_frozen': self.independent.pk,
            'creator_principal': 'forged',
            'creator_source': 'forged',
            'created_at': row.created_at.replace(year=2025),
            'origin': {},
            'origin_hash': '0' * 64,
            'idempotency_key': uuid4(),
            'request_hash': '0' * 64,
        }
        for name, value in values.items():
            with self.assertRaises(DatabaseError), transaction.atomic():
                FieldReviewArbitrationHandoff.objects.filter(pk=row.pk).update(**{name: value})
        for name, value in (
            ('review_batch_id', row.review_batch_id),
            ('field_arbitration_id', row.field_arbitration_id),
            ('created_by_id', row.created_by_id),
        ):
            FieldReviewArbitrationHandoff.objects.filter(pk=row.pk).update(**{name: None})
            with self.assertRaises(DatabaseError), transaction.atomic():
                FieldReviewArbitrationHandoff.objects.filter(pk=row.pk).update(**{name: value})
