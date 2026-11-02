import copy
from unittest.mock import patch
from uuid import uuid4

from access_control.project_operations import local_principal
from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from rest_framework.exceptions import APIException
from rest_framework.test import APITestCase
from review_batches import second_review_contract as c
from review_batches import second_review_service as s
from review_batches.models import ReviewBatch, SecondReviewDecision, SecondReviewTask
from tasks.models import ReviewDecision

from .second_review_fixtures import SecondReviewFixtures


class TestSecondReviewLedger(SecondReviewFixtures, APITestCase):
    def setUp(self):
        self.second_setup()
        self.source, self.batch = self.prepared()
        self.key = uuid4()
        result, self.body = self.dispatch(self.source, self.batch, key=self.key)
        assert result.status_code == 201, result.data
        self.pk = result.data['receipt']['task_id']

    def test_C04_deleted_initial_review_cannot_rebind_same_numeric_id(self):
        first = ReviewDecision.objects.get(submission=self.source)
        fields = {f.attname: getattr(first, f.attname) for f in first._meta.fields}
        first.delete()
        ReviewDecision.objects.create(**fields)
        row = SecondReviewTask.objects.get(pk=self.pk)
        assert row.first_review_id is None
        self.client.force_authenticate(self.supervisor)
        assert self.client.get(self.second_base + self.pk + '/source/').status_code == 409
        assert self.client.get(self.second_base + self.pk + '/').status_code == 200
        assert self.dispatch(self.source, self.batch, self.body, self.key)[0].status_code == 200

    def test_C06_frozen_columns_bulk_native_and_json_numeric_scale(self):
        row = SecondReviewTask.objects.get(pk=self.pk)
        changes = [
            {'id': uuid4()},
            {'project_id': self.project.pk + 1000},
            {'source_hash': 'a' * 64},
            {'assignee_id_frozen': self.worker.pk},
            {'first_review_hash': 'a' * 64},
            {'request_hash': 'a' * 64},
            {'creator_principal': 'forged'},
            {'creator_source': 'forged'},
            {'idempotency_key': uuid4()},
            {'created_at': timezone.now()},
            {'receipt_hash': 'b' * 64},
            {'review_batch_id_frozen': self.batch + 1},
            {'proof': {**row.proof, 'subject': {**row.proof['subject'], 'revision': float(row.revision)}}},
            {'receipt': {**row.receipt, 'version': 1.0}},
            {'first_review_id': row.first_review_id + 10000},
        ]
        for mutation in changes:
            with self.subTest(mutation=mutation), self.assertRaises(DatabaseError), transaction.atomic():
                SecondReviewTask.objects.filter(pk=self.pk).update(**mutation)
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                'UPDATE review_batches_secondreviewtask SET status=%s, version=2 WHERE id=%s',
                ['cancelled', self.pk.replace('-', '') if connection.vendor == 'sqlite' else self.pk],
            )
        # A non-null convenience FK can only remain unchanged or be cleared.
        with self.assertRaises(DatabaseError), transaction.atomic():
            SecondReviewTask.objects.filter(pk=self.pk).update(assignee_id=self.reviewer.pk)

    def test_C06_no_receipt_or_incomplete_cancellation_cannot_flip_state(self):
        minimal = {
            'receipt': dict(
                command='cancel',
                task_id=self.pk,
                owner=dict(actor_id=self.manager.pk),
                version=2,
                effect=c.EFFECT,
                reason='x',
            )
        }
        for envelope in ({}, {'receipt': {}}, {'receipt': {'command': 'cancel', 'version': 2}}, minimal):
            with self.assertRaises(DatabaseError), transaction.atomic():
                SecondReviewTask.objects.filter(pk=self.pk).update(
                    status='cancelled',
                    version=2,
                    cancel_key=uuid4(),
                    cancelled_by=self.manager,
                    cancellation=envelope,
                    cancellation_hash='c' * 64,
                )
        with self.assertRaises(DatabaseError), transaction.atomic():
            SecondReviewTask.objects.filter(pk=self.pk).update(status='completed', version=2)

    def test_C06_cancel_native_sql_rejects_wrong_json_types_scope_and_uuid(self):
        ctx = c.guard(self.project.pk, local_principal(self.manager), self.manager, 'cancel')
        row, key, now = SecondReviewTask.objects.get(pk=self.pk), uuid4(), timezone.now()
        body = dict(expected_version=1, reason='123', expected_viewer_hash=self.body['expected_viewer_hash'])
        ack = s.receipt(
            'cancel',
            ctx,
            row.pk,
            key,
            c.digest(body),
            now,
            source_hash=row.source_hash,
            reason=body['reason'],
            version=2,
        )
        original = dict(body=body, receipt=ack)
        mutations = []
        for path, value in (
            ('body.reason', 123),
            ('body.expected_viewer_hash', int('1' * 64)),
            ('receipt.request_hash', int('1' * 64)),
            ('receipt.version', 2.0),
            ('receipt.owner.actor_id', True),
            ('receipt.project_id', self.project.pk + 1),
            ('receipt.key', str(uuid4())),
            (
                'receipt.task_id',
                self.pk.replace('-', '')[:4]
                + '-'
                + self.pk.replace('-', '')[4:8]
                + '-'
                + self.pk.replace('-', '')[8:12]
                + '-'
                + self.pk.replace('-', '')[12:16]
                + '-'
                + self.pk.replace('-', '')[16:],
            ),
        ):
            data = copy.deepcopy(original)
            target = data
            names = path.split('.')
            for name in names[:-1]:
                target = target[name]
            target[names[-1]] = value
            mutations.append(data)
        for envelope in mutations:
            with self.assertRaises(DatabaseError), transaction.atomic():
                SecondReviewTask.objects.filter(pk=self.pk).update(
                    status='cancelled',
                    version=2,
                    cancel_key=key,
                    cancelled_by=self.manager,
                    cancellation=envelope,
                    cancellation_hash=c.digest(envelope),
                )
        row.refresh_from_db()
        assert row.status == 'pending' and row.cancel_key is None

    def test_C02_scalar_first_review_hash_must_match_frozen_proof(self):
        # Native INSERT can import malformed historical records. Source and new
        # opinion must fail closed even when both local receipt/scalar agree.
        old = SecondReviewTask.objects.get(pk=self.pk)
        fields = {f.attname: getattr(old, f.attname) for f in old._meta.fields}
        other_instance = str(uuid4())
        proof = copy.deepcopy(old.proof)
        proof['instance_id'] = other_instance
        ack = {
            **old.receipt,
            'task_id': str(uuid4()),
            'instance_id': other_instance,
            'source_hash': c.digest(proof),
            'first_review_hash': 'e' * 64,
            'key': str(uuid4()),
        }
        fields.update(
            id=ack['task_id'],
            instance_id=other_instance,
            proof=proof,
            source_hash=ack['source_hash'],
            first_review_hash=ack['first_review_hash'],
            receipt=ack,
            receipt_hash=c.digest(ack),
            idempotency_key=ack['key'],
        )
        bad = SecondReviewTask.objects.create(**fields)
        with self.settings(ANNOTATION_SECOND_REVIEW_INSTANCE_ID=other_instance):
            self.client.force_authenticate(self.supervisor)
            assert self.client.get(self.second_base + str(bad.pk) + '/source/').status_code == 409
        assert c.hexadecimal(old.first_review_hash)

    def test_C06_sqlite_half_state_reports_corruption_and_pg_defers_constraint(self):
        if connection.vendor == 'postgresql':
            body = self.opinion_body(self.pk)
            assert self.decide(self.pk, body).status_code == 201
            with self.assertRaises(DatabaseError), transaction.atomic():
                SecondReviewDecision.objects.filter(task_id=self.pk).delete()
                with connection.cursor() as cursor:
                    cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
        else:
            # The command is always atomic; native SQL can create a pending +
            # opinion half-state on SQLite. It must never become actionable.
            row = SecondReviewTask.objects.get(pk=self.pk)
            body = self.opinion_body(self.pk)
            decision_id, key, now = uuid4(), uuid4(), timezone.now()
            ctx = c.guard(self.project.pk, local_principal(self.supervisor), self.supervisor, 'decide')
            ack = s.receipt(
                'decision',
                ctx,
                row.pk,
                key,
                c.digest(body),
                now,
                decision_id=str(decision_id),
                source_hash=row.source_hash,
                first_review_hash=row.first_review_hash,
                outcome='confirmed',
                reason='',
                version=2,
            )
            SecondReviewDecision.objects.create(
                id=decision_id,
                project=self.project,
                task=row,
                task_id_frozen=row.pk,
                created_by=self.supervisor,
                actor_id_frozen=self.supervisor.pk,
                creator_principal=ctx[0].principal_id,
                creator_source=ctx[0].source,
                created_at=now,
                source_hash=row.source_hash,
                first_review_hash=row.first_review_hash,
                outcome='confirmed',
                reason='',
                idempotency_key=key,
                request_hash=c.digest(body),
                receipt=ack,
                receipt_hash=c.digest(ack),
            )
            self.client.force_authenticate(self.supervisor)
            assert self.client.get(self.second_base + self.pk + '/').status_code == 409
            assert self.decide(self.pk, body).status_code == 409

    def test_C07_subject_slot_across_batch_key_assignee_and_terminal(self):
        old = ReviewBatch.objects.get(pk=self.batch)
        other = ReviewBatch.objects.create(
            project=self.project,
            created_by=self.manager,
            actor_id_frozen=self.manager.pk,
            creator_principal=old.creator_principal,
            name='same exact subject',
            idempotency_key=uuid4(),
            request_hash='a' * 64,
            plan=old.plan,
            plan_hash=old.plan_hash,
        )
        body = self.dispatch_body(self.source, other.pk)
        result, _ = self.dispatch(self.source, other.pk, body)
        assert result.status_code == 409 and SecondReviewTask.objects.count() == 1
        with self.assertRaises(DatabaseError), transaction.atomic():
            fields = {
                f.attname: getattr(SecondReviewTask.objects.get(pk=self.pk), f.attname)
                for f in SecondReviewTask._meta.fields
            }
            fields.update(
                id=uuid4(), idempotency_key=uuid4(), assignee_id=self.reviewer.pk, assignee_id_frozen=self.reviewer.pk
            )
            SecondReviewTask.objects.create(**fields)

    def test_C08_terminal_cannot_reopen_change_opinion_or_reassign(self):
        assert self.decide(self.pk).status_code == 201
        for mutation in (
            {'status': 'pending', 'version': 1},
            {'status': 'cancelled'},
            {'assignee_id': self.reviewer.pk},
        ):
            with self.assertRaises(DatabaseError), transaction.atomic():
                SecondReviewTask.objects.filter(pk=self.pk).update(**mutation)
        for mutation in (
            {'outcome': 'concern'},
            {'reason': 'altered'},
            {'created_by_id': self.reviewer.pk},
            {'receipt': {'version': 2}},
        ):
            with self.assertRaises(DatabaseError), transaction.atomic():
                SecondReviewDecision.objects.filter(task_id=self.pk).update(**mutation)
        body = dict(expected_version=1, reason='stop', expected_viewer_hash=self.body['expected_viewer_hash'])
        self.client.force_authenticate(self.manager)
        assert (
            self.client.post(
                self.second_base + self.pk + '/cancel/', body, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
            ).status_code
            == 409
        )

    def test_D01_D02_recovery_exact_body_namespace_and_current_owner(self):
        result, _ = self.dispatch(self.source, self.batch, {**self.body, 'assignee_id': self.worker.pk}, self.key)
        assert result.status_code == 409
        self.client.force_authenticate(self.producer)
        result = self.client.post(self.second_base, self.body, format='json', HTTP_IDEMPOTENCY_KEY=str(self.key))
        assert result.status_code == 409
        # Provider/principal identity, not just numeric actor, owns the receipt.
        real = local_principal(self.manager)
        for principal in (
            type(real)('different', real.source, real.username, real.local_user_id),
            type(real)(real.principal_id, 'different', real.username, real.local_user_id),
        ):
            with patch('review_batches.second_review_contract.resolve_actor', return_value=(principal, self.manager)):
                assert self.dispatch(self.source, self.batch, self.body, self.key)[0].status_code == 409
        pk = self.pk
        decision_key = self.key  # Separate namespace: legal once for this command.
        body = self.opinion_body(pk)
        result = self.decide(pk, body, decision_key)
        assert result.status_code == 201, result.data
        with patch('review_batches.second_review_origin.load', side_effect=AssertionError('no recovery source')):
            again = self.decide(pk, body, decision_key)
        assert again.status_code == 200 and again.data['receipt'] == result.data['receipt']

    def test_D03_recovery_after_batch_loss_and_source_flags_off_has_zero_origin_sql(self):
        ReviewBatch.objects.filter(pk=self.batch).delete()
        with self.settings(ANNOTATION_REVIEW_BATCHES_ENABLED=False), patch(
            'review_batches.second_review_origin.load', side_effect=AssertionError('no source')
        ):
            result, _ = self.dispatch(self.source, self.batch, self.body, self.key)
        assert result.status_code == 200 and result.data['source_integrity'] == 'not_checked'

    def test_D09_salt_rotation_recovers_original_viewer_body_same_owner(self):
        with self.settings(SECRET_KEY='second-review-test-salt-only'):
            result, _ = self.dispatch(self.source, self.batch, self.body, self.key)
        assert result.status_code == 200 and result.data['replayed']
        assert result.data['viewer_hash'] != self.body['expected_viewer_hash']

    def test_typed_receipts_reject_partial_owner_time_version_and_unknown_fields(self):
        row = SecondReviewTask.objects.get(pk=self.pk)
        for mutation in (
            {'version': True},
            {'version': 1.0},
            {'effect': 'blocking'},
            {'created_at': 'invalid'},
            {'owner': {'principal': 'p', 'source': 's', 'actor_id': True}},
            {'unexpected': 1},
        ):
            with self.assertRaises(APIException):
                s.checked_receipt({**row.receipt, **mutation}, 'dispatch', row, row.idempotency_key, row.request_hash)
