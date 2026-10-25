import copy
from unittest.mock import patch
from uuid import uuid4

from access_control.project_operations import ProjectOperationPolicy
from classification_disagreements import arbitration as a, batches
from classification_disagreements.models import ArbitrationTask
from classification_disagreements.tests import test_arbitration as fixtures
from django.db import DatabaseError, transaction
from django.test import override_settings
from projects.models import ProjectMember
from rest_framework.response import Response
from rest_framework.test import APITestCase
from review_batches import handoff as h
from review_batches.models import ReviewArbitrationHandoff
from tasks.models import ReviewDecision, Submission

from .handoff_fixtures import HandoffFixtures


class ReadOnlyPolicy:
    def evaluate(self, project):
        return ProjectOperationPolicy("handoff-read-only/v1", "active", frozenset({"read_work"}))


@override_settings(
    ANNOTATION_REVIEW_BATCHES_ENABLED=True,
    ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=True,
    ANNOTATION_CLASSIFICATION_ARBITRATION_ENABLED=True,
    ANNOTATION_REVIEW_ARBITRATION_HANDOFF_ENABLED=True,
)
class TestHandoff(HandoffFixtures, APITestCase):
    setUpTestData = classmethod(fixtures.TestArbitration.setUpTestData.__func__)

    def test_dispatch_freezes_provenance_atomically_without_changing_review(self):
        before = list(Submission.objects.order_by("pk").values())
        plan = copy.deepcopy(self.batch.plan)
        response = self.handoff()
        assert response.status_code == 201, response.data
        row = ReviewArbitrationHandoff.objects.get()
        target = ArbitrationTask.objects.get()
        assert response.data["arbitration_id"] == target.pk
        assert response.data["receipt"]["operation"] == "handoff"
        assert row.origin["review"]["candidate"]["submission_id"] == self.first.pk
        assert row.origin["analysis"]["manifest_hash"] == self.analysis.manifest_hash
        assert row.origin["target"]["origin_hash"] == target.origin_hash
        assert "Synthetic 合成" not in str(row.origin) and "choices" not in str(row.origin)
        assert list(Submission.objects.order_by("pk").values()) == before
        assert not ReviewDecision.objects.exists()
        self.batch.refresh_from_db()
        assert self.batch.plan == plan
        detail = self.handoff_detail(row.pk)
        assert detail.status_code == 200 and detail.data["available"]
        assert detail.data["source_status"] == "pending"
        assert detail["Cache-Control"] == "private, no-store" and detail["Referrer-Policy"] == "no-referrer"

    def test_duplicate_dispatch_requires_explicit_link_and_new_key_does_not_duplicate_binding(self):
        target = self.pending()
        assert self.handoff().status_code == 409
        assert not ReviewArbitrationHandoff.objects.exists()
        value, key = self.handoff_body(target), uuid4()
        first, replay = self.handoff(value, key), self.handoff(value, key)
        assert [first.status_code, replay.status_code] == [201, 200]
        assert first.data["id"] == replay.data["id"] and replay.data["replayed"]
        assert self.handoff(value).status_code == 409
        assert ArbitrationTask.objects.count() == ReviewArbitrationHandoff.objects.count() == 1

    def test_semantic_match_keeps_both_analysis_origins(self):
        self.args["name"] = "Another analysis 同输入"
        other = self.new_batch()
        created = self.dispatch(self.dispatch_body(other))
        assert created.status_code == 201, created.data
        target = ArbitrationTask.objects.get(pk=created.data["id"])
        result = self.handoff(self.handoff_body(target))
        assert result.status_code == 201, result.data
        row = ReviewArbitrationHandoff.objects.get()
        assert row.origin["analysis"]["batch_id"] == self.analysis.pk
        assert row.origin["target"]["batch_id"] == other.pk != self.analysis.pk
        assert self.handoff_detail(row.pk).data["available"]

    def test_resolved_target_stays_independent_of_source_approval(self):
        target = self.pending()
        self.client.force_authenticate(self.arbiter)
        assert self.command(target).status_code == 201
        target.refresh_from_db()
        self.client.force_authenticate(self.manager)
        linked = self.handoff(self.handoff_body(target))
        assert linked.status_code == 201, linked.data
        detail = self.handoff_detail(linked.data["id"]).data
        assert detail["arbitration_status"] == "resolved" and detail["decision"]
        assert detail["source_status"] == "pending"
        self.first.refresh_from_db()
        assert self.first.status == "pending" and not ReviewDecision.objects.exists()

    def test_link_cas_and_cancelled_target_fail_without_partial_records(self):
        target = self.pending()
        body = self.handoff_body(target)
        assert self.command(target, cancel=True).status_code == 201
        assert self.handoff(body).status_code == 409
        assert not ReviewArbitrationHandoff.objects.exists()
        fresh = self.handoff()
        assert fresh.status_code == 201, fresh.data
        assert ArbitrationTask.objects.count() == 2

    def test_original_key_recovers_after_cancel_new_revision_and_source_loss(self):
        value, key = self.handoff_body(), uuid4()
        response = self.handoff(value, key)
        assert response.status_code == 201
        target = ArbitrationTask.objects.get()
        assert self.command(target, cancel=True).status_code == 201
        self.make_source(assignment=self.first.assignment, revision=2)
        assert self.handoff(value, key).status_code == 200
        assert self.handoff(value).status_code == 409
        self.first.delete()
        self.batch.delete()
        target.delete()
        replay = self.handoff(value, key)
        assert replay.status_code == 200 and replay.data["id"] == response.data["id"]
        detail = self.handoff_detail(response.data["id"])
        assert detail.status_code == 200 and not detail.data["available"] and detail.data["decision"] is None
        assert ArbitrationTask.objects.count() == 0

    def test_changed_body_plan_hash_input_hash_and_actor_fields_reject(self):
        body = self.handoff_body()
        for name in ("actor", "reviewer", "result", "status", "viewer_hash"):
            assert self.handoff({**body, name: self.manager.pk}).status_code == 400
        for name in ("expected_plan_hash", "expected_input_set_hash"):
            assert self.handoff({**body, name: "0" * 64}).status_code == 409
        response = self.handoff(body)
        assert response.status_code == 201
        key = ReviewArbitrationHandoff.objects.get().idempotency_key
        assert self.handoff({**body, "assignee_id": self.other_arbiter.pk}, key).status_code == 409
        assert ReviewArbitrationHandoff.objects.count() == 1

    def test_existing_arbitration_key_is_not_borrowed_and_old_create_receipts_stay_compatible(self):
        key, normal = uuid4(), self.dispatch_body(self.analysis)
        created = self.dispatch(normal, key)
        assert created.status_code == 201
        assert self.handoff(self.handoff_body(), key).status_code == 409
        assert not ReviewArbitrationHandoff.objects.exists()
        replay = self.dispatch(normal, key)
        assert replay.status_code == 200 and replay.data["id"] == created.data["id"]

    def test_visibility_intersects_review_source_and_assigned_arbitration_before_counts(self):
        response = self.handoff()
        assert response.status_code == 201
        for actor in (self.other_arbiter, self.reviewer):
            self.client.force_authenticate(actor)
            rows = self.client.get(self.handoff_url)
            assert rows.status_code == 200 and rows.data["count"] == 0
            assert self.handoff_detail(response.data["id"]).status_code == 404
            assert self.client.get(self.context_url).status_code == 404
        self.client.force_authenticate(self.worker)
        assert self.client.get(self.handoff_url).status_code in (403, 404)
        self.client.force_authenticate(self.arbiter)
        rows = self.client.get(self.handoff_url)
        assert rows.status_code == 200 and rows.data["count"] == 1
        assert self.handoff_detail(response.data["id"]).data["available"]
        assert "manifest_hash" not in str(rows.data) and "cohort" not in str(rows.data)
        body = {"mode": "dispatch", "expected_plan_hash": self.batch.plan_hash,
                "expected_input_set_hash": response.data["input_set_hash"], "assignee_id": self.arbiter.pk}
        assert self.handoff(body).status_code in (403, 404)

    def test_foreign_ids_membership_revocation_and_independent_operation_denial(self):
        body, key = self.handoff_body(), uuid4()
        response = self.handoff(body, key)
        assert response.status_code == 201
        assert self.client.get(self.context_url.replace(str(self.project.pk), str(self.other.pk), 1)).status_code == 404
        with self.settings(ANNOTATION_PROJECT_OPERATION_PROVIDER=__name__ + ".ReadOnlyPolicy"):
            assert not self.client.get(self.context_url).data["can_handoff"]
            assert self.handoff(body, key).status_code == 403
            assert self.handoff_detail(response.data["id"]).status_code == 200
        ProjectMember.objects.filter(project=self.project, user=self.manager).update(enabled=False)
        assert self.handoff(body, key).status_code in (403, 404)
        assert self.handoff_detail(response.data["id"]).status_code in (403, 404)

    def test_core_only_and_disabled_modes_deny_all_new_routes_without_changing_existing_arbitration(self):
        body, key = self.handoff_body(), uuid4()
        result = self.handoff(body, key)
        assert result.status_code == 201
        for setting, value in (
            ("ANNOTATION_REVIEW_ARBITRATION_HANDOFF_ENABLED", False),
            ("ANNOTATION_REVIEW_BATCH_MODES", {f"{self.org.pk}:{self.project.pk}": "core_only_internal"}),
            ("ANNOTATION_CLASSIFICATION_ARBITRATION_MODES", {f"{self.org.pk}:{self.project.pk}": "unknown"}),
        ):
            with self.settings(**{setting: value}):
                assert self.client.get(self.context_url).status_code in (403, 404)
                assert self.client.get(self.handoff_url).status_code in (403, 404)
                assert self.handoff_detail(result.data["id"]).status_code in (403, 404)
                assert self.handoff(body, key).status_code in (403, 404)
        with self.settings(ANNOTATION_REVIEW_ARBITRATION_HANDOFF_ENABLED=False, ANNOTATION_REVIEW_BATCHES_ENABLED=False):
            assert self.client.get(f"{self.arb_base}{result.data['arbitration_id']}/").status_code == 200

    def test_uniform_and_unselected_batches_are_not_upgraded(self):
        for batch in (self.review_batch(prioritized=False), self.review_batch(rate=50)):
            source = next(c["submission_id"] for c in batch.plan["candidates"] if not c["selected"]) if "priority" in batch.plan else self.first.pk
            url = f"{self.review_base}{batch.pk}/sources/{source}/arbitration-context/"
            assert self.client.get(url).status_code == 409
        assert not ReviewArbitrationHandoff.objects.exists()

    def test_exact_budgets_reject_before_any_write_and_replay_does_not_reload_sources(self):
        body, key = self.handoff_body(), uuid4()
        snapshots = sum(len(batches.canonical(s.result_snapshot)) for s in (self.first, self.second))
        with patch.object(a, "MAX_SNAPSHOT_BYTES", snapshots - 1):
            assert self.handoff(body, key).status_code == 422
        with patch.object(a, "MAX_PARTICIPANTS", 1):
            assert self.handoff(body, key).status_code == 422
        assert not ArbitrationTask.objects.exists() and not ReviewArbitrationHandoff.objects.exists()
        with patch.object(a, "MAX_SNAPSHOT_BYTES", snapshots):
            response = self.handoff(body, key)
            assert response.status_code == 201, response.data
        with patch.object(a, "evidence", side_effect=AssertionError("Replay must not reload answers")):
            assert self.handoff(body, key).status_code == 200

    def test_context_never_loads_unrelated_candidate_snapshots(self):
        unrelated = self.make_source()
        self.batch = self.review_batch()
        self.context_url = f"{self.review_base}{self.batch.pk}/sources/{self.first.pk}/arbitration-context/"
        original = Submission.from_db.__func__

        def check(cls, database, fields, values):
            if "id" in fields and values[fields.index("id")] == unrelated.pk and "result_snapshot" in fields:
                raise AssertionError("Unrelated answer loaded")
            return original(cls, database, fields, values)

        with patch.object(Submission, "from_db", classmethod(check)):
            assert self.client.get(self.context_url).status_code == 200

    def test_response_time_revocation_and_representation_tampering_rollback_both_records(self):
        body = self.handoff_body()
        render = Response.render

        def revoke(response):
            result = render(response)
            if response.status_code == 201 and response.data.get("schema") == h.SCHEMA:
                ProjectMember.objects.filter(project=self.project, user=self.manager).update(enabled=False)
            return result

        with patch.object(Response, "render", revoke):
            assert self.handoff(body).status_code in (403, 404)
        assert not ArbitrationTask.objects.exists() and not ReviewArbitrationHandoff.objects.exists()
        assert ProjectMember.objects.get(project=self.project, user=self.manager).enabled

        def tamper(response):
            result = render(response)
            if response.status_code == 201 and response.data.get("schema") == h.SCHEMA:
                response.data["arbitration_id"] += 1
            return result

        with patch.object(Response, "render", tamper):
            assert self.handoff(body).status_code == 409
        assert not ArbitrationTask.objects.exists() and not ReviewArbitrationHandoff.objects.exists()

    def test_source_change_during_render_rolls_back_and_original_key_can_be_used(self):
        body, key = self.handoff_body(), uuid4()
        render = Response.render

        def corrupt(response):
            result = render(response)
            if response.status_code == 201 and response.data.get("schema") == h.SCHEMA:
                Submission.objects.filter(pk=self.first.pk).update(result_hash="0" * 64)
            return result

        with patch.object(Response, "render", corrupt):
            assert self.handoff(body, key).status_code == 409
        assert not ArbitrationTask.objects.exists() and not ReviewArbitrationHandoff.objects.exists()
        assert self.handoff(body, key).status_code == 201

    def test_sql_frozen_fields_and_nullable_fk_rebinding(self):
        response = self.handoff()
        assert response.status_code == 201
        row = ReviewArbitrationHandoff.objects.get()
        changes = {
            "id": row.pk + 10000, "project_id": self.other.pk,
            "review_batch_id_frozen": row.review_batch_id_frozen + 1,
            "submission_id_frozen": row.submission_id_frozen + 1,
            "arbitration_id_frozen": row.arbitration_id_frozen + 1,
            "actor_id_frozen": self.arbiter.pk, "creator_principal": "forged",
            "origin": {}, "origin_hash": "0" * 64, "idempotency_key": uuid4(), "request_hash": "0" * 64,
        }
        for field, value in changes.items():
            with self.assertRaises(DatabaseError), transaction.atomic():
                ReviewArbitrationHandoff.objects.filter(pk=row.pk).update(**{field: value})
        ReviewArbitrationHandoff.objects.filter(pk=row.pk).update(arbitration=None)
        with self.assertRaises(DatabaseError), transaction.atomic():
            ReviewArbitrationHandoff.objects.filter(pk=row.pk).update(arbitration_id=row.arbitration_id_frozen)
        assert not self.handoff_detail(row.pk).data["available"]
