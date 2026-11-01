"""Explicit remaining review findings: short streams, dynamic authority and exact budgets."""
import copy
import importlib
import io
import json
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from access_control.identity import LocalIdentityProvider
from django.db import DatabaseError, OperationalError, connection, transaction
from rest_framework.exceptions import APIException
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APITestCase
from review_batches import confidence_contract as c
from review_batches import confidence_observations as o
from review_batches.confidence_api import ConfidenceJSONParser
from review_batches.models import ReviewBatch
from review_batches.models import ReviewConfidenceObservation as Fact
from tasks.models import ReviewDecision, Submission, TaskAssignment
from tasks.submissions import _snapshot_hash

from .confidence_fixtures import INSTANCE, NAMESPACE, PROFILE, ConfidenceFixtures


class ShortStream:
    def __init__(self, value, size=3):
        self.value, self.size = io.BytesIO(value), size

    def read(self, size):
        return self.value.read(min(size, self.size))


class TestConfidenceFinalEdges(ConfidenceFixtures, APITestCase):
    def setUp(self):
        self.confidence_setup()
        self.source = self.make_source()

    def test_linked_fact_corruption_is_not_masked_by_unsupported_or_reassignment(self):
        assert self.publish(self.source).status_code == 201
        original = copy.deepcopy(self.source.result_snapshot)
        for mutation in ('unsupported', 'reassigned', 'missing_xml', 'missing_data'):
            snapshot = copy.deepcopy(original)
            if mutation == 'unsupported':
                snapshot['project']['label_config'] = snapshot['project']['label_config'].replace(
                    '<View>', '<View style="background:red">'
                )
            elif mutation == 'reassigned':
                snapshot['task']['data']['text'] = 'Coherently rehashed tampering'
            elif mutation == 'missing_xml':
                snapshot['project'].pop('label_config')
            else:
                snapshot['task'].pop('data')
            Submission.objects.filter(pk=self.source.pk).update(
                result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
            )
            TaskAssignment.objects.filter(pk=self.source.assignment_id).update(
                assignee=self.second_worker if mutation == 'reassigned' else self.worker
            )
            result = self.preview()
            assert result.status_code == 409, (mutation, result.data)
            assert not ReviewBatch.objects.exists()

    def test_linked_fact_pure_reassignment_or_submitter_loss_remains_unknown(self):
        assert self.publish(self.source).status_code == 201
        for reassigned in (True, False):
            TaskAssignment.objects.filter(pk=self.source.assignment_id).update(
                assignee=self.second_worker if reassigned else self.worker
            )
            Submission.objects.filter(pk=self.source.pk).update(submitted_by=self.worker if reassigned else None)
            result = self.preview()
            assert result.status_code == 200, result.data
            assert result.data['plan']['priority']['evidence'][0]['reason'] == 'revision_binding_changed'
        Submission.objects.filter(pk=self.source.pk).update(submitted_by=self.second_worker)
        assert self.preview().status_code == 409

    def test_duplicate_fact_input_is_rejected_instead_of_selecting_first(self):
        assert self.publish(self.source).status_code == 201
        original = o.rows

        def duplicate(*args, **kwargs):
            rows = original(*args, **kwargs)
            return rows + rows

        with patch.object(o, 'rows', duplicate):
            assert self.preview().status_code == 409

    def test_known_foreign_submission_and_unscoped_global_admin_cannot_decode_sources(self):
        from projects.tests.factories import ProjectFactory
        from users.tests.factories import UserFactory

        body = self.fact_body(self.source)
        original_project = self.project
        self.project = ProjectFactory(
            organization=self.org, created_by=self.manager, label_config=original_project.label_config
        )
        foreign = self.make_source()
        self.project = original_project
        admin = UserFactory(active_organization=self.org, is_superuser=True, is_staff=True)
        self.org.add_user(admin)
        with patch.object(c, 'load', side_effect=AssertionError('unauthorized source decoded')):
            # Foreign and absent IDs share the same metadata-only conflict.
            assert self.source_input(foreign).status_code == 409
            assert self.publish(body={**body, 'submission_id': foreign.pk}).status_code == 409
            assert self.source_input(self.source, actor=admin).status_code in (403, 404)
            assert self.publish(body=body, actor=admin).status_code in (403, 404)

    def test_operational_timeout_or_connection_loss_is_never_a_successful_receipt(self):
        body = self.fact_body(self.source)
        self.client.raise_request_exception = False
        for message in ('statement timeout', 'connection closed unexpectedly'):
            with patch.object(Fact.objects, 'create', side_effect=OperationalError(message)):
                result = self.publish(body=body)
            assert result.status_code == 500 and not Fact.objects.exists()

    def test_valid_mixed_ordinary_classification_ner_field_confidence_page_flags_and_union_budgets(self):
        from access_control.project_operations import (
            ORGANIZE_CLASSIFICATION,
            ORGANIZE_FIELD,
            ORGANIZE_NER,
            ORGANIZE_REVIEW,
            PROVIDE_REVIEW_CONFIDENCE,
            READ_WORK,
            REVIEW,
            SUBMIT,
        )
        from classification_disagreements.tests import test_field_metrics as field
        from classification_disagreements.tests import test_ner_metrics as ner
        from django.db.models import JSONField
        from review_batches import guarded

        modes = {f'{self.org.pk}:{self.project.pk}': 'fork_review'}
        with self.settings(
            ANNOTATION_FIELD_DISAGREEMENTS_ENABLED=True,
            ANNOTATION_FIELD_BATCHES_ENABLED=True,
            ANNOTATION_FIELD_REVIEW_PRIORITY_ENABLED=True,
            ANNOTATION_FIELD_DISAGREEMENT_MODES=modes,
            ANNOTATION_NER_DISAGREEMENTS_ENABLED=True,
            ANNOTATION_NER_BATCHES_ENABLED=True,
            ANNOTATION_NER_REVIEW_PRIORITY_ENABLED=True,
            ANNOTATION_NER_DISAGREEMENT_MODES=modes,
            ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=True,
            ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=True,
            ANNOTATION_CLASSIFICATION_DISAGREEMENT_MODES=modes,
            CONFIDENCE_TEST_OPERATIONS=[
                READ_WORK,
                REVIEW,
                SUBMIT,
                ORGANIZE_REVIEW,
                PROVIDE_REVIEW_CONFIDENCE,
                ORGANIZE_FIELD,
                ORGANIZE_NER,
                ORGANIZE_CLASSIFICATION,
            ],
        ):
            confidence = self.create_batch()
            assert confidence.status_code == 201, confidence.data
            args = {k: v for k, v in self.args.items() if k != 'priority'}
            preview = self.preview(args)
            ordinary = self.create_batch({**args, 'expected_plan_hash': preview.data['plan_hash']})
            assert ordinary.status_code == 201, ordinary.data
            for kind, snapshots in (
                ('field', (field.snapshot(), field.snapshot(('张三', '北京', '1')))),
                ('ner', (ner.snapshot([(0, 5, 'PER')]), ner.snapshot([(0, 4, 'PER')]))),
                (
                    'classification',
                    (copy.deepcopy(self.source.result_snapshot), copy.deepcopy(self.source.result_snapshot)),
                ),
            ):
                first, second = self.make_source(), self.make_source(worker=self.second_worker)
                TaskAssignment.objects.filter(pk=second.assignment_id).update(task=first.assignment.task)
                for row, snapshot in zip((first, second), snapshots):
                    snapshot['project']['id'] = self.project.pk
                    snapshot['task']['id'] = first.assignment.task_id
                    Submission.objects.filter(pk=row.pk).update(
                        result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
                    )
                path = f'/api/projects/{self.project.pk}/{kind}-disagreements/batches/'
                args = {'name': kind, 'scope': {'kind': 'tasks', 'task_ids': [first.assignment.task_id]}}
                preview = self.client.post(path + 'preview/', args, format='json')
                assert preview.status_code == 200, preview.data
                frozen = self.client.post(
                    path,
                    {'preview_token': preview.data['preview_token'], 'expected_plan_hash': preview.data['plan_hash']},
                    format='json',
                    HTTP_IDEMPOTENCY_KEY=str(uuid4()),
                )
                assert frozen.status_code == 201, frozen.data
                args = {
                    **self.args,
                    'priority': {
                        'kind': kind + '_conflict',
                        'batch_id': frozen.data['id'],
                        'manifest_hash': frozen.data['manifest_hash'],
                    },
                }
                preview = self.preview(args)
                assert preview.status_code == 200, preview.data
                created = self.create_batch({**args, 'expected_plan_hash': preview.data['plan_hash']})
                assert created.status_code == 201, created.data
            listed = self.client.get(self.base)
            assert listed.status_code == 200 and listed.data['count'] == 5, listed.data
            assert listed.data['schema'] == c.SCHEMA
            for row in listed.data['results']['batches']:
                assert self.client.get(self.base + f'{row["id"]}/').status_code == 200
            with self.settings(ANNOTATION_REVIEW_CONFIDENCE_ENABLED=False):
                listed = self.client.get(self.base)
                assert listed.status_code == 200 and listed.data['count'] == 4, listed.data
                assert 'schema' not in listed.data
            stored = sum(guarded.query(self.project).values_list('_ner_plan_size', flat=True))
            original, decoded = JSONField.from_db_value, []

            def decode(field, value, expression, db):
                if value and 'algorithm' in str(value):
                    decoded.append(value)
                return original(field, value, expression, db)

            with patch.object(c, 'MAX_METADATA_STORED', stored - 1), patch.object(JSONField, 'from_db_value', decode):
                assert self.client.get(self.base).status_code == 422
            assert not decoded
            canonical = sum(
                len(json.dumps(b.plan, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8'))
                for b in ReviewBatch.objects.all()
            )
            with patch.object(c, 'MAX_METADATA', canonical - 1):
                assert self.client.get(self.base).status_code == 422

    def test_short_read_chunked_stream_is_complete_and_bounded_before_decoder(self):
        parser = ConfidenceJSONParser()
        value = b'{"score_units":123,"payload":"chunked"}'
        assert parser.parse(ShortStream(value)) == json.loads(value)
        with patch('review_batches.confidence_api.json.load', side_effect=AssertionError('No oversized decode')):
            with self.assertRaises(APIException) as raised:
                parser.parse(ShortStream(b'{}' + b' ' * c.MAX_BODY, 2))
            assert raised.exception.status_code == 422
        with self.assertRaises(APIException) as raised:
            parser.parse(ShortStream(value + b'garbage'))
        assert raised.exception.status_code == 400
        assert parser.parse(ShortStream(b'{}' + b' ' * (c.MAX_BODY - 2), 37)) == {}

    def test_getter_stale_after_review_is_rejected_but_original_event_recovers(self):
        body, key = self.fact_body(self.source), uuid4()
        assert self.publish(body=body, key=key).status_code == 201
        ReviewDecision.objects.create(submission=self.source, reviewer=self.reviewer, decision='approved')
        Submission.objects.filter(pk=self.source.pk).update(status='approved')
        assert self.source_input(self.source).status_code == 409
        assert self.publish(body=body).status_code == 409
        with patch.object(c, 'load', side_effect=AssertionError('source-free event')):
            assert self.publish(body=body, key=key).status_code == 200

    def test_unicode_code_point_seed_boundary_uses_full_utf8_request_hash(self):
        args = {**self.args, 'seed': '😀' * 128, 'per_submitter': False, 'package_ids': [], 'submitter_ids': []}
        preview = self.preview(args)
        assert preview.status_code == 200, preview.data
        body = {**args, 'expected_plan_hash': preview.data['plan_hash']}
        result = self.create_batch(body)
        assert result.status_code == 201 and result.data['request_hash'] == c.digest(body), result.data
        assert self.preview({**args, 'seed': args['seed'] + '😀'}).status_code == 400

    def test_last_candidate_reviewed_before_create_is_changed_not_permanent_unknown(self):
        preview = self.preview()
        body = {**self.args, 'expected_plan_hash': preview.data['plan_hash']}
        self.client.force_authenticate(self.reviewer)
        result = self.client.post(
            f'/api/submissions/{self.source.pk}/review/', {'decision': 'approved'}, format='json'
        )
        assert result.status_code == 200, result.data
        result = self.create_batch(body)
        assert (
            result.status_code == 409 and result.data['display_context']['reason'] == 'REVIEW_BATCH_PREVIEW_CHANGED'
        ), result.data
        assert not ReviewBatch.objects.exists()
        result = self.preview()
        assert result.data['display_context']['reason'] == 'REVIEW_BATCH_EMPTY'

    def test_post_render_retirement_or_digest_change_rolls_back_producer_and_batch(self):
        body = self.fact_body(self.source)
        preview = self.preview()
        batch_body = {**self.args, 'expected_plan_hash': preview.data['plan_hash']}
        render = JSONRenderer.render
        for producer in (True, False):
            for change in ('retired', 'digest'):
                original = copy.deepcopy(self.entries[0])

                def rendering(renderer, data, *args, **kwargs):
                    result = render(renderer, data, *args, **kwargs)
                    if isinstance(data, dict) and data.get('schema') == (c.RECEIPT if producer else c.SCHEMA):
                        if change == 'retired':
                            self.entries[0]['fresh'] = False
                        else:
                            descriptor = {**self.descriptor, 'model_artifact_sha256': 'b' * 64}
                            self.entries[0].update(descriptor=descriptor, digest=c.digest(descriptor))
                    return result

                with patch.object(JSONRenderer, 'render', rendering):
                    result = self.publish(body=body) if producer else self.create_batch(batch_body)
                self.entries[0] = original
                assert result.status_code == 403, result.data
                assert not Fact.objects.exists() and not ReviewBatch.objects.exists()

    def test_post_render_actor_or_provider_switch_rolls_back_both_paths(self):
        body = self.fact_body(self.source)
        preview = self.preview()
        batch_body = {**self.args, 'expected_plan_hash': preview.data['plan_hash']}
        render, resolve = JSONRenderer.render, LocalIdentityProvider.resolve
        for producer in (True, False):
            for field in ('actor', 'source'):
                changed = []

                def identity(provider, request):
                    principal = resolve(provider, request)
                    if not changed:
                        return principal
                    return (
                        replace(principal, source='another-provider')
                        if field == 'source'
                        else replace(principal, local_user_id=self.manager.pk if producer else self.producer.pk)
                    )

                def rendering(renderer, data, *args, **kwargs):
                    value = render(renderer, data, *args, **kwargs)
                    if isinstance(data, dict) and data.get('schema') == (c.RECEIPT if producer else c.SCHEMA):
                        changed.append(True)
                    return value

                with patch.object(LocalIdentityProvider, 'resolve', identity), patch.object(
                    JSONRenderer, 'render', rendering
                ):
                    result = self.publish(body=body) if producer else self.create_batch(batch_body)
                assert result.status_code == 403, result.data
                assert not Fact.objects.exists() and not ReviewBatch.objects.exists()

    def test_rendered_input_score_plan_and_ack_mutations_fail_closed(self):
        render = JSONRenderer.render
        for mode in ('input', 'plan', 'producer_ack', 'batch_ack'):
            body = self.fact_body(self.source)
            preview = self.preview()
            batch_body = {**self.args, 'expected_plan_hash': preview.data['plan_hash']}

            def rendering(renderer, data, *args, **kwargs):
                if isinstance(data, dict):
                    if mode == 'input' and data.get('schema') == c.INPUT:
                        data['score_units'] = 0
                    if mode == 'plan' and 'plan' in data and data.get('schema') == c.SCHEMA:
                        data['plan']['priority']['evidence'][0]['score_units'] = 0
                    if mode == 'producer_ack' and data.get('schema') == c.RECEIPT:
                        data['fact_hash'] = '0' * 64
                    if mode == 'batch_ack' and data.get('schema') == c.SCHEMA and 'replayed' in data:
                        data['plan_hash'] = '0' * 64
                return render(renderer, data, *args, **kwargs)

            with patch.object(JSONRenderer, 'render', rendering):
                if mode == 'input':
                    result = self.source_input(self.source)
                elif mode == 'plan':
                    result = self.preview()
                elif mode == 'producer_ack':
                    result = self.publish(body=body)
                else:
                    result = self.create_batch(batch_body)
            assert result.status_code == 409, result.data
            assert not Fact.objects.exists() and not ReviewBatch.objects.exists()

    def test_each_of_nine_pins_is_verified_even_with_self_consistent_fact_hash(self):
        assert self.publish(self.source).status_code == 201
        original = o.rows
        for name in c.PINS:

            def mutated(*args, **kwargs):
                rows = original(*args, **kwargs)
                for row in rows:
                    value = row.proof['subject'][name]
                    row.proof['subject'][name] = value + 1 if type(value) is int else '0' * 64
                    row.fact_hash = c.digest(row.proof)
                    row.receipt['fact_hash'] = row.fact_hash
                    row.receipt_hash = c.digest(row.receipt)
                return rows

            with patch.object(o, 'rows', mutated):
                result = self.preview()
            assert result.status_code == 409, (name, result.data)

    def test_same_namespace_granted_profile_switch_conflicts_and_other_namespace_is_independent(self):
        body, key = self.fact_body(self.source), uuid4()
        assert self.publish(body=body, key=key).status_code == 201
        descriptor = {**self.descriptor, 'profile_id': str(uuid4())}
        digest = c.digest(descriptor)
        self.entries.append({**self.entries[0], 'descriptor': descriptor, 'digest': digest})
        self.grants[0]['profile_ids'].append(descriptor['profile_id'])
        result = self.publish(body={**body, 'profile_id': descriptor['profile_id'], 'profile_digest': digest}, key=key)
        assert (
            result.status_code == 409 and result.data['display_context']['reason'] == 'REVIEW_CONFIDENCE_KEY_CONFLICT'
        )
        descriptor = {**descriptor, 'producer_namespace': str(uuid4())}
        digest = c.digest(descriptor)
        self.entries[-1].update(descriptor=descriptor, digest=digest)
        from projects.models import ProjectMember
        from users.tests.factories import UserFactory

        second = UserFactory(active_organization=self.org)
        self.org.add_user(second)
        ProjectMember.objects.create(project=self.project, user=second, role='manager')
        self.grants.append(
            {
                **self.grants[0],
                'principal_id': f'label_studio_local:{second.pk}',
                'actor_id': second.pk,
                'producer_namespace': descriptor['producer_namespace'],
                'profile_ids': [descriptor['profile_id']],
            }
        )
        result = self.publish(
            body={**body, 'profile_id': descriptor['profile_id'], 'profile_digest': digest}, key=key, actor=second
        )
        assert result.status_code == 201 and Fact.objects.count() == 2, result.data
        assert self.publish(body=body, key=key).status_code == 200

    def test_every_frozen_column_and_save_bulk_raw_sql_paths_are_guarded(self):
        assert self.publish(self.source).status_code == 201
        fact = Fact.objects.get()
        migration = importlib.import_module('review_batches.migrations.0006_confidence_observation')
        for field in migration.FROZEN:
            old = getattr(fact, field)
            if isinstance(old, dict):
                value = {**old, 'illegal-update': True}
            elif field == 'created_at':
                value = old + timedelta(seconds=1)
            elif field in ('id', 'instance_id', 'producer_namespace', 'profile_id', 'event_uuid'):
                value = uuid4()
            elif type(old) is int:
                value = old + 1
            else:
                value = '0' * 64 if 'hash' in field or field == 'profile_digest' else 'another-identity'
            with self.assertRaises(DatabaseError) as raised, transaction.atomic():
                Fact.objects.filter(pk=fact.pk).update(**{field: value})
            assert 'Confidence observation is immutable' in str(raised.exception), (field, raised.exception)
        fact.proof = {**fact.proof, 'illegal-update': True}
        with self.assertRaises(DatabaseError), transaction.atomic():
            fact.save(update_fields=['proof'])
        with self.assertRaises(DatabaseError), transaction.atomic():
            Fact.objects.bulk_update([fact], ['proof'])
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                'UPDATE review_batches_reviewconfidenceobservation SET fact_hash=%s WHERE id=%s',
                ['0' * 64, fact.pk.hex],
            )

    def test_exact_actual_response_utf8_limit_and_one_byte_over(self):
        base = len(JSONRenderer().render({'padding': ''}))
        for extra in (-1, 0):
            value = {'padding': 'x' * (c.MAX_OUTPUT - base + extra)}
            response = o.response(value)
            assert len(response.content) == c.MAX_OUTPUT + extra
        with self.assertRaises(APIException) as raised:
            o.response({'padding': 'x' * (c.MAX_OUTPUT - base + 1)})
        assert raised.exception.status_code == 422

    def test_database_json_integer_to_equal_float_updates_are_immutable(self):
        assert self.publish(self.source).status_code == 201
        fact = Fact.objects.get()
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT \'{"value":1}\'::jsonb = \'{"value":1.0}\'::jsonb, \'{"value":1}\'::jsonb::text <> \'{"value":1.0}\'::jsonb::text'
                )
                assert cursor.fetchone() == (True, True)
        for field, target in (('proof', 'score_units'), ('proof', 'revision'), ('receipt', 'actor_id')):
            value = copy.deepcopy(getattr(fact, field))
            container = value['subject'] if target == 'revision' else value['owner'] if target == 'actor_id' else value
            container[target] = float(container[target])
            with self.assertRaises(DatabaseError) as raised, transaction.atomic():
                Fact.objects.filter(pk=fact.pk).update(**{field: value})
            assert 'Confidence observation is immutable' in str(raised.exception)
        with self.assertRaises(DatabaseError) as raised, transaction.atomic(), connection.cursor() as cursor:
            if connection.vendor == 'postgresql':
                cursor.execute(
                    "UPDATE review_batches_reviewconfidenceobservation SET proof=jsonb_set(proof,'{score_units}','100000.0'::jsonb) WHERE id=%s",
                    [fact.pk.hex],
                )
            else:
                cursor.execute(
                    "UPDATE review_batches_reviewconfidenceobservation SET proof=json_set(proof,'$.score_units',100000.0) WHERE id=%s",
                    [fact.pk.hex],
                )
        assert 'Confidence observation is immutable' in str(raised.exception)
        fact.refresh_from_db()
        assert type(fact.proof['score_units']) is int and type(fact.receipt['owner']['actor_id']) is int

    def test_strict_actual_json_bool_float_scalar_changes_rollback_every_entry(self):
        render = JSONRenderer.render
        for mode in ('input', 'receipt', 'preview', 'create'):
            for kind in ('bool', 'float'):
                body = self.fact_body(self.source)
                preview = self.preview()
                command = {**self.args, 'expected_plan_hash': preview.data['plan_hash']}

                def rendering(renderer, data, *args, **kwargs):
                    if isinstance(data, dict):
                        if mode == 'input' and data.get('schema') == c.INPUT:
                            data['subject']['revision'] = True if kind == 'bool' else 1.0
                        elif mode == 'receipt' and data.get('schema') == c.RECEIPT:
                            if kind == 'bool':
                                data['replayed'] = 0  # False == 0 in Python, but distinct JSON scalars.
                            else:
                                data['project_id'] = float(data['project_id'])
                        elif mode == 'preview' and data.get('schema') == c.SCHEMA and 'plan' in data:
                            data['plan']['candidates'][0]['revision'] = True if kind == 'bool' else 1.0
                        elif mode == 'create' and data.get('schema') == c.SCHEMA and 'replayed' in data:
                            data['processing_cost']['per_pass']['fact_count'] = False if kind == 'bool' else 0.0
                    return render(renderer, data, *args, **kwargs)

                with patch.object(JSONRenderer, 'render', rendering):
                    result = (
                        self.source_input(self.source)
                        if mode == 'input'
                        else self.publish(body=body)
                        if mode == 'receipt'
                        else self.preview()
                        if mode == 'preview'
                        else self.create_batch(command)
                    )
                assert result.status_code == 409, (mode, kind, result.data)
                assert not Fact.objects.exists() and not ReviewBatch.objects.exists()

    def test_confidence_read_actual_types_and_as_of_are_bound_while_replay_time_is_ignored(self):
        created = self.create_batch()
        render = JSONRenderer.render
        for mode in ('list', 'detail', 'capability', 'as_of'):

            def rendering(renderer, data, *args, **kwargs):
                if isinstance(data, dict):
                    if mode == 'list' and data.get('schema') == c.SCHEMA and 'count' in data:
                        data['count'] = True
                    elif mode == 'detail' and data.get('algorithm') == c.ALGORITHM:
                        data['candidate_count'] = 1.0
                    elif mode == 'as_of' and data.get('algorithm') == c.ALGORITHM:
                        data['as_of'] = {'injected': True}
                    elif mode == 'capability' and 'can_read_confidence_priority' in data:
                        data['can_create'] = 1
                return render(renderer, data, *args, **kwargs)

            url = (
                self.base
                if mode == 'list'
                else self.base + 'capability/'
                if mode == 'capability'
                else self.base + f'{created.data["id"]}/'
            )
            self.client.force_authenticate(self.manager)
            with patch.object(JSONRenderer, 'render', rendering):
                result = self.client.get(url)
            assert result.status_code == 409, (mode, result.data)
        assert self.client.get(self.base + f'{created.data["id"]}/').status_code == 200

    def test_duplicate_or_non_finite_rendered_json_is_not_a_matching_ack(self):
        render = JSONRenderer.render
        for mode in ('duplicate', 'NaN', 'Infinity'):
            body = self.fact_body(self.source)

            def rendering(renderer, data, *args, **kwargs):
                normal = render(renderer, data, *args, **kwargs)
                if isinstance(data, dict) and data.get('schema') == c.RECEIPT:
                    value = str(data['project_id']).encode() if mode == 'duplicate' else mode.encode()
                    return normal[:-1] + b',"project_id":' + value + b'}'
                return normal

            with patch.object(JSONRenderer, 'render', rendering):
                result = self.publish(body=body)
            assert result.status_code == 409 and not Fact.objects.exists(), result.data

    def test_batch_context_review_rendering_is_inside_atomic_and_current_authority(self):
        created = self.create_batch()
        render, resolve = JSONRenderer.render, LocalIdentityProvider.resolve
        for mode in ('actor', 'provider', 'answer', 'number', 'overflow'):
            changed = []

            def identity(provider, request):
                value = resolve(provider, request)
                if changed and mode == 'provider':
                    return replace(value, source='another-provider')
                if changed and mode == 'actor':
                    return replace(value, local_user_id=self.producer.pk)
                return value

            def rendering(renderer, data, *args, **kwargs):
                controlled = isinstance(data, dict) and 'result_snapshot' in data and 'review' in data
                if controlled:
                    if mode == 'answer':
                        data['result_snapshot']['task']['data']['text'] = 'rendered different answer'
                    if mode == 'number':
                        data['revision'] = 1.0
                    if mode in ('actor', 'provider'):
                        changed.append(True)
                value = render(renderer, data, *args, **kwargs)
                return value + b' ' * c.MAX_OUTPUT if controlled and mode == 'overflow' else value

            self.client.force_authenticate(self.reviewer)
            with patch.object(LocalIdentityProvider, 'resolve', identity), patch.object(
                JSONRenderer, 'render', rendering
            ):
                result = self.client.post(
                    f'/api/submissions/{self.source.pk}/review/?review_batch={created.data["id"]}',
                    {'decision': 'approved'},
                    format='json',
                )
            assert result.status_code in (403, 409, 422), (mode, result.data)
            assert not ReviewDecision.objects.exists()
            assert Submission.objects.get(pk=self.source.pk).status == 'pending'
        result = self.client.post(
            f'/api/submissions/{self.source.pk}/review/?review_batch={created.data["id"]}',
            {'decision': 'approved'},
            format='json',
        )
        assert result.status_code == 200 and ReviewDecision.objects.count() == 1, result.data

    def test_exact_metadata_canonical_union_below_at_and_above_four_mib(self):
        value = {'padding': 'x' * (c.MAX_FACT - len(b'{"padding":""}'))}
        rows = [
            Fact(
                project=self.project,
                instance_id=INSTANCE,
                producer_namespace=NAMESPACE,
                profile_id=PROFILE,
                profile_digest=self.profile_digest,
                event_uuid=uuid4(),
                submission_id_frozen=10000 + i,
                actor_id_frozen=self.producer.pk,
                creator_principal='synthetic',
                creator_source='synthetic',
                request_hash='a' * 64,
                proof=value,
                receipt=value,
                fact_hash='b' * 64,
                receipt_hash='c' * 64,
            )
            for i in range(257)
        ]
        Fact.objects.bulk_create(rows)
        for count in (255, 256):
            budget = c.metadata_budget()
            budget.rows(Fact.objects.filter(submission_id_frozen__lt=10000 + count), o.FIELDS, o.LIMITS)
            assert budget.canonical == count * c.MAX_FACT * 2
        with self.assertRaises(APIException) as raised:
            c.metadata_budget().rows(Fact.objects.all(), o.FIELDS, o.LIMITS)
        assert raised.exception.status_code == 422

    def test_actual_legal_snapshot_union_below_at_and_above_eight_mib(self):
        from .test_confidence_budgets import TestConfidenceBudgets

        Submission.objects.filter(pk=self.source.pk).update(status='superseded')
        rows = TestConfidenceBudgets.bulk_sources(self, 33)
        target, remainder = divmod(c.MAX_SNAPSHOT, len(rows))
        last = None
        for i, row in enumerate(rows):
            snapshot = copy.deepcopy(row.result_snapshot)
            snapshot['exact-union-padding'] = ''
            size = len(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
            snapshot['exact-union-padding'] = 'x' * (target + (remainder if i == len(rows) - 1 else 0) - size)
            Submission.objects.filter(pk=row.pk).update(result_snapshot=snapshot, result_hash=c.digest(snapshot))
            last = snapshot
        for delta in (-1, 0, 1):
            snapshot = copy.deepcopy(last)
            if delta < 0:
                snapshot['exact-union-padding'] = snapshot['exact-union-padding'][:-1]
            elif delta > 0:
                snapshot['exact-union-padding'] += 'x'
            Submission.objects.filter(pk=rows[-1].pk).update(result_snapshot=snapshot, result_hash=c.digest(snapshot))
            result = self.preview()
            if delta <= 0:
                assert result.status_code == 200, result.data
                assert result.data['processing_cost']['per_pass']['snapshot_canonical_bytes'] == c.MAX_SNAPSHOT + delta
            else:
                assert result.status_code == 422, result.data

    def test_sql_single_snapshot_threshold_is_distinct_from_tighter_canonical_threshold(self):
        from django.db.models import BigIntegerField, JSONField
        from django.db.models.expressions import RawSQL

        measure = (
            'OCTET_LENGTH(CAST(result_snapshot AS TEXT))'
            if connection.vendor == 'postgresql'
            else 'LENGTH(CAST(result_snapshot AS BLOB))'
        )

        def stored():
            return (
                Submission.objects.filter(pk=self.source.pk)
                .annotate(size=RawSQL(measure, [], output_field=BigIntegerField()))
                .values_list('size', flat=True)
                .get()
            )

        original = JSONField.from_db_value
        for delta in (-1, 0, 1):
            snapshot = copy.deepcopy(self.source.result_snapshot)
            snapshot['exact-sql-marker'] = ''
            Submission.objects.filter(pk=self.source.pk).update(result_snapshot=snapshot)
            snapshot['exact-sql-marker'] = 'x' * (c.MAX_SINGLE_STORED + delta - stored())
            Submission.objects.filter(pk=self.source.pk).update(result_snapshot=snapshot)
            assert stored() == c.MAX_SINGLE_STORED + delta
            decoded = []

            def decode(field, value, expression, db):
                if isinstance(value, str) and 'exact-sql-marker' in value:
                    decoded.append(1)
                return original(field, value, expression, db)

            with patch.object(JSONField, 'from_db_value', decode):
                result = self.preview()
            assert result.status_code == 422
            # At/below SQL cap transfers bounded JSON, then the tighter 256KiB
            # canonical limit rejects. Above SQL cap transfers no JSON at all.
            assert bool(decoded) == (delta <= 0)

    def test_descriptor_exact_four_kib_and_capability_wrapper_unit_boundaries(self):
        descriptor = copy.deepcopy(self.descriptor)
        free = ('label', 'version', 'evaluator_id', 'evaluator_version', 'model_id', 'calibration_version')
        for key in free:
            descriptor[key] = 'a'

        def size():
            return len(json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())

        for key in free:
            while len(descriptor[key]) < 160 and size() + 6 <= c.MAX_PROFILE:
                descriptor[key] += '\x01'
            while len(descriptor[key]) < 160 and size() < c.MAX_PROFILE:
                descriptor[key] += 'x'
        assert size() == c.MAX_PROFILE
        assert c.descriptor(descriptor) == descriptor
        key = next(k for k in free if len(descriptor[k]) < 160)
        descriptor[key] += 'x'
        with self.assertRaises(APIException) as raised:
            c.descriptor(descriptor)
        assert raised.exception.status_code == 422
        base = len(b'{"padding":""}')
        for delta in (-1, 0):
            assert (
                c.bounded({'padding': 'x' * (c.MAX_CAPABILITY - base + delta)}, c.MAX_CAPABILITY)
                == c.MAX_CAPABILITY + delta
            )
        with self.assertRaises(APIException):
            c.bounded({'padding': 'x' * (c.MAX_CAPABILITY - base + 1)}, c.MAX_CAPABILITY)
