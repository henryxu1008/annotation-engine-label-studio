import copy
from unittest.mock import patch
from uuid import uuid4

from access_control.identity import LocalIdentityProvider, Principal, get_identity_provider
from django.db import DatabaseError, transaction
from django.test import override_settings
from organizations.tests.factories import OrganizationFactory
from projects.models import ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.test import APITestCase
from review_batches import service
from review_batches.models import ReviewBatch


class ReadOnlyPolicy:
    def evaluate(self, project):
        from access_control.project_operations import READ_WORK, ProjectOperationPolicy
        return ProjectOperationPolicy('test-read-only/v1', 'paused', frozenset({READ_WORK}))
from tasks.models import Annotation, ReviewDecision, Submission, TaskAssignment
from tasks.submissions import _canonical_snapshot, _snapshot_hash
from tasks.tests.factories import TaskFactory
from users.tests.factories import UserFactory


class MappedIdentity(LocalIdentityProvider):
    actor = None

    def resolve(self, request):
        if self.actor:
            return Principal("mapped-review-test", "mapped", self.actor.email, self.actor.pk)
        return super().resolve(request)


@override_settings(ANNOTATION_REVIEW_BATCHES_ENABLED=True, ANNOTATION_FIXED_PACKAGES_ENABLED=True)
class TestReviewBatches(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.manager = cls.org.created_by
        cls.project = ProjectFactory(organization=cls.org)
        cls.worker = UserFactory(active_organization=cls.org)
        cls.reviewer = UserFactory(active_organization=cls.org)
        cls.member = ProjectMember.objects.create(project=cls.project, user=cls.worker, role="annotator")
        cls.review_member = ProjectMember.objects.create(project=cls.project, user=cls.reviewer, role="reviewer")
        cls.other = ProjectFactory(organization=cls.org)

    def setUp(self):
        self.mode = self.settings(
            ANNOTATION_REVIEW_BATCH_MODES={
                f"{self.org.pk}:{self.project.pk}": "fork_review",
                f"{self.org.pk}:{self.other.pk}": "fork_review",
            }
        )
        self.mode.enable()
        self.addCleanup(self.mode.disable)
        self.base = f"/api/projects/{self.project.pk}/review-batches/"
        self.client.force_authenticate(self.manager)
        self.source = self.make_source()
        self.args = {"name": "Review 复核", "rate": 50, "seed": "fixed", "per_submitter": False}

    def make_source(self, worker=None, project=None, revision=1, assignment=None):
        project = project or self.project
        worker = worker or self.worker
        if assignment is None:
            task = TaskFactory(project=project)
            assignment = TaskAssignment.objects.create(
                project=project, task=task, assignee=worker, assigned_by=self.manager
            )
        annotation = Annotation.objects.create(task=assignment.task, project=project, completed_by=worker, result=[])
        assignment.annotation = annotation
        assignment.save()
        snap = _canonical_snapshot(annotation, assignment)
        return Submission.objects.create(
            assignment=assignment,
            annotation=annotation,
            revision=revision,
            submitted_by=worker,
            result_snapshot=snap,
            result_hash=_snapshot_hash(snap),
        )

    def preview(self, **kwargs):
        return self.client.post(self.base + "preview/", {**self.args, **kwargs}, format="json")

    def create(self, args=None, key=None, expected=None):
        data = {**self.args, **(args or {})}
        if expected is None:
            result = self.preview(**(args or {}))
            assert result.status_code == 200, result.data
            expected = result.data["plan_hash"]
        return self.client.post(
            self.base,
            {**data, "expected_plan_hash": expected},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(key or uuid4()),
        )

    def review(self, source, batch, decision="approved"):
        self.client.force_authenticate(self.reviewer)
        return self.client.post(
            f"/api/submissions/{source.pk}/review/?review_batch={batch}",
            {"decision": decision, "reason": "Fix labels 修正标签"},
            format="json",
        )

    def test_preview_replays_exact_plan_and_per_person_rounding(self):
        other_worker = UserFactory(active_organization=self.org)
        for _ in range(4):
            self.make_source()
        self.make_source(worker=other_worker)
        first = self.preview(per_submitter=True, rate=10)
        assert first.status_code == 200
        assert first.data == self.preview(per_submitter=True, rate=10).data
        items = first.data["plan"]["candidates"]
        assert len(items) == 6
        assert sum(item["selected"] for item in items) == 2
        assert "result_snapshot" not in str(first.data)
        assert not ReviewBatch.objects.exists()

    def test_all_selected_at_100_and_unique_package_union(self):
        from task_packages.models import TaskPackage, TaskPackageItem

        for _ in range(2):
            package = TaskPackage.objects.create(
                project=self.project,
                name="P",
                creator_principal="x",
                idempotency_key=uuid4(),
                request_hash="a" * 64,
                membership_hash="b" * 64,
            )
            TaskPackageItem.objects.create(
                package=package,
                task=self.source.assignment.task,
                original_task_id=self.source.assignment.task_id,
                original_created_at=self.source.assignment.task.created_at,
            )
        result = self.preview(rate=100, package_ids=list(TaskPackage.objects.values_list("pk", flat=True)))
        assert result.status_code == 200
        assert len(result.data["plan"]["candidates"]) == 1
        assert result.data["plan"]["candidates"][0]["selected"]

    def test_latest_pending_only_and_no_approval_mutation(self):
        self.source.status = "approved"
        self.source.save()
        fresh = self.make_source(assignment=self.source.assignment, revision=2)
        result = self.preview()
        assert [item["submission_id"] for item in result.data["plan"]["candidates"]] == [fresh.pk]
        self.create()
        assert Submission.objects.get(pk=self.source.pk).status == "approved"
        assert Submission.objects.get(pk=fresh.pk).status == "pending"
        assert not ReviewDecision.objects.exists()

    def test_create_idempotent_after_source_review_and_request_conflict(self):
        preview = self.preview()
        key = uuid4()
        first = self.create(key=key, expected=preview.data["plan_hash"])
        assert first.status_code == 201
        assert self.review(self.source, first.data["id"]).status_code == 200
        self.client.force_authenticate(self.manager)
        replay = self.create(key=key, expected=preview.data["plan_hash"])
        assert replay.status_code == 200
        assert replay.data["id"] == first.data["id"]
        assert replay.data["replayed"]
        conflict = self.create(args={"name": "Changed"}, key=key, expected=preview.data["plan_hash"])
        assert conflict.status_code == 409
        assert ReviewBatch.objects.count() == 1

    def test_changed_candidates_require_new_preview(self):
        expected = self.preview().data["plan_hash"]
        self.make_source()
        assert self.create(expected=expected).status_code == 409
        assert not ReviewBatch.objects.exists()

    def test_empty_changed_pool_requires_preview(self):
        expected = self.preview().data["plan_hash"]
        self.source.status = "superseded"
        self.source.save()
        result = self.create(expected=expected)
        assert result.status_code == 409

    def test_boundary_1000_1001_never_truncates(self):
        # Distinct assignments, using existing source snapshots and real scope.
        for _ in range(999):
            self.make_source()
        assert len(self.preview().data["plan"]["candidates"]) == 1000
        self.make_source()
        result = self.preview()
        assert result.status_code == 422
        assert not ReviewBatch.objects.exists()

    def test_rate_empty_and_unknown_fields_rejected(self):
        for change in (
            {"rate": 0},
            {"rate": 101},
            {"rate": -1},
            {"seed": ""},
            {"reviewer": self.reviewer.pk},
            {"submitter_ids": [self.worker.pk, self.worker.pk]},
            {"package_ids": [999999]},
        ):
            assert self.preview(**change).status_code == 400, change
        assert self.preview(submitter_ids=[999999]).status_code == 400
        assert self.client.post(self.base, self.args, format="json").status_code == 400

    def test_manager_does_not_gain_review_role(self):
        batch = self.create().data["id"]
        assert self.client.get(f"{self.base}{batch}/queue/").status_code == 403
        result = self.client.post(
            f"/api/submissions/{self.source.pk}/review/?review_batch={batch}", {"decision": "approved"}, format="json"
        )
        assert result.status_code == 403

    def test_read_only_policy_removes_creation_and_review_actions(self):
        batch = self.create().data["id"]
        with override_settings(ANNOTATION_PROJECT_OPERATION_PROVIDER=f"{__name__}.ReadOnlyPolicy"):
            assert self.preview().status_code == 403
            self.client.force_authenticate(self.reviewer)
            detail = self.client.get(f"{self.base}{batch}/")
            assert detail.status_code == 200
            assert not detail.data["items"][0]["can_review"]
            assert self.client.get(f"{self.base}{batch}/queue/").status_code == 403
            assert self.review(self.source, batch).status_code == 403

    def test_annotator_cannot_read_or_create_batches(self):
        batch = self.create().data["id"]
        self.client.force_authenticate(self.worker)
        for url in (self.base, self.base + "capability/", f"{self.base}{batch}/", f"{self.base}{batch}/queue/"):
            assert self.client.get(url).status_code in (403, 404)
        assert self.preview().status_code in (403, 404)
        assert self.client.post(self.base, self.args, format="json").status_code in (403, 404)

    def test_cross_project_and_arbitrary_batch_ids_denied(self):
        batch = self.create().data["id"]
        self.client.force_authenticate(self.reviewer)
        url = f"/api/projects/{self.other.pk}/review-batches/{batch}/"
        assert self.client.get(url).status_code in (403, 404)
        assert self.review(self.source, 999999).status_code == 404

    def test_unselected_cannot_use_batch_review_and_cannot_release(self):
        self.make_source()
        batch = self.create().data["id"]
        data = self.client.get(f"{self.base}{batch}/").data
        unselected = next(item for item in data["items"] if not item["selected"])
        source = Submission.objects.get(pk=unselected["submission_id"])
        assert self.review(source, batch).status_code == 403
        self.client.force_authenticate(self.manager)
        assert self.client.get(f"/api/submissions/{source.pk}/release/").status_code == 400
        assert self.client.get(f"/api/projects/{self.project.pk}/export?exportType=JSON").status_code == 400

    def test_self_review_and_query_injection_denied(self):
        own = self.make_source(worker=self.reviewer)
        batch = self.create(args={"rate": 100}).data["id"]
        assert self.review(own, batch).status_code == 403
        for suffix in ("0", "-1", "abc", f"{batch}&review_batch={batch}"):
            assert self.review(self.source, suffix).status_code == 400

    def test_source_corruption_denies_preview_detail_queue_and_decision(self):
        batch = self.create().data["id"]
        Submission.objects.filter(pk=self.source.pk).update(result_snapshot={})
        assert self.client.get(f"{self.base}{batch}/").status_code == 409
        assert self.preview().status_code == 409
        self.client.force_authenticate(self.reviewer)
        assert self.client.get(f"{self.base}{batch}/queue/").status_code == 409
        assert self.review(self.source, batch).status_code == 409
        assert not ReviewDecision.objects.exists()

    def test_missing_source_preserves_denominator(self):
        batch = self.create().data["id"]
        self.source.delete()
        result = self.client.get(f"{self.base}{batch}/")
        assert result.status_code == 200
        assert result.data["candidate_count"] == 1
        assert result.data["items"][0]["status"] == "unavailable"

    def test_status_without_matching_decision_is_not_approval(self):
        batch = self.create().data['id']
        Submission.objects.filter(pk=self.source.pk).update(status='approved')
        assert self.client.get(f'{self.base}{batch}/').status_code == 409

    def test_queue_validates_actual_serialized_snapshot(self):
        from tasks.serializers import SubmissionSerializer
        batch = self.create().data['id']
        original = SubmissionSerializer.to_representation
        def corrupt(serializer, instance):
            data = original(serializer, instance)
            data['result_snapshot'] = {'tampered': True}
            return data
        self.client.force_authenticate(self.reviewer)
        with patch.object(SubmissionSerializer, 'to_representation', corrupt):
            assert self.client.get(f'{self.base}{batch}/queue/').status_code == 409

    def test_consumed_last_page_retains_frozen_denominator(self):
        for _ in range(50):
            self.make_source()
        batch = self.create(args={'rate': 100}).data['id']
        self.client.force_authenticate(self.reviewer)
        second = self.client.get(f'{self.base}{batch}/queue/?page=2')
        assert second.status_code == 200
        assert len(second.data['results']) == 1
        row = second.data['results'][0]
        source = Submission.objects.get(pk=row['id'])
        assert self.review(source, batch).status_code == 200
        exhausted = self.client.get(f'{self.base}{batch}/queue/?page=2')
        assert exhausted.status_code == 404
        assert exhausted.data['display_context']['reason'] == 'SUBMISSION_PAGE_OUT_OF_RANGE'
        first = self.client.get(f'{self.base}{batch}/queue/?page=1')
        assert len(first.data['results']) == first.data['count'] == 50
        assert self.client.get(f'{self.base}{batch}/').data['candidate_count'] == 51

    def test_frozen_plan_database_guard(self):
        batch = self.create().data["id"]
        for field, value in (("plan", {}), ("name", "changed"), ("plan_hash", "0" * 64)):
            with self.assertRaises(DatabaseError), transaction.atomic():
                ReviewBatch.objects.filter(pk=batch).update(**{field: value})

    def test_feature_off_or_core_mode_denies_new_paths(self):
        batch = self.create().data["id"]
        for modes in (
            {},
            {f"{self.org.pk}:{self.project.pk}": "core_only_internal"},
            {f"{self.org.pk}:{self.project.pk}": "unknown"},
        ):
            with override_settings(ANNOTATION_REVIEW_BATCH_MODES=modes):
                assert self.client.get(f"{self.base}{batch}/").status_code == 403
                assert self.preview().status_code == 403
                assert self.review(self.source, batch).status_code == 403
                self.client.force_authenticate(self.manager)
        with override_settings(ANNOTATION_REVIEW_BATCHES_ENABLED=False):
            assert self.client.get(self.base).status_code == 404
        # Ordinary internal source reads retain their original contract.
        assert self.client.get(f"/api/submissions/{self.source.pk}/").status_code == 200

    def test_revoke_reviewer_clears_collection_detail_queue_and_stale_decision(self):
        batch = self.create().data["id"]
        self.review_member.enabled = False
        self.review_member.save()
        self.client.force_authenticate(self.reviewer)
        for url in (self.base, f"{self.base}{batch}/", f"{self.base}{batch}/queue/"):
            assert self.client.get(url).status_code in (403, 404)
        assert self.review(self.source, batch).status_code == 403

    def test_queue_rechecks_authority_after_materialization(self):
        batch = self.create().data["id"]
        original = service.checked_sources
        calls = []

        def revoke(value):
            rows = original(value)
            calls.append(True)
            if len(calls) == 2:
                ProjectMember.objects.filter(pk=self.review_member.pk).update(enabled=False)
            return rows

        self.client.force_authenticate(self.reviewer)
        with patch.object(service, "checked_sources", side_effect=revoke):
            assert self.client.get(f"{self.base}{batch}/queue/").status_code in (403, 404)

    def test_trusted_mapped_actor_cannot_borrow_session_manager(self):
        batch = self.create().data["id"]
        with override_settings(ANNOTATION_IDENTITY_PROVIDER=f"{__name__}.MappedIdentity"):
            get_identity_provider.cache_clear()
            try:
                MappedIdentity.actor = self.reviewer
                result = self.client.get(f"{self.base}{batch}/queue/")
                assert result.status_code == 200
                assert [row["id"] for row in result.data["results"]] == [self.source.pk]
                assert self.preview().status_code in (403, 404)
                decision = self.client.post(
                    f"/api/submissions/{self.source.pk}/review/?review_batch={batch}",
                    {"decision": "approved", "reviewer": self.manager.pk},
                    format="json",
                )
                assert decision.status_code == 200
                assert ReviewDecision.objects.get(submission=self.source).reviewer_id == self.reviewer.pk
                MappedIdentity.actor = self.worker
                assert self.client.get(f"{self.base}{batch}/queue/").status_code in (403, 404)
            finally:
                MappedIdentity.actor = None
                get_identity_provider.cache_clear()

    def test_source_identity_change_and_task_relocation_denied(self):
        batch = self.create().data["id"]
        Submission.objects.filter(pk=self.source.pk).update(submitted_by=self.reviewer)
        assert self.client.get(f"{self.base}{batch}/").status_code == 409
        Submission.objects.filter(pk=self.source.pk).update(submitted_by=self.worker)
        self.source.assignment.task.project = self.other
        self.source.assignment.task.save(update_fields=["project"])
        assert self.client.get(f"{self.base}{batch}/").status_code == 409

    def test_org_revocation_and_disabled_account_deny_batch_access(self):
        from organizations.models import OrganizationMember

        batch = self.create().data["id"]
        self.client.force_authenticate(self.reviewer)
        OrganizationMember.objects.filter(organization=self.org, user=self.reviewer).update(
            deleted_at=self.source.submitted_at
        )
        assert self.client.get(f"{self.base}{batch}/").status_code in (403, 404)
        OrganizationMember.objects.filter(organization=self.org, user=self.reviewer).update(deleted_at=None)
        administrator = UserFactory(is_staff=True, is_superuser=True)
        type(self.reviewer).objects.filter(pk=self.reviewer.pk).update(is_active=False, session_actor=administrator)
        assert self.client.get(f"{self.base}{batch}/").status_code in (401, 403, 404)

    def test_rejected_rework_new_revision_and_old_evidence_unchanged(self):
        batch = self.create().data["id"]
        frozen = copy.deepcopy(self.source.result_snapshot)
        assert self.review(self.source, batch, "rejected").status_code == 200
        assert self.review(self.source, batch, "approved").status_code == 400
        self.client.force_authenticate(self.worker)
        work = self.client.get(f"/api/projects/{self.project.pk}/my-work/").data
        assert work["results"][0]["state"] == "rework"
        assignment = self.source.assignment
        assignment.refresh_from_db()
        result = self.client.patch(
            f"/api/annotations/{self.source.annotation_id}/",
            {
                "result": [],
                "assignment_id": assignment.pk,
                "assignment_version": assignment.version,
                "submit_for_review": True,
            },
            format="json",
        )
        assert result.status_code == 200, result.data
        newer = Submission.objects.get(assignment=assignment, revision=2)
        self.source.refresh_from_db()
        assert self.source.status == "rejected"
        assert self.source.result_snapshot == frozen
        assert newer.status == "pending"
        self.client.force_authenticate(self.manager)
        detail = self.client.get(f"{self.base}{batch}/").data
        assert detail["candidate_count"] == 1
        assert detail["items"][0]["successor"]["id"] == newer.pk
        assert self.review(newer, batch).status_code == 403
        assert (
            self.client.post(
                f"/api/submissions/{newer.pk}/review/", {"decision": "approved"}, format="json"
            ).status_code
            == 200
        )
        assert ReviewDecision.objects.count() == 2
