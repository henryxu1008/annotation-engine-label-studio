import copy
from unittest.mock import patch
from uuid import uuid4

from access_control.identity import get_identity_provider
from classification_disagreements import batches
from classification_disagreements.models import ClassificationBatch
from classification_disagreements.tests import test_comparison as fixtures
from django.db import DatabaseError, connection, transaction
from django.test import override_settings
from projects.models import ProjectMember
from rest_framework.response import Response
from rest_framework.test import APITestCase
from tasks.models import Submission, Task, TaskAssignment
from tasks.submissions import _snapshot_hash


@override_settings(
    ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=True, ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=True
)
class TestClassificationBatches(APITestCase):
    setUpTestData = classmethod(fixtures.TestClassification.setUpTestData.__func__)
    make_source = fixtures.TestClassification.make_source
    set_answer = fixtures.TestClassification.set_answer

    def setUp(self):
        fixtures.TestClassification.setUp(self)
        self.base = f"/api/projects/{self.project.pk}/classification-disagreements/batches/"
        self.args = {
            "name": "分类批次 Classification",
            "scope": {"kind": "tasks", "task_ids": [self.first.assignment.task_id]},
        }

    def preview(self, args=None):
        response = self.client.post(self.base + "preview/", args or self.args, format="json")
        assert response.status_code == 200, response.data
        return response.data

    def body(self, preview=None):
        preview = preview or self.preview()
        return {"preview_token": preview["preview_token"], "expected_plan_hash": preview["plan_hash"]}

    def create(self, body=None, key=None):
        return self.client.post(
            self.base, body or self.body(), format="json", HTTP_IDEMPOTENCY_KEY=str(key or uuid4())
        )

    def new_batch(self):
        response = self.create()
        assert response.status_code == 201, response.data
        return ClassificationBatch.objects.get(pk=response.data["id"])

    def item(self, batch):
        return self.client.get(f"{self.base}{batch.pk}/items/{self.first.assignment.task_id}/")

    def test_persists_exact_results_without_copying_answers_or_writing_review(self):
        self.set_answer(self.second, "Negative")
        before = list(Submission.objects.order_by("pk").values())
        batch = self.new_batch()
        assert batch.manifest_hash == batches.digest(batch.manifest)
        assert "choices" not in str(batch.manifest) and "Synthetic 合成" not in str(batch.manifest)
        first = self.item(batch)
        assert first.status_code == 200, first.data
        assert first.data["item"]["agreement"] == 0
        assert first.data["item"]["locations"][0]["values"][1]["choices"] == ["Negative"]
        assert first["Cache-Control"] == "private, no-store" and first["Referrer-Policy"] == "no-referrer"
        assert list(Submission.objects.order_by("pk").values()) == before
        assert self.item(batch).data["item"] == first.data["item"]
        assert self.client.get(f"{self.base}{batch.pk}/").data["display_counts"] == {
            "agreement": 0,
            "disagreement": 1,
            "unavailable": 0,
        }

    def test_latest_revision_never_falls_back_and_frozen_batch_never_advances(self):
        batch = self.new_batch()
        newer = self.make_source(assignment=self.first.assignment, revision=2)
        self.set_answer(newer, "Negative", lambda snap: snap["annotation"].update(result=[]))
        assert self.item(batch).data["item"]["agreement"] == 1
        fresh = self.new_batch()
        assert self.item(fresh).data["item"]["reason"] == "missing_or_malformed"
        pins = fresh.manifest["tasks"][0]["cohort"]
        assert pins[0]["source"]["submission_id"] == newer.pk
        newer.delete()
        Submission.objects.filter(pk=self.first.pk).update(status="superseded")
        assert self.item(self.new_batch()).data["item"]["reason"] == "latest_revision_unusable"

    def test_missing_active_participant_is_not_removed_from_denominator(self):
        TaskAssignment.objects.create(project=self.project, task=self.first.assignment.task, assignee=self.manager)
        batch = self.new_batch()
        report = self.item(batch).data["item"]
        assert len(batch.manifest["tasks"][0]["cohort"]) == 3
        assert report["reason"] == "missing_submission"
        assert report["agreement"] is None and report["locations"] == []
        third = self.make_source(
            worker=self.manager,
            assignment=TaskAssignment.objects.get(task=self.first.assignment.task, assignee=self.manager),
        )
        self.set_answer(third, "Positive")
        assert self.item(batch).data["item"] == report
        assert self.item(self.new_batch()).data["item"]["agreement"] == 1

    def test_cancelled_assignments_excluded_only_from_new_batches(self):
        batch = self.new_batch()
        self.second.assignment.cancel()
        assert self.item(batch).data["item"]["agreement"] == 1
        assert self.item(self.new_batch()).data["item"]["reason"] == "insufficient_inputs"

    def test_signed_project_scope_does_not_append_new_tasks(self):
        preview = self.preview({"name": "All", "scope": {"kind": "project"}})
        task = Task.objects.create(project=self.project, data={"text": "new"})
        response = self.create(self.body(preview))
        assert response.status_code == 201, response.data
        batch = ClassificationBatch.objects.get(pk=response.data["id"])
        assert batch.manifest["task_ids"] == preview["task_ids"] and task.pk not in preview["task_ids"]

    def test_changed_revision_or_cohort_rejects_preview_without_rows(self):
        body = self.body()
        TaskAssignment.objects.create(project=self.project, task=self.first.assignment.task, assignee=self.manager)
        response = self.create(body)
        assert response.status_code == 409
        assert not ClassificationBatch.objects.exists()

    def test_original_key_recovers_after_key_rotation_expiry_and_source_deletion(self):
        body, key = self.body(), uuid4()
        first = self.create(body, key)
        assert first.status_code == 201
        self.first.delete()
        with (
            self.settings(SECRET_KEY="rotated", SECRET_KEY_FALLBACKS=[]),
            patch("django.core.signing.loads", side_effect=AssertionError("replay must not decode preview")),
        ):
            replay = self.create(body, key)
            assert replay.status_code == 200 and replay.data["replayed"] is True
            assert replay.data["id"] == first.data["id"]
        assert ClassificationBatch.objects.count() == 1
        changed = {**body, "expected_plan_hash": "0" * 64}
        assert self.create(changed, key).status_code == 409
        with patch(
            "django.core.signing.loads",
            side_effect=__import__("django.core.signing", fromlist=["SignatureExpired"]).SignatureExpired(),
        ):
            assert self.create(body).status_code == 400

    def test_source_deleted_or_corrupt_masks_frozen_answer_without_replacing_it(self):
        batch = self.new_batch()
        original = copy.deepcopy(batch.manifest)
        Submission.objects.filter(pk=self.first.pk).update(result_hash="0" * 64)
        report = self.item(batch).data["item"]
        assert report["outcome"] == "unavailable" and report["agreement"] is None
        assert report["locations"] == [] and report["inputs"] == []
        self.first.delete()
        assert self.item(batch).data["item"]["outcome"] == "unavailable"
        batch.refresh_from_db()
        assert batch.manifest == original

    def test_corrupt_source_refuses_plan_even_when_other_inputs_are_valid(self):
        Submission.objects.filter(pk=self.second.pk).update(result_hash="0" * 64)
        response = self.client.post(self.base + "preview/", self.args, format="json")
        assert response.status_code == 409 and not ClassificationBatch.objects.exists()

    def test_reviewer_self_tasks_hidden_from_objects_counts_pagination_and_hashes(self):
        batch = self.new_batch()
        self.client.force_authenticate(self.reviewer)
        listing = self.client.get(self.base)
        assert listing.status_code == 200 and listing.data["count"] == 0
        for path in (f"{batch.pk}/", f"{batch.pk}/items/", f"{batch.pk}/items/{self.first.assignment.task_id}/"):
            assert self.client.get(self.base + path).status_code == 404
        assert self.client.post(self.base + "preview/", self.args, format="json").status_code == 404

    def test_reviewer_visible_subset_has_no_full_manifest_hash(self):
        ProjectMember.objects.filter(pk=self.review_member.pk).update(role="manager")
        self.second.assignment.cancel()
        self.second.delete()
        # Manager's source is independent of the reading reviewer.
        source = self.make_source(worker=self.manager)
        source.assignment.task = self.first.assignment.task
        source.assignment.save()
        self.set_answer(source, "Negative")
        batch = self.new_batch()
        ProjectMember.objects.filter(pk=self.review_member.pk).update(role="reviewer")
        self.client.force_authenticate(self.reviewer)
        for response in (self.client.get(self.base), self.client.get(f"{self.base}{batch.pk}/"), self.item(batch)):
            assert response.status_code == 200, response.data
            assert "manifest_hash" not in str(response.data) and "projection_hash" in str(response.data)
        data = self.client.get(f"{self.base}{batch.pk}/items/").data
        assert data["count"] == 1 and data["display_counts"]["disagreement"] == 1

    def test_annotator_id_swaps_mutable_fields_disabled_modes_and_operations(self):
        batch = self.new_batch()
        body = self.body()
        paths = (
            "capability/",
            "",
            f"{batch.pk}/",
            f"{batch.pk}/items/",
            f"{batch.pk}/items/{self.first.assignment.task_id}/",
        )
        self.client.force_authenticate(self.worker)
        for path in paths:
            assert self.client.get(self.base + path).status_code in (403, 404)
        self.client.force_authenticate(self.manager)
        assert (
            self.client.get(
                self.base.replace(str(self.project.pk), str(self.other.pk), 1) + f"{batch.pk}/"
            ).status_code
            == 404
        )
        for field in ("actor", "reviewer", "result", "status"):
            assert self.create({**body, field: self.manager.pk}).status_code == 400
        for mode in ({}, {f"{self.org.pk}:{self.project.pk}": "core_only_internal"}):
            with self.settings(ANNOTATION_CLASSIFICATION_DISAGREEMENT_MODES=mode):
                for path in paths:
                    assert self.client.get(self.base + path).status_code == 404
                assert self.create(body).status_code == 404
                assert self.client.post(self.base + "preview/", self.args, format="json").status_code == 404
        with self.settings(ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=False):
            assert self.client.get(self.base).status_code == 404
        with self.settings(ANNOTATION_PROJECT_OPERATION_PROVIDER="review_batches.tests.test_batches.ReadOnlyPolicy"):
            assert self.client.get(self.base + "capability/").data["can_create"] is False
            assert self.create(body).status_code == 403
            assert self.item(batch).status_code == 200

    def test_same_role_actor_remap_and_revocation_during_serialization_discard_payload(self):
        from django.utils import timezone
        from organizations.models import OrganizationMember

        batch = self.new_batch()
        ProjectMember.objects.filter(pk=self.review_member.pk).update(role="manager")
        provider = get_identity_provider()
        original = provider.resolve
        changed = False

        def remap(request):
            nonlocal changed
            if changed:
                return type(original(request))("mapped", "mapped", self.reviewer.email, self.reviewer.pk)
            return original(request)

        project_items = batches.project_items

        def drift(*args):
            nonlocal changed
            data = project_items(*args)
            changed = True
            return data

        with patch.object(provider, "resolve", remap), patch.object(batches, "project_items", drift):
            assert self.item(batch).status_code == 409
        original_render = Response.render

        def revoke(response):
            result = original_render(response)
            OrganizationMember.objects.filter(organization=self.org, user=self.manager).update(
                deleted_at=timezone.now()
            )
            return result

        with patch.object(Response, "render", revoke):
            assert self.item(batch).status_code in (403, 404)

    def test_freeze_guards_cover_bulk_and_sql_and_allow_only_creator_null_cleanup(self):
        batch = self.new_batch()
        changes = {
            "project_id": self.other.pk,
            "creator_principal": "other",
            "actor_id_frozen": self.worker.pk,
            "created_at": batch.created_at.replace(year=2001),
            "name": "tampered",
            "idempotency_key": uuid4(),
            "request_hash": "a" * 64,
            "manifest": {},
            "manifest_hash": "b" * 64,
            "created_by_id": self.worker.pk,
        }
        for field, value in changes.items():
            with self.assertRaises(DatabaseError), transaction.atomic():
                ClassificationBatch.objects.filter(pk=batch.pk).update(**{field: value})
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE classification_disagreements_classificationbatch SET name=%s WHERE id=%s", ["bad", batch.pk]
            )
        ClassificationBatch.objects.filter(pk=batch.pk).update(created_by=None)
        batch.refresh_from_db()
        assert batch.actor_id_frozen == self.manager.pk

    def test_exact_budget_boundaries_and_no_partial_batch(self):
        # Canonical bytes, including Unicode, not Python character count.
        size = sum(len(batches.canonical(row.result_snapshot)) for row in (self.first, self.second))
        with patch.object(batches, "MAX_BYTES", size):
            assert self.create().status_code == 201
        with patch.object(batches, "MAX_BYTES", size - 1):
            assert self.client.post(self.base + "preview/", self.args, format="json").status_code == 422
        with patch.object(batches, "MAX_PARTICIPANTS", 2):
            self.preview()
        with patch.object(batches, "MAX_PARTICIPANTS", 1):
            assert self.client.post(self.base + "preview/", self.args, format="json").status_code == 422
        assert len(batches.positive_ids(list(range(1, 101)))) == 100
        response = self.client.post(
            self.base + "preview/",
            {"name": "large", "scope": {"kind": "tasks", "task_ids": list(range(1, 102))}},
            format="json",
        )
        assert response.status_code == 422
        assert ClassificationBatch.objects.count() == 1

    def test_creation_serialization_failure_rolls_back_then_original_key_can_succeed(self):
        body, key = self.body(), uuid4()
        render = Response.render

        def corrupt(response):
            result = render(response)
            if response.status_code == 201:
                response.data["name"] = "tampered"
            return result

        with patch.object(Response, "render", corrupt):
            assert self.create(body, key).status_code == 409
        assert not ClassificationBatch.objects.exists()
        assert self.create(body, key).status_code == 201

    def test_actual_eight_mebibyte_boundary_is_inclusive(self):
        snapshot = copy.deepcopy(self.first.result_snapshot)
        snapshot["task"]["meta"] = {"padding": ""}
        size = len(batches.canonical(snapshot)) + len(batches.canonical(self.second.result_snapshot))
        snapshot["task"]["meta"]["padding"] = "x" * (batches.MAX_BYTES - size)
        Submission.objects.filter(pk=self.first.pk).update(
            result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
        )
        assert self.create().status_code == 201
        snapshot["task"]["meta"]["padding"] += "x"
        Submission.objects.filter(pk=self.first.pk).update(
            result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
        )
        assert self.client.post(self.base + "preview/", self.args, format="json").status_code == 422
        assert ClassificationBatch.objects.count() == 1

    def test_actual_task_and_participant_limits_reject_without_truncating(self):
        from users.tests.factories import UserFactory

        for _ in range(18):
            TaskAssignment.objects.create(
                project=self.project,
                task=self.first.assignment.task,
                assignee=UserFactory(active_organization=self.org),
            )
        self.preview()
        TaskAssignment.objects.create(project=self.project, task=self.first.assignment.task, assignee=self.manager)
        assert self.client.post(self.base + "preview/", self.args, format="json").status_code == 422
        TaskAssignment.objects.filter(task=self.first.assignment.task, assignee=self.manager).delete()
        count = Task.objects.filter(project=self.project).count()
        Task.objects.bulk_create([Task(project=self.project, data={"text": "Synthetic"}) for _ in range(100 - count)])
        preview = self.preview({"name": "100 tasks", "scope": {"kind": "project"}})
        assert len(preview["task_ids"]) == 100
        Task.objects.create(project=self.project, data={"text": "Synthetic extra"})
        assert (
            self.client.post(
                self.base + "preview/", {"name": "101 tasks", "scope": {"kind": "project"}}, format="json"
            ).status_code
            == 422
        )
        assert not ClassificationBatch.objects.exists()

    def test_other_manager_cannot_replay_a_principal_bound_key(self):
        body, key = self.body(), uuid4()
        assert self.create(body, key).status_code == 201
        ProjectMember.objects.filter(pk=self.review_member.pk).update(role="manager")
        self.client.force_authenticate(self.reviewer)
        assert self.create(body, key).status_code == 409
        assert ClassificationBatch.objects.count() == 1

    def test_mixed_batch_counts_and_pages_exclude_the_whole_self_task(self):
        preview = self.preview({"name": "Mixed", "scope": {"kind": "project"}})
        response = self.create(self.body(preview))
        assert response.status_code == 201
        batch_id = response.data["id"]
        self.client.force_authenticate(self.reviewer)
        detail = self.client.get(f"{self.base}{batch_id}/")
        assert detail.status_code == 200 and detail.data["visible_task_count"] == 1
        assert detail.data["display_counts"] == {"agreement": 0, "disagreement": 0, "unavailable": 1}
        items = self.client.get(f"{self.base}{batch_id}/items/?state=all&page_size=1")
        assert items.data["count"] == 1 and items.data["next"] is None
        assert items.data["results"][0]["task_id"] != self.first.assignment.task_id
        assert "manifest_hash" not in detail.data

    def test_untrusted_source_owner_and_foreign_task_graph_fail_closed(self):
        Submission.objects.filter(pk=self.first.pk).update(submitted_by=self.manager)
        assert self.client.post(self.base + "preview/", self.args, format="json").status_code == 409
        Submission.objects.filter(pk=self.first.pk).update(submitted_by=self.worker)
        TaskAssignment.objects.filter(pk=self.first.assignment_id).update(project=self.other)
        assert self.client.post(self.base + "preview/", self.args, format="json").status_code == 409

    def test_manifest_integrity_and_independent_read_operation(self):
        batch = self.new_batch()
        ClassificationBatch.objects.create(
            project=self.project,
            created_by=self.manager,
            creator_principal="invalid",
            actor_id_frozen=self.manager.pk,
            name="Bad",
            idempotency_key=uuid4(),
            request_hash="0" * 64,
            manifest=batch.manifest,
            manifest_hash="0" * 64,
        )
        assert self.client.get(self.base).status_code == 409
        with self.settings(
            ANNOTATION_PROJECT_OPERATION_PROVIDER="review_batches.tests.test_batches.ReviewWithoutReadPolicy"
        ):
            assert self.item(batch).status_code == 403
            assert self.client.get(self.base + "capability/").status_code == 403

    def test_live_review_state_changes_do_not_change_frozen_calculation(self):
        from tasks.models import ReviewDecision

        batch = self.new_batch()
        first = self.item(batch).data["item"]
        ReviewDecision.objects.create(
            submission=self.first, reviewer=self.reviewer, decision="rejected", reason="Synthetic"
        )
        Submission.objects.filter(pk=self.first.pk).update(status="rejected")
        assert self.item(batch).data["item"] == first
        assert self.item(self.new_batch()).data["item"]["agreement"] == 1

    def test_two_thousand_participant_boundary_and_strict_query_ranges(self):
        from users.tests.factories import UserFactory

        actors = [self.worker, self.reviewer, *UserFactory.create_batch(18, active_organization=self.org)]
        existing = {(a.task_id, a.assignee_id) for a in TaskAssignment.objects.filter(project=self.project)}
        count = Task.objects.filter(project=self.project).count()
        Task.objects.bulk_create([Task(project=self.project, data={"text": "Synthetic"}) for _ in range(100 - count)])
        tasks = list(Task.objects.filter(project=self.project))
        TaskAssignment.objects.bulk_create(
            [
                TaskAssignment(project=self.project, task=task, assignee=actor)
                for task in tasks
                for actor in actors
                if (task.pk, actor.pk) not in existing
            ]
        )
        preview = self.preview({"name": "2,000 participants", "scope": {"kind": "project"}})
        created = self.create(self.body(preview))
        assert created.status_code == 201, created.data
        batch = ClassificationBatch.objects.get(pk=created.data["id"])
        assert sum(len(item["cohort"]) for item in batch.manifest["tasks"]) == 2000
        with patch.object(batches, "MAX_INPUTS", 1999):
            assert (
                self.client.post(
                    self.base + "preview/", {"name": "large", "scope": {"kind": "project"}}, format="json"
                ).status_code
                == 422
            )
        for query in ("page_size=101", "page=" + "9" * 5000, "page=1&page=2", "actor=1"):
            assert self.client.get(self.base + "?" + query).status_code == 400
        for ids in ([True], [2**53], [0], [1, 1]):
            assert (
                self.client.post(
                    self.base + "preview/", {"name": "bad", "scope": {"kind": "tasks", "task_ids": ids}}, format="json"
                ).status_code
                == 400
            )
