"""A NULL convenience link is evidence of loss, never permission to rebind IDs."""
import copy

from data_export import audit_contract as c
from data_export.audit_models import AuditActorBinding, AuditPackage
from django.db import connection
from rest_framework.test import APITestCase
from review_batches.models import ReviewBatch
from tasks.models import ReviewDecision, Submission
from tasks.submissions import _snapshot_hash

from .test_review_audit import AuditFixtures, denied


def values(row):
    return {field.attname: getattr(row, field.attname) for field in row._meta.fields}


class TestDetachedAudit(AuditFixtures, APITestCase):
    def setUp(self):
        self.audit_setup()

    def test_D03_same_pk_same_payload_cannot_restore_detached_subject(self):
        source, batch = self.prepared()
        assert self.dispatch(source, batch)[0].status_code == 201
        self.successor(source)
        frozen = values(source)
        source.delete()
        Submission.objects.create(**frozen)
        assert self.capture(batch).status_code == 202
        graph = c.strict(AuditPackage.objects.get().input_bundle)['graph']
        root = next(f for f in graph['facts'] if f['type'] == 'revision' and f['key'] == str(frozen['id']))
        assert root['public']['state'] == 'unavailable' and root['public']['evidence'] == 'frozen_receipt_verified'
        assert 'pins' not in root['bindings'] and root['bindings']['type_proof']

    def test_D05_same_review_pk_cannot_restore_a_null_first_review_link(self):
        source, batch = self.prepared()
        assert self.dispatch(source, batch)[0].status_code == 201
        review = ReviewDecision.objects.get(submission=source)
        frozen = values(review)
        review.delete()
        ReviewDecision.objects.create(**frozen)
        ReviewDecision.objects.filter(pk=frozen['id']).update(created_at=frozen['created_at'])
        assert self.capture(batch).status_code == 202
        records = c.strict(AuditPackage.objects.get().projection)['records']
        initial = next(row for row in records if row['type'] == 'initial_review')
        assert initial['evidence'] == 'frozen_receipt_verified' and initial['gap'] == 'live_origin_unavailable'

    def test_D01_same_root_pk_and_plan_cannot_adopt_detached_supervision(self):
        source, batch = self.prepared()
        assert self.dispatch(source, batch)[0].status_code == 201
        old = ReviewBatch.objects.get(pk=batch)
        frozen = values(old)
        old.delete()
        ReviewBatch.objects.create(**frozen)
        denied(self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (409, 422))
        assert not AuditPackage.objects.exists()

    def test_C04_live_followup_target_must_match_frozen_nine_pins(self):
        source, batch, task = self.concerned()
        target = self.successor(source)
        assert self.record_followup(self.followup_body(task, target)).status_code == 201
        snapshot = copy.deepcopy(target.result_snapshot)
        snapshot['task']['data']['text'] = 'Changed after disposition'
        Submission.objects.filter(pk=target.pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
        denied(self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (409,))
        assert not AuditPackage.objects.exists()

    def test_D05_exact_target_recreation_is_retained_unavailable(self):
        source, batch, task = self.concerned()
        target = self.successor(source)
        assert self.record_followup(self.followup_body(task, target)).status_code == 201
        frozen = values(target)
        target.delete()
        Submission.objects.create(**frozen)
        assert self.capture(batch).status_code == 202
        facts = c.strict(AuditPackage.objects.get().input_bundle)['graph']['facts']
        target = next(f for f in facts if f['type'] == 'revision' and f['key'] == str(frozen['id']))
        assert target['public']['state'] == 'unavailable' and target['bindings']['target_hash']

    def test_D05_completed_without_opinion_is_gap_or_native_guard_refusal(self):
        from review_batches.models import SecondReviewDecision

        source, batch, task = self.concerned()
        if connection.vendor == 'postgresql':
            from django.db import DatabaseError, transaction

            with self.assertRaises(DatabaseError), transaction.atomic():
                SecondReviewDecision.objects.filter(task_id_frozen=task).delete()
                with connection.cursor() as cursor:
                    cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
            return
        SecondReviewDecision.objects.filter(task_id_frozen=task).delete()
        response = self.capture(batch)
        assert response.status_code == 202, response.data
        projection = c.strict(AuditPackage.objects.get().projection)
        assert projection['gaps']['second_opinion_unavailable'] == 1
        assert next(row for row in projection['records'] if row['type'] == 'second_review')['state'] == 'completed'

    def test_H05_I03_shared_budget_rejection_is_typed_and_uncommitted(self):
        source, batch = self.prepared()
        body = self.audit_body(batch)
        ReviewDecision.objects.filter(submission=source).update(reason='x' * 9000)
        response = self.capture(batch, body=body)
        assert response.status_code == 422 and response.data['display_context']['reason'] == 'AUDIT_TOO_LARGE'
        assert response['X-Audit-Command-State'] == 'rejected'
        assert not AuditPackage.objects.exists() and not AuditActorBinding.objects.exists()
