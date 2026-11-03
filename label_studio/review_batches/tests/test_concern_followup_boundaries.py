"""Hostile stored values, pre-hydration budgets and frozen record boundaries."""

from unittest.mock import patch
from uuid import uuid4

from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from django.db.models import JSONField
from rest_framework.test import APITestCase
from review_batches import followup_contract, followup_origin, followup_records, followup_snapshots
from review_batches.models import SecondReviewConcernFollowup
from review_batches.second_review_bounded import Budget
from tasks.models import ReviewDecision, Submission
from tasks.submissions import _snapshot_hash

from .test_concern_followup import ConcernFixtures


class TestConcernFollowupBoundaries(ConcernFixtures, APITestCase):
    def setUp(self):
        self.followup_setup()

    def test_E03_E06_short_invalid_original_and_target_dates_return_corrupt(self):
        if connection.vendor != 'sqlite':
            self.skipTest('SQLite weak storage types')
        for side in ('original', 'target'):
            source, batch, task = self.concerned()
            target = self.successor(source, decision='approved') if side == 'target' else None
            subject = target or source
            with connection.cursor() as cursor:
                cursor.execute(
                    f'UPDATE {ReviewDecision._meta.db_table} SET created_at=%s WHERE submission_id=%s',
                    ['not-a-date', subject.pk],
                )
            response = self.followup_context(task, target)
            assert response.status_code == 409, response.data
            assert 'original' not in response.data
            with connection.cursor() as cursor:
                cursor.execute(
                    f'UPDATE {ReviewDecision._meta.db_table} SET created_at=123 WHERE submission_id=%s', [subject.pk]
                )
            response = self.followup_context(task, target)
            assert response.status_code == 409, response.data

    def test_E03_E06_native_short_bad_opinion_date_in_history_is_corrupt(self):
        import copy

        from review_batches.models import SecondReviewDecision, SecondReviewTask

        if connection.vendor != 'sqlite':
            self.skipTest('PostgreSQL enforces native timestamp storage')
        source, batch, task = self.concerned()
        original = SecondReviewDecision.objects.get(task_id_frozen=task)
        for bad in (123, 'not-a-date'):
            with transaction.atomic():
                source, batch = self.prepared()
                dispatched, _ = self.dispatch(source, batch)
                pending = SecondReviewTask.objects.get(pk=dispatched.data['receipt']['task_id'])
                damaged = copy.copy(original)
                damaged.id, damaged.idempotency_key = uuid4(), uuid4()
                damaged.task_id = damaged.task_id_frozen = pending.pk
                damaged.source_hash, damaged.first_review_hash = pending.source_hash, pending.first_review_hash
                fields = list(damaged._meta.local_concrete_fields)
                columns = ','.join(connection.ops.quote_name(f.column) for f in fields)
                params = [
                    bad if f.name == 'created_at' else f.get_db_prep_save(getattr(damaged, f.attname), connection)
                    for f in fields
                ]
                with connection.cursor() as cursor:
                    cursor.execute(
                        'INSERT INTO '
                        + damaged._meta.db_table
                        + ' ('
                        + columns
                        + ') VALUES ('
                        + ','.join(['%s'] * len(fields))
                        + ')',
                        params,
                    )
                SecondReviewTask.objects.filter(pk=pending.pk).update(status='completed', version=2)
                self.client.force_authenticate(self.manager)
                response = self.client.get(self.followup_base)
                assert response.status_code == 409, response.data
                assert response.data['display_context']['reason'] == 'SECOND_REVIEW_CORRUPT'
                assert 'results' not in response.data
                transaction.set_rollback(True)

    def test_E03_oversized_snapshot_and_scalar_never_decode(self):
        source, batch, task = self.concerned()
        target = self.successor(source)
        poisoned = {**target.result_snapshot, 'NEVER_DECODE_POISON': 'x' * (2 * 1024 * 1024)}
        Submission.objects.filter(pk=target.pk).update(result_snapshot=poisoned, result_hash=_snapshot_hash(poisoned))
        original = JSONField.from_db_value
        decoded = []

        def spy(field, value, *args):
            if value and 'NEVER_DECODE_POISON' in str(value):
                decoded.append(True)
            return original(field, value, *args)

        with patch.object(JSONField, 'from_db_value', spy):
            assert self.followup_context(task, target).status_code == 422
        assert not decoded
        if connection.vendor == 'sqlite':
            Submission.objects.filter(pk=target.pk).update(
                result_snapshot=target.result_snapshot, result_hash='h' * 300
            )
            assert self.followup_context(task, target).status_code == 422
        else:
            with self.assertRaises(DatabaseError), transaction.atomic():
                Submission.objects.filter(pk=target.pk).update(result_hash='h' * 300)

    def test_E04_request_union_budget_is_not_reset_between_original_target_and_verification(self):
        source, batch, task = self.concerned()
        target = self.successor(source)
        normal = self.followup_context(task, target)
        assert normal.status_code == 200
        seen = []
        real = followup_origin.load

        def metered(ctx, pk, target_id, budget):
            before = (id(budget), budget.stored, budget.canonical)
            result = real(ctx, pk, target_id, budget)
            seen.append((before, (id(budget), budget.stored, budget.canonical)))
            return result

        with patch.object(followup_origin, 'load', metered):
            assert self.followup_context(task, target).status_code == 200
        assert len(seen) == 2 and seen[0][1][0] == seen[1][0][0]
        assert seen[1][0][1] >= seen[0][1][1] and seen[1][1][1] > seen[1][0][1]
        peak = seen[1][1][1]
        with patch('review_batches.followup_service.Budget', side_effect=lambda: Budget(stored=peak - 1)):
            assert self.followup_context(task, target).status_code == 422

    def test_E04_remaining_stored_gate_blocks_next_snapshot_before_decode(self):
        source, batch, task = self.concerned()
        # Call the narrow adapter with a nearly exhausted request budget, not a
        # fake oversized individual source. Its valid JSON must never hydrate.
        budget = Budget(stored=32 * 1024 * 1024)
        budget.stored = budget.stored_limit - 1
        original, decoded = JSONField.from_db_value, []

        def spy(field, value, *args):
            if value and 'Frozen text' in str(value):
                decoded.append(True)
            return original(field, value, *args)

        with patch.object(JSONField, 'from_db_value', spy), self.assertRaises(Exception) as error:
            followup_snapshots.load(Submission.objects.filter(pk=source.pk), budget)
        assert error.exception.status_code == 422 and not decoded

    def test_D10_every_frozen_field_and_nullable_rebind_are_guarded(self):
        source, batch, task = self.concerned()
        saved = self.record_followup(self.followup_body(task))
        row = SecondReviewConcernFollowup.objects.get(pk=saved.data['receipt']['followup_id'])
        nullable = {'second_review_task', 'opinion', 'created_by', 'related_submission'}
        for field in row._meta.fields:
            if field.name in nullable:
                continue
            old = getattr(row, field.attname)
            if field.get_internal_type() == 'JSONField':
                new = {'tampered': True}
            elif field.get_internal_type() == 'UUIDField':
                new = uuid4()
            elif field.get_internal_type() == 'DateTimeField':
                from datetime import timedelta

                new = old + timedelta(seconds=1)
            elif field.get_internal_type() in (
                'PositiveBigIntegerField',
                'PositiveIntegerField',
                'BigAutoField',
                'ForeignKey',
            ):
                new = (old or 0) + 1
            else:
                new = str(old) + 'x'
            with self.assertRaises(DatabaseError, msg=field.name), transaction.atomic():
                SecondReviewConcernFollowup.objects.filter(pk=row.pk).update(**{field.attname: new})
        SecondReviewConcernFollowup.objects.filter(pk=row.pk).update(second_review_task=None)
        with self.assertRaises(DatabaseError), transaction.atomic():
            SecondReviewConcernFollowup.objects.filter(pk=row.pk).update(second_review_task_id=row.task_id_frozen)
        row.refresh_from_db()
        assert row.receipt == saved.data['receipt']

    def test_D10_queryset_project_cascades(self):
        source, batch, task = self.concerned()
        assert self.record_followup(self.followup_body(task)).status_code == 201
        self.project.__class__.objects.filter(pk=self.project.pk).delete()
        assert not SecondReviewConcernFollowup.objects.exists()

    def test_D10_organization_cascades(self):
        source, batch, task = self.concerned()
        assert self.record_followup(self.followup_body(task)).status_code == 201
        # Existing JWTSettings deliberately uses DO_NOTHING. A naked physical
        # org delete is already blocked; preserve its rollback semantics, then
        # exercise the valid dependency cleanup only in this synthetic scope.
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.org.__class__.objects.filter(pk=self.org.pk).delete()
            connection.check_constraints()
        assert SecondReviewConcernFollowup.objects.exists()
        from jwt_auth.models import JWTSettings

        JWTSettings.objects.filter(organization=self.org).delete()
        self.org.delete()
        assert not SecondReviewConcernFollowup.objects.exists()

    def test_C01_C02_malformed_frozen_envelope_fails_history_without_500(self):
        for mutation in ('missing_batch', 'numeric_project', 'extra_root_field'):
            with transaction.atomic():
                source, batch, task = self.concerned()
                body = self.followup_body(task)
                captured = []
                with patch.object(
                    SecondReviewConcernFollowup,
                    'save',
                    autospec=True,
                    side_effect=lambda row, **kw: captured.append(row),
                ):
                    assert self.record_followup(body).status_code == 404
                row = captured[0]
                if mutation == 'missing_batch':
                    del row.origin_ref['source']['batch_id']
                elif mutation == 'numeric_project':
                    row.origin_ref['source']['project_id'] = float(self.project.pk)
                else:
                    row.origin_ref['unexpected'] = True
                row.origin_ref['opinion']['source_hash'] = followup_contract.digest(row.origin_ref['source'])
                row.origin_ref['opinion_receipt_hash'] = followup_contract.digest(row.origin_ref['opinion'])
                row.concern_hash = followup_contract.digest(row.origin_ref)
                row.receipt = followup_records.receipt(row)
                row.receipt_hash = followup_contract.digest(row.receipt)
                if mutation == 'extra_root_field':
                    row.save(force_insert=True)
                    response = self.client.get(self.followup_base + str(row.pk) + '/')
                    assert response.status_code == 409, response.data
                    assert self.client.get(self.followup_base).status_code == 409
                else:
                    with self.assertRaises(DatabaseError), transaction.atomic():
                        row.save(force_insert=True)
                    with self.assertRaises(Exception) as error:
                        followup_records.checked(row)
                    assert error.exception.status_code == 409
                transaction.set_rollback(True)

    def test_D10_sqlite_project_cleanup_then_real_flush(self):
        if connection.vendor != 'sqlite':
            self.skipTest('SQLite administrative reset boundary')
        source, batch, task = self.concerned()
        assert self.record_followup(self.followup_body(task)).status_code == 201
        self.project.delete()
        call_command('flush', interactive=False, verbosity=0)
        assert not SecondReviewConcernFollowup.objects.exists()

    def test_E02_reason_character_and_utf8_boundaries(self):
        source, batch, task = self.concerned()
        body = self.followup_body(task)
        reason = '\U0001f600' * 2000
        assert len(reason.encode('utf-8')) == 8000
        assert self.record_followup({**body, 'reason': reason}).status_code == 201

    def test_C03_browser_numeric_and_key_presentation_vectors_keep_legacy_hash(self):
        import json
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[3]
            / 'web/apps/labelstudio/src/pages/DataManager/followupPresentationVectors.json'
        )
        for vector in json.loads(path.read_text(encoding='utf-8')):
            assert followup_origin.presentation_hash(vector['input']) == vector['sha256'], vector['name']
        with self.assertRaises(Exception) as error:
            followup_origin.presentation_hash({'not_binary64': 10**400})
        assert error.exception.status_code == 400
        with self.assertRaises(Exception) as error:
            followup_origin.presentation_hash({'invalid_unicode': '\ud800'})
        assert error.exception.status_code == 409

    def test_E01_invalid_unicode_and_infinite_request_are_bad_input(self):
        import json

        source, batch, task = self.concerned()
        body = self.followup_body(task)
        for reason in ('\ud800', float('inf')):
            response = self.client.post(
                self.followup_base,
                json.dumps({**body, 'reason': reason}),
                content_type='application/json',
                HTTP_IDEMPOTENCY_KEY=str(uuid4()),
            )
            assert response.status_code == 400, response.data

    def test_C03_original_identity_snapshot_and_full_initial_proof_are_frozen(self):
        import copy

        from django.utils import timezone
        from tasks.models import TaskAssignment

        source, batch, task = self.concerned()
        mutations = [
            (Submission, {'revision': 99}),
            (Submission, {'submitted_by_id': self.second_worker.pk}),
            (Submission, {'result_hash': 'f' * 64}),
            (TaskAssignment, {'assignee_id': self.second_worker.pk}),
            (ReviewDecision, {'reason': 'Changed full initial reason'}),
            (ReviewDecision, {'reviewer_id': self.second_worker.pk}),
            (ReviewDecision, {'decision': 'rejected'}),
            (ReviewDecision, {'created_at': timezone.now()}),
        ]
        for model, values in mutations:
            with transaction.atomic():
                query = (
                    model.objects.filter(submission=source)
                    if model is ReviewDecision
                    else model.objects.filter(pk=source.assignment_id if model is TaskAssignment else source.pk)
                )
                query.update(**values)
                response = self.followup_context(task)
                assert response.status_code in (403, 409), (values, response.data)
                assert 'original' not in response.data
                transaction.set_rollback(True)
        for part in ('result', 'text', 'config'):
            with transaction.atomic():
                snapshot = copy.deepcopy(source.result_snapshot)
                if part == 'result':
                    snapshot['annotation']['result'][0]['value']['choices'] = ['B']
                elif part == 'text':
                    snapshot['task']['data']['text'] = 'Altered frozen text'
                else:
                    snapshot['project']['label_config'] += ' '
                Submission.objects.filter(pk=source.pk).update(
                    result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
                )
                assert self.followup_context(task).status_code == 409
                transaction.set_rollback(True)

    def test_C02_frozen_plan_cannot_refresh_selected_rule_or_priority(self):
        import copy

        from review_batches import service
        from review_batches.models import ReviewBatch

        source, batch, task = self.concerned()
        original = ReviewBatch.objects.get(pk=batch)
        for alter in (
            lambda p: p.update(algorithm='unknown'),
            lambda p: p['rule'].update(rate=True),
            lambda p: p['candidates'][0].update(selected=False),
            lambda p: p.update(priority={'schema': 'unknown'}),
        ):
            plan = copy.deepcopy(original.plan)
            alter(plan)
            with self.assertRaises(DatabaseError), transaction.atomic():
                ReviewBatch.objects.filter(pk=batch).update(plan=plan, plan_hash=service.digest(plan))
            # Model a damaged legacy read after the SQL size gate, without
            # disabling immutable storage guards to manufacture corruption.
            read = Budget.rows

            def damaged(budget, query, limits, **kwargs):
                values = read(budget, query, limits, **kwargs)
                if query.model is ReviewBatch:
                    for value in values:
                        value.plan, value.plan_hash = copy.deepcopy(plan), service.digest(plan)
                return values

            with patch.object(Budget, 'rows', damaged):
                response = self.followup_context(task)
                assert response.status_code in (400, 409), response.data
                assert response.data['display_context']['reason'] in (
                    'SECOND_REVIEW_UNSUPPORTED',
                    'SECOND_REVIEW_CORRUPT',
                    'SECOND_REVIEW_SOURCE_CHANGED',
                    'SECOND_REVIEW_SOURCE_INELIGIBLE',
                )

    def test_E05_snapshot_exact_canonical_boundary_and_one_unicode_byte_over(self):
        import copy

        source, batch, task = self.concerned()
        snapshot = copy.deepcopy(source.result_snapshot)
        snapshot['padding'] = ''
        snapshot['padding'] = 'x' * (256 * 1024 - followup_contract.bounded(snapshot))
        Submission.objects.filter(pk=source.pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
        assert len(followup_snapshots.load(Submission.objects.filter(pk=source.pk), Budget())) == 1
        snapshot['padding'] += '中'
        Submission.objects.filter(pk=source.pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
        with self.assertRaises(Exception) as error:
            followup_snapshots.load(Submission.objects.filter(pk=source.pk), Budget())
        assert error.exception.status_code == 422

    def test_D10_native_json_duplicates_rejected_and_object_order_preserved(self):
        import json

        source, batch, task = self.concerned()
        target = self.successor(source)
        body, captured = self.followup_body(task, target), []
        with patch.object(
            SecondReviewConcernFollowup, 'save', autospec=True, side_effect=lambda row, **kwargs: captured.append(row)
        ):
            assert self.record_followup(body).status_code == 404
        row = captured[0]

        def reversed_keys(value):
            if isinstance(value, dict):
                return {k: reversed_keys(value[k]) for k in reversed(value)}
            if isinstance(value, list):
                return [reversed_keys(v) for v in value]
            return value

        row.origin_ref = reversed_keys(row.origin_ref)
        with transaction.atomic():
            row.save(force_insert=True)
            assert followup_records.checked(row).receipt_hash == row.receipt_hash
            changed = json.loads(json.dumps(row.origin_ref))
            changed['source']['project_id'] = float(changed['source']['project_id'])
            with self.assertRaises(DatabaseError), transaction.atomic():
                SecondReviewConcernFollowup.objects.filter(pk=row.pk).update(origin_ref=changed)
            transaction.set_rollback(True)
        if connection.vendor != 'sqlite':
            return
        fields = list(row._meta.local_concrete_fields)
        columns = ','.join(connection.ops.quote_name(f.column) for f in fields)
        command = (
            'INSERT INTO ' + row._meta.db_table + ' (' + columns + ') VALUES (' + ','.join(['%s'] * len(fields)) + ')'
        )
        for field_name, key, escaped in (
            ('origin_ref', 'source', 'sour\\u0063e'),
            ('receipt', 'effect', 'effe\\u0063t'),
            ('target_ref', 'subject', 'subje\\u0063t'),
        ):
            value = getattr(row, field_name)
            for alias in (key, escaped):
                raw = '{"' + alias + '":' + json.dumps(value[key]) + ',' + json.dumps(value)[1:]
                assert json.loads(raw) == value
                params = [
                    raw if f.name == field_name else f.get_db_prep_save(getattr(row, f.attname), connection)
                    for f in fields
                ]
                with self.assertRaises(DatabaseError), transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(command, params)

    def test_B07_response_time_role_revocation_rolls_back_without_answers(self):
        from projects.models import ProjectMember

        source, batch, task = self.concerned()
        body = self.followup_body(task)
        from review_batches import second_review_contract

        render = second_review_contract.JSONRenderer.render

        def revoke(renderer, data, *args, **kwargs):
            result = render(renderer, data, *args, **kwargs)
            if isinstance(data, dict) and 'receipt' in data:
                ProjectMember.objects.filter(project=self.project, user=self.manager).update(enabled=False)
                # Creator fallback is also denied by organization membership.
                from organizations.models import OrganizationMember

                OrganizationMember.objects.filter(organization=self.org, user=self.manager).delete()
            return result

        with patch.object(second_review_contract.JSONRenderer, 'render', revoke):
            response = self.record_followup(body)
        assert response.status_code in (403, 404), response.data
        assert not SecondReviewConcernFollowup.objects.exists()
