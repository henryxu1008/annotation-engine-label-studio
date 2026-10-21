import copy
import hashlib
import json
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from django.db import connection, IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TransactionTestCase, override_settings
from organizations.models import OrganizationMember
from organizations.tests.factories import OrganizationFactory
from projects.models import ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.test import APITestCase
from tasks.models import Annotation, Submission, TaskAssignment
from tasks.submissions import create_submission, review_submission, _snapshot_hash
from tasks.tests.factories import TaskFactory
from users.tests.factories import UserFactory

from data_export import approved as service
from data_export.approved_models import (
    ApprovedDatasetRelease as Release,
    ApprovedDatasetReleaseAttempt as Attempt,
    ApprovedDatasetReleaseAccessEvent as Event,
)

XML = '<View><Labels name="entity" toName="text"><Label value="Entity"/></Labels><Text name="text" value="$text"/></View>'


class TestApprovedExports(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = OrganizationFactory()
        cls.manager = cls.organization.created_by
        cls.admin = UserFactory(is_staff=True, is_superuser=True)
        cls.project = ProjectFactory(organization=cls.organization, label_config=XML)
        cls.annotator = UserFactory(active_organization=cls.organization)
        cls.reviewer = UserFactory(active_organization=cls.organization)
        for user, role in [(cls.annotator, "annotator"), (cls.reviewer, "reviewer")]:
            ProjectMember.objects.create(project=cls.project, user=user, role=role)
        cls.sources = []
        for text in ["Alpha sample", "Beta sample", "Gamma sample"]:
            task = TaskFactory(project=cls.project, data={"text": text}, meta={"synthetic": True})
            annotation = Annotation.objects.create(
                task=task,
                project=cls.project,
                completed_by=cls.annotator,
                result=[
                    {
                        "id": text[0],
                        "from_name": "entity",
                        "to_name": "text",
                        "type": "labels",
                        "value": {
                            "start": 0,
                            "end": len(text.split()[0]),
                            "text": text.split()[0],
                            "labels": ["Entity"],
                        },
                    }
                ],
            )
            assignment = TaskAssignment.objects.create(
                task=task, project=cls.project, assignee=cls.annotator, assigned_by=cls.manager, annotation=annotation
            )
            source = create_submission(assignment=assignment, annotation=annotation, actor=cls.annotator)
            review_submission(submission=source, reviewer=cls.reviewer, decision="approved")
            source.refresh_from_db()
            cls.sources.append(source)

    def setUp(self):
        assert connection.vendor == "postgresql", "Acceptance must run against isolated PostgreSQL"
        self.root = tempfile.TemporaryDirectory(prefix="issue79-")
        self.policy = {
            "source_namespace": "00000000-0000-4000-8000-000000000079",
            "projects": {f"{self.organization.pk}:{self.project.pk}": {"mode": "fork_approved", "version": "test/v1"}},
        }
        self.settings_override = override_settings(
            APPROVED_EXPORT_POLICY=self.policy, APPROVED_EXPORT_ROOT=self.root.name
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.root.cleanup)
        self.queue = patch.object(service, "queue_available", return_value=True)
        self.queue.start()
        self.addCleanup(self.queue.stop)
        self.client.force_authenticate(self.manager)
        self.url = f"/api/projects/{self.project.pk}/approved-dataset-releases/"

    def body(self, sources=None, fmt="JSONL"):
        return {
            "items": [
                {"submission_id": s.pk, "revision": s.revision, "result_hash": s.result_hash}
                for s in (sources if sources is not None else self.sources)
            ],
            "output": {"format": fmt, "resource_mode": "references_only"},
            "filters": {},
        }

    def create(self, body=None, key="test-key"):
        return self.client.post(self.url, data=body or self.body(), format="json", HTTP_IDEMPOTENCY_KEY=key)

    def frozen(self, body=None):
        response = self.create(body)
        assert response.status_code == 202, response.content
        return Release.objects.get(pk=response.json()["release_id"])

    def generate(self, release):
        release.state, release.attempt_number = "queued", 1
        release.save(update_fields=["state", "attempt_number"])
        attempt = Attempt.objects.create(release=release, number=1)
        service.generate(str(release.pk), str(attempt.token))
        release.refresh_from_db()
        return attempt

    def detail(self, release, suffix=""):
        return self.url + str(release.pk) + "/" + suffix

    def test_capabilities_reject_invalid_submission_ids_without_server_errors(self):
        for value in ("abc", "", "-1", "0", "1.5", "9223372036854775808", "9" * 5000):
            with self.subTest(value=value[:30]):
                response = self.client.get(self.url + "capabilities/", {"submission_id": value})
                assert response.status_code == 400, response.content
                assert response.json()["display_context"]["reason"] == "INVALID_REQUEST"
        assert self.client.get(self.url + "capabilities/", {"submission_id": self.sources[0].pk}).status_code == 200

    def test_release_history_is_newest_first_with_a_stable_tiebreaker(self):
        from datetime import timedelta

        releases = []
        for index in range(3):
            response = self.create(key=f"history-{index}")
            assert response.status_code == 202
            release = Release.objects.get(pk=response.json()["release_id"])
            self.generate(release)
            assert release.state == "ready"
            releases.append(release)
        releases.sort(key=lambda row: row.pk)
        latest = service.timezone.now()
        Release.objects.filter(pk=releases[0].pk).update(created_at=latest - timedelta(days=1))
        Release.objects.filter(pk__in=[row.pk for row in releases[1:]]).update(created_at=latest)
        params = {"page_size": 1}
        seen = []
        while True:
            response = self.client.get(self.url, params)
            assert response.status_code == 200, response.content
            page = response.json()
            seen.extend(row["release_id"] for row in page["results"])
            if not page["next_cursor"]:
                break
            params["cursor"] = page["next_cursor"]
            assert len(seen) <= 3
        assert seen == [str(row.pk) for row in reversed(releases)]

    def test_failed_generation_discards_only_its_own_scratch_directory(self):
        ready = self.frozen()
        self.generate(ready)
        retained = {a.storage_key: service.artifact_path(a.storage_key).read_bytes() for a in ready.artifacts.all()}
        original_audit = service.audit

        def fail_publication(*args, **kwargs):
            if args[3] == "publish":
                raise OSError("synthetic final audit failure")
            return original_audit(*args, **kwargs)

        response = self.create(key="failed-scratch")
        failed = Release.objects.get(pk=response.json()["release_id"])
        with patch.object(service, "audit", side_effect=fail_publication):
            attempt = self.generate(failed)
        assert failed.state == "failed_retryable"
        assert not (Path(self.root.name) / str(failed.pk) / str(attempt.token)).exists()
        for key, blob in retained.items():
            assert service.artifact_path(key).read_bytes() == blob

    def test_superseded_worker_discards_its_files_without_touching_the_new_attempt(self):
        release = self.frozen()
        newer = Attempt.objects.create(release=release, number=2)
        newer_dir = Path(self.root.name) / str(release.pk) / str(newer.token)
        newer_dir.mkdir(parents=True)
        (newer_dir / "sentinel").write_bytes(b"newer attempt")
        original_fsync = service.os.fsync
        advanced = False

        def supersede_after_write(fd):
            nonlocal advanced
            if not advanced:
                advanced = True
                Release.objects.filter(pk=release.pk).update(state="queued", attempt_number=2)
                Attempt.objects.filter(release=release, number=1).update(state="expired")
            return original_fsync(fd)

        with patch.object(service.os, "fsync", side_effect=supersede_after_write):
            old = self.generate(release)
        assert release.state == "queued" and release.attempt_number == 2
        assert not (Path(self.root.name) / str(release.pk) / str(old.token)).exists()
        assert (newer_dir / "sentinel").read_bytes() == b"newer attempt"

    def test_json_jsonl_manifest_exact_bytes_and_private_download_range(self):
        release = self.frozen()
        self.generate(release)
        assert release.state == "ready", release.error_code
        manifest_response = self.client.get(self.detail(release, "manifest/"))
        assert manifest_response.status_code == 200
        manifest = manifest_response.json()
        assert hashlib.sha256(manifest_response.content).hexdigest() == manifest_response["X-Manifest-SHA256"]
        assert service.digest(service.canonical(manifest["input"])) == release.input_sha256
        assert service.digest(service.canonical(manifest["input"]["content"])) == release.content_sha256
        blobs = {}
        for artifact in release.artifacts.all():
            url = self.detail(release, f"artifacts/{artifact.pk}/download/")
            response = self.client.get(url)
            assert response.status_code == 200
            blob = b"".join(response.streaming_content)
            assert len(blob) == artifact.size_bytes
            assert service.digest(blob) == artifact.sha256
            assert response["Cache-Control"] == "private, no-store"
            ranged = self.client.get(url, HTTP_RANGE="bytes=3-14", HTTP_IF_RANGE=response["ETag"])
            assert ranged.status_code == 206
            assert b"".join(ranged.streaming_content) == blob[3:15]
            blobs[artifact.format] = blob
        rows = json.loads(blobs["JSON"])
        assert rows == [json.loads(line) for line in blobs["JSONL"].splitlines()]
        assert len(rows) == 3 and blobs["JSONL"].endswith(b"\n")
        assert [r["result_hash"] for r in rows] == [s.result_hash for s in self.sources]
        assert Event.objects.filter(release=release, phase="publish", allowed=True).exists()

    def test_actual_sdk_conll_conversion_and_source_index(self):
        assert "CONLL2003" in service.formats(XML)
        release = self.frozen(self.body(fmt="CONLL2003"))
        self.generate(release)
        assert release.state == "ready", release.error_code
        artifact = release.artifacts.get(format="CONLL2003")
        with zipfile.ZipFile(service.artifact_path(artifact.storage_key)) as archive:
            names = archive.namelist()
            index = json.loads(archive.read("source-index.json"))
            assert [i["source"]["id"] for i in index] == [s.pk for s in self.sources]
            corpus = "\n".join(archive.read(n).decode("utf-8") for n in names if n != "source-index.json")
            for word in ("Alpha", "Beta", "Gamma"):
                assert word in corpus
            assert "B-Entity" in corpus
        assert artifact.provenance["sdk_version"] == service.runtime("CONLL2003")["sdk_version"]

    def test_default_disabled_and_core_only_reject_all_new_routes_but_preserve_internal_read(self):
        release = self.frozen()
        self.generate(release)
        urls = [
            self.url,
            self.url + "capabilities/",
            self.url + "candidates/",
            self.detail(release),
            self.detail(release, "manifest/"),
            self.detail(release, "items/"),
        ]
        urls += [self.detail(release, f"artifacts/{a.pk}/download/") for a in release.artifacts.all()]
        for mode in ("core_only_internal", "disabled"):
            self.policy["projects"][f"{self.organization.pk}:{self.project.pk}"]["mode"] = mode
            for url in urls:
                assert self.client.get(url).status_code == 403, url
            assert self.create().status_code == 403
            assert self.client.post(self.url + "preview/", self.body(), format="json").status_code == 403
            assert self.client.post(self.detail(release, "retry/"), {}, format="json").status_code == 403
            assert self.client.get(f"/api/submissions/{self.sources[0].pk}/").status_code == 200
        with override_settings(APPROVED_EXPORT_POLICY={}):
            assert self.create().status_code == 403

    def test_reviewer_annotator_and_foreign_organization_are_denied(self):
        for user in (self.reviewer, self.annotator, UserFactory(active_organization=OrganizationFactory())):
            self.client.force_authenticate(user)
            assert self.create().status_code in (403, 404)
            assert self.client.get(self.url + "candidates/").status_code in (403, 404)

    def test_superuser_does_not_acquire_project_authority(self):
        user = UserFactory(active_organization=self.organization, is_superuser=True)
        self.client.force_authenticate(user)
        assert self.create().status_code in (403, 404)

    def test_disabled_account_and_removed_organization_creator_are_denied(self):
        self.manager.is_active = False
        self.manager.save(update_fields=["is_active"], session_actor=self.admin)
        assert self.create().status_code == 401
        from users.session_security import reactivate_accounts

        reactivate_accounts(type(self.manager).objects.filter(pk=self.manager.pk), actor=self.admin)
        self.manager.refresh_from_db()
        OrganizationMember.objects.filter(user=self.manager, organization=self.organization).update(
            deleted_at=service.timezone.now()
        )
        assert self.create().status_code == 403

    def test_spoofed_identity_mode_and_snapshots_are_rejected(self):
        for key in (
            "actor", "reviewer", "mode", "label_config", "result_snapshot", "namespace", "created_by",
            "dataset_version_id", "release_manifest", "core_review_decision",
        ):
            body = self.body()
            body[key] = "forged"
            assert self.create(body).status_code == 400
        assert Release.objects.count() == 0

    def test_foreign_or_missing_selection_rejects_whole_batch(self):
        foreign_project = ProjectFactory(organization=self.organization)
        Submission.objects.filter(pk=self.sources[0].pk).update(
            assignment=TaskAssignment.objects.create(
                project=foreign_project,
                task=TaskFactory(project=foreign_project),
                assignee=self.annotator,
                assigned_by=self.manager,
            )
        )
        assert self.create().status_code == 404
        assert Release.objects.count() == 0

    def test_pending_rejected_superseded_and_missing_review_never_export(self):
        for status in ("pending", "rejected", "superseded"):
            Submission.objects.filter(pk=self.sources[0].pk).update(status=status)
            assert self.create().status_code == 409
        Submission.objects.filter(pk=self.sources[0].pk).update(status="approved")
        self.sources[0].review.delete()
        assert self.create().status_code == 409

    def test_revision_hash_and_label_config_conflicts_fail_closed(self):
        body = self.body()
        body["items"][0]["revision"] += 1
        assert self.create(body).status_code == 409
        body = self.body()
        body["items"][0]["result_hash"] = "0" * 64
        assert self.create(body).status_code == 409
        snapshot = copy.deepcopy(self.sources[0].result_snapshot)
        snapshot["task"]["data"]["text"] = "tampered"
        Submission.objects.filter(pk=self.sources[0].pk).update(result_snapshot=snapshot)
        assert self.create().status_code == 409

    def test_mixed_xml_and_duplicate_assignments_are_rejected(self):
        source = self.sources[0]
        snapshot = copy.deepcopy(source.result_snapshot)
        snapshot["project"]["label_config"] += " "
        source.result_snapshot = snapshot
        source.result_hash = _snapshot_hash(snapshot)
        source.save()
        assert self.create(self.body()).status_code == 409

    def test_duplicate_ids_and_two_revisions_per_assignment(self):
        body = self.body()
        body["items"].append(body["items"][0])
        assert self.create(body).status_code == 400
        first = self.sources[0]
        other = Submission.objects.create(
            assignment=first.assignment,
            annotation=first.annotation,
            revision=2,
            result_snapshot=first.result_snapshot,
            result_hash=first.result_hash,
            submitted_by=self.annotator,
            status="approved",
        )
        from tasks.models import ReviewDecision

        ReviewDecision.objects.create(submission=other, reviewer=self.reviewer, decision="approved")
        assert self.create(self.body([first, other])).status_code == 400

    def test_idempotent_replay_conflict_and_database_uniqueness(self):
        release = self.frozen()
        assert self.create().json()["release_id"] == str(release.pk)
        assert self.create(self.body(self.sources[:1])).status_code == 409
        assert Release.objects.count() == 1
        with self.assertRaises(IntegrityError), transaction.atomic():
            release.pk = uuid.uuid4()
            release._state.adding = True
            release.save(force_insert=True)

    def test_queue_unavailable_and_enqueue_failure_never_sync_generate(self):
        with (
            patch.object(service, "queue_available", return_value=False),
            patch.object(service, "generate") as generate,
        ):
            assert self.create().status_code == 503
            generate.assert_not_called()
        release = self.frozen()
        with (
            patch("django_rq.get_queue", side_effect=RuntimeError("unavailable")),
            patch.object(service, "generate") as generate,
        ):
            service.dispatch(release.pk)
            generate.assert_not_called()
        release.refresh_from_db()
        assert release.state == "dispatch_pending"

    def test_worker_rechecks_creator_before_run_and_before_publish(self):
        release = self.frozen()
        self.manager.is_active = False
        self.manager.save(update_fields=["is_active"], session_actor=self.admin)
        self.generate(release)
        assert release.state == "blocked_auth" and not release.artifacts.exists()

    def test_source_deletion_stops_worker_but_retains_frozen_evidence(self):
        release = self.frozen()
        self.sources[0].assignment.delete()
        assert release.items.count() == 3
        self.generate(release)
        assert release.state == "failed_final" and release.error_code == "SOURCE_UNAVAILABLE"
        with self.assertRaises(ProtectedError):
            self.project.delete()

    def test_native_records_ignore_mutable_annotation_task_and_project_changes(self):
        release = self.frozen()
        source = self.sources[0]
        Annotation.objects.filter(pk=source.annotation_id).update(result=[])
        source.assignment.task.data = {"text": "MUTABLE"}
        source.assignment.task.save(update_fields=["data"])
        self.project.label_config = XML + " "
        self.project.save(update_fields=["label_config"])
        self.generate(release)
        assert release.state == "ready", release.error_code
        assert release.items.first().record["result_snapshot"]["task"]["data"]["text"] == "Alpha sample"

    def test_download_rechecks_revocation_and_nested_scope_even_with_old_headers(self):
        release = self.frozen()
        self.generate(release)
        artifact = release.artifacts.first()
        url = self.detail(release, f"artifacts/{artifact.pk}/download/")
        self.manager.is_active = False
        self.manager.save(update_fields=["is_active"], session_actor=self.admin)
        for method in (self.client.get, self.client.head):
            assert method(url, HTTP_RANGE="bytes=0-10", HTTP_IF_NONE_MATCH='"cached"').status_code == 401
        from users.session_security import reactivate_accounts

        reactivate_accounts(type(self.manager).objects.filter(pk=self.manager.pk), actor=self.admin)
        self.manager.refresh_from_db()
        foreign = ProjectFactory(organization=self.organization)
        other_url = url.replace(f"/projects/{self.project.pk}/", f"/projects/{foreign.pk}/")
        self.policy["projects"][f"{self.organization.pk}:{foreign.pk}"] = {
            "mode": "fork_approved",
            "version": "test/v1",
        }
        assert self.client.get(other_url).status_code == 404

    def test_policy_version_and_source_corruption_block_existing_artifacts(self):
        release = self.frozen()
        self.generate(release)
        self.policy["projects"][f"{self.organization.pk}:{self.project.pk}"]["version"] = "test/v2"
        assert self.client.get(self.detail(release, "manifest/")).status_code == 409
        self.policy["projects"][f"{self.organization.pk}:{self.project.pk}"]["version"] = "test/v1"
        Submission.objects.filter(pk=self.sources[0].pk).update(result_hash="0" * 64)
        assert self.client.get(self.detail(release, "manifest/")).status_code == 409

    def test_corrupt_artifact_returns_no_bytes_and_no_public_url(self):
        release = self.frozen()
        self.generate(release)
        artifact = release.artifacts.first()
        service.artifact_path(artifact.storage_key).write_bytes(b"bad")
        response = self.client.get(self.detail(release, f"artifacts/{artifact.pk}/download/"))
        assert response.status_code == 409
        detail = self.client.get(self.detail(release)).json()
        assert "storage_key" not in str(detail)
        assert self.client.get("/media/approved-exports/" + artifact.storage_key).status_code in (403, 404)

    def test_input_limits_unsupported_media_format_and_filters(self):
        with override_settings(APPROVED_EXPORT_LIMITS={"items": 2}):
            assert self.create().status_code == 413
        body = self.body()
        body["output"]["resource_mode"] = "download_resources"
        assert self.create(body).status_code == 422
        body = self.body()
        body["output"]["format"] = "COCO"
        assert self.create(body).status_code == 422
        body = self.body()
        body["filters"] = {"task_id": self.sources[0].assignment.task_id}
        assert self.create(body).status_code == 400

    def test_cursor_is_project_and_actor_bound(self):
        response = self.client.get(self.url + "candidates/?page_size=1")
        assert response.status_code == 200
        cursor = response.json()["next_cursor"]
        assert cursor
        assert (
            len(self.client.get(self.url + "candidates/", {"page_size": 1, "cursor": cursor}).json()["results"]) == 1
        )
        assert self.client.get(self.url + "candidates/", {"cursor": cursor + "tamper"}).status_code == 400

    def test_duplicate_worker_delivery_does_not_duplicate_artifacts(self):
        release = self.frozen()
        attempt = self.generate(release)
        before = list(release.artifacts.values_list("id", "sha256"))
        service.generate(str(release.pk), str(attempt.token))
        assert list(release.artifacts.values_list("id", "sha256")) == before

    def test_nonowner_manager_membership_revocation_blocks_queued_job_and_all_old_links(self):
        user = UserFactory(active_organization=self.organization)
        member = ProjectMember.objects.create(project=self.project, user=user, role="manager")
        self.client.force_authenticate(user)
        release = self.frozen()
        member.enabled = False
        member.save(update_fields=["enabled"])
        self.generate(release)
        assert release.state == "blocked_auth"
        self.client.force_authenticate(self.manager)
        assert self.client.get(self.detail(release, "items/")).status_code == 403
        assert self.client.post(self.detail(release, "retry/"), {}, format="json").status_code == 403

    def test_permission_revocation_after_start_prevents_ready(self):
        release = self.frozen()
        original = service.integrity

        def withdraw_after_validation(value):
            original(value)
            self.manager.is_active = False
            self.manager.save(update_fields=["is_active"], session_actor=self.admin)

        with patch.object(service, "integrity", side_effect=withdraw_after_validation):
            self.generate(release)
        assert release.state == "blocked_auth" and not release.artifacts.exists()

    def test_audit_allow_failure_prevents_publication(self):
        release = self.frozen()
        original = service.audit

        def fail_publication(*args, **kwargs):
            if args[3] == "publish":
                raise OSError("synthetic audit failure")
            return original(*args, **kwargs)

        with patch.object(service, "audit", side_effect=fail_publication):
            self.generate(release)
        assert release.state == "failed_retryable" and not release.artifacts.exists()
        assert not release.manifest

    def test_private_storage_must_be_outside_media_and_static(self):
        release = self.frozen()
        with override_settings(APPROVED_EXPORT_ROOT="/data/media/exposed", MEDIA_ROOT="/data/media"):
            self.generate(release)
        assert release.state == "failed_final" and not release.artifacts.exists()

    def test_manifest_corruption_quarantines_ready_receipt(self):
        release = self.frozen()
        self.generate(release)
        Release.objects.filter(pk=release.pk).update(manifest=b"{}")
        assert self.client.get(self.detail(release, "manifest/")).status_code == 409
        release.refresh_from_db()
        assert release.state == "failed_final"
        assert self.client.post(self.detail(release, "retry/"), {}, format="json").status_code == 409

    def test_bad_and_unsatisfiable_ranges_have_no_payload(self):
        release = self.frozen()
        self.generate(release)
        artifact = release.artifacts.first()
        url = self.detail(release, f"artifacts/{artifact.pk}/download/")
        for value in ("bytes=999999999-", "nonsense", "bytes=0-5,10-15", "bytes=-0"):
            response = self.client.get(url, HTTP_RANGE=value)
            assert response.status_code == 416 and response.content == b""
        response = self.client.get(url, HTTP_RANGE="bytes=0-999999999")
        assert response.status_code == 206
        assert len(b"".join(response.streaming_content)) == artifact.size_bytes

    def test_frozen_item_tamper_and_missing_review_block_generation(self):
        release = self.frozen()
        item = release.items.first()
        value = copy.deepcopy(item.record)
        value["revision"] += 1
        type(item).objects.filter(pk=item.pk).update(record=value)
        self.generate(release)
        assert release.state == "failed_final" and not release.artifacts.exists()

    def test_stale_attempt_cannot_claim_or_publish_after_new_attempt(self):
        release = self.frozen()
        old = Attempt.objects.create(release=release, number=1, state="expired")
        new = Attempt.objects.create(release=release, number=2)
        Release.objects.filter(pk=release.pk).update(state="queued", attempt_number=2)
        service.generate(str(release.pk), str(old.token))
        assert not release.artifacts.exists()
        service.generate(str(release.pk), str(new.token))
        release.refresh_from_db()
        assert release.state == "ready"

    def test_bio_adapter_rejects_partial_token_and_overlapping_spans(self):
        source = self.sources[0]
        snapshot = copy.deepcopy(source.result_snapshot)
        snapshot["annotation"]["result"][0]["value"].update(start=1, text="lpha")
        Submission.objects.filter(pk=source.pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
        source.refresh_from_db()
        assert self.create(self.body(fmt="CONLL2003")).status_code == 422

    def test_historical_legacy_config_hash_differences_do_not_change_xml_compatibility(self):
        source = self.sources[0]
        snapshot = copy.deepcopy(source.result_snapshot)
        snapshot["project"]["label_config_hash"] = -7900790079007900790
        Submission.objects.filter(pk=source.pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
        source.refresh_from_db()
        release = self.frozen()
        self.generate(release)
        assert release.state == "ready"
        artifact = release.artifacts.get(format="JSON")
        assert b"-7900790079007900790" in service.artifact_path(artifact.storage_key).read_bytes()

    def test_unknown_background_identity_provider_fails_closed(self):
        with patch.object(service, "get_identity_provider", return_value=object()):
            assert self.create().status_code == 403

    def test_runtime_sdk_provenance_is_not_inferred_from_version_number(self):
        with patch.object(service.importlib.metadata, "distribution") as distribution:
            distribution.return_value.version = "2.0.18"
            distribution.return_value.read_text.return_value = '{"url":"https://invalid.example/other-build"}'
            assert self.create(self.body(fmt="CONLL2003")).status_code == 409

    def test_frozen_receipt_keeps_legacy_mutable_exports_closed_after_source_cascade(self):
        from data_export.models import Export
        from io_storages.localfiles.models import LocalFilesExportStorage
        from rest_framework.exceptions import ValidationError

        release = self.frozen()
        for source in self.sources:
            source.assignment.delete()
        assert Submission.objects.filter(assignment__project=self.project).count() == 0
        assert release.items.count() == 3
        assert self.client.get(f"/api/projects/{self.project.pk}/export").status_code == 400
        snapshot = Export.objects.create(project=self.project, created_by=self.manager)
        with patch.object(snapshot, "get_export_data") as mutable_read:
            snapshot.export_to_file()
            mutable_read.assert_not_called()
        storage = LocalFilesExportStorage(project=self.project, path="/tmp/issue79-unwritten")
        with self.assertRaises(ValidationError):
            storage.require_mutable_delivery_allowed()

    def test_range_adapter_never_reads_entire_file_to_determine_size(self):
        from data_export.approved_api import BoundedRangeReader
        from unittest.mock import Mock

        with tempfile.TemporaryFile() as file:
            file.write(b"bounded")
            file.seek(0)
            wrapper = Mock(wraps=file)
            reader = BoundedRangeReader(wrapper)
            wrapper.read.assert_not_called()
            assert reader.size == 7
            assert b"".join(reader) == b"bounded"
            assert all(call.args for call in wrapper.read.call_args_list)

    def test_options_and_head_cannot_bypass_disabled_policy(self):
        with override_settings(APPROVED_EXPORT_POLICY={}):
            assert self.client.options(self.url).status_code == 403
            assert self.client.head(self.url + "candidates/").status_code == 403

    def test_organization_transfer_does_not_expose_previous_organization_receipts(self):
        from projects.models import Project

        release = self.frozen()
        destination = OrganizationFactory()
        actor = destination.created_by
        Project.objects.filter(pk=self.project.pk).update(organization=destination, created_by=actor)
        self.policy["projects"][f"{destination.pk}:{self.project.pk}"] = {
            "mode": "fork_approved",
            "version": "test/v1",
        }
        self.client.force_authenticate(actor)
        assert self.client.get(self.url).json()["results"] == []
        assert self.client.get(self.detail(release)).status_code == 404

    def test_mutable_task_reparenting_cannot_mix_project_scopes(self):
        from tasks.models import Task

        destination = ProjectFactory(organization=self.organization)
        Task.objects.filter(pk=self.sources[0].assignment.task_id).update(project=destination)
        assert self.create().status_code == 422
        assert Release.objects.count() == 0

    def test_malformed_finite_snapshot_and_invalid_date_filter_return_explicit_errors(self):
        body = self.body()
        body["filters"] = {"submitted_from": "2026-02-31T00:00:00Z"}
        assert self.create(body).status_code == 400
        source = self.sources[0]
        snapshot = copy.deepcopy(source.result_snapshot)
        snapshot["annotation"]["result"] = [{"type": "labels", "value": None}]
        Submission.objects.filter(pk=source.pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
        source.refresh_from_db()
        assert self.create().status_code == 422

    def test_same_task_different_assignments_remain_separate_standard_samples(self):
        first = self.sources[0]
        other_author = UserFactory(active_organization=self.organization)
        annotation = Annotation.objects.create(
            task=first.assignment.task,
            project=self.project,
            completed_by=other_author,
            result=first.result_snapshot["annotation"]["result"],
        )
        assignment = TaskAssignment.objects.create(
            task=first.assignment.task,
            project=self.project,
            assignee=other_author,
            assigned_by=self.manager,
            annotation=annotation,
        )
        second = create_submission(assignment=assignment, annotation=annotation, actor=other_author)
        review_submission(submission=second, reviewer=self.reviewer, decision="approved")
        release = self.frozen(self.body([first, second], fmt="CONLL2003"))
        self.generate(release)
        assert release.state == "ready", release.error_code
        assert len({item.record["task_id"] for item in release.items.all()}) == 1
        artifact = release.artifacts.get(format="CONLL2003")
        with zipfile.ZipFile(service.artifact_path(artifact.storage_key)) as archive:
            assert archive.read("result.conll").count(b"Alpha -X- _ B-Entity") == 2
            index = json.loads(archive.read("source-index.json"))
            assert len({item["sdk_task_id"] for item in index}) == 2

    def test_new_pending_revision_does_not_replace_an_approved_frozen_revision(self):
        release = self.frozen()
        first = self.sources[0]
        annotation = first.annotation
        annotation.result = []
        annotation.save(update_fields=["result"])
        create_submission(assignment=first.assignment, annotation=annotation, actor=self.annotator)
        self.generate(release)
        assert release.state == "ready"
        assert release.items.first().record["revision"] == 1
        assert release.items.first().record["result_hash"] == first.result_hash

    def test_low_scratch_budget_fails_before_converter_writes(self):
        release = self.frozen(self.body(fmt="CONLL2003"))
        with (
            override_settings(APPROVED_EXPORT_LIMITS={"scratch_bytes": 1024}),
            patch.object(service.Converter, "convert") as convert,
        ):
            self.generate(release)
            convert.assert_not_called()
        assert release.state == "failed_final" and release.error_code == "BATCH_LIMIT_EXCEEDED"

    def test_expired_worker_is_recoverable_via_status_and_authenticated_retry(self):
        from datetime import timedelta

        release = self.frozen()
        attempt = Attempt.objects.create(release=release, number=1, state="running")
        Attempt.objects.filter(pk=attempt.pk).update(heartbeat=service.timezone.now() - timedelta(hours=1))
        Release.objects.filter(pk=release.pk).update(state="running", attempt_number=1)
        assert self.client.get(self.detail(release)).json()["retryable"] is True
        before = release.input_sha256
        response = self.client.post(self.detail(release, "retry/"), {}, format="json")
        assert response.status_code == 202
        attempt.refresh_from_db()
        assert attempt.state == "expired"
        release.refresh_from_db()
        assert release.input_sha256 == before

    def test_native_writer_enforces_scratch_limit_before_large_write(self):
        release = self.frozen()
        with override_settings(APPROVED_EXPORT_LIMITS={"scratch_bytes": 64}):
            self.generate(release)
        assert release.state == "failed_final" and not release.artifacts.exists()
        from pathlib import Path

        assert sum(p.stat().st_size for p in Path(self.root.name).rglob("*") if p.is_file()) <= 64

    def test_http_queue_connection_has_bounded_timeouts_without_changing_worker_settings(self):
        from django.conf import settings

        configured = {"approved_exports": {"URL": "redis://127.0.0.1:6379/0", "DEFAULT_TIMEOUT": 1800}}
        # django-rq captures QUEUES at import time; simulate configured startup.
        with override_settings(RQ_QUEUES=configured), patch.dict("django_rq.settings.QUEUES", configured):
            before = copy.deepcopy(settings.RQ_QUEUES)
            queue = service.api_queue()
            try:
                kwargs = queue.connection.connection_pool.connection_kwargs
                assert kwargs["socket_timeout"] == 3 and kwargs["socket_connect_timeout"] == 3
                assert settings.RQ_QUEUES == before
            finally:
                queue.connection.close()

    def test_durability_barrier_failure_never_publishes_ready(self):
        release = self.frozen(self.body(fmt="CONLL2003"))
        with patch.object(service.os, "fsync", side_effect=OSError("synthetic durability failure")):
            self.generate(release)
        assert release.state == "failed_retryable" and not release.artifacts.exists() and not release.manifest

    def test_artifact_metadata_drift_cannot_override_frozen_manifest(self):
        release = self.frozen()
        self.generate(release)
        artifact = release.artifacts.get(format="JSON")
        payload = b'{"replacement":"not the approved dataset"}'
        service.artifact_path(artifact.storage_key).write_bytes(payload)
        type(artifact).objects.filter(pk=artifact.pk).update(sha256=service.digest(payload), size_bytes=len(payload))
        response = self.client.get(self.detail(release, f"artifacts/{artifact.pk}/download/"))
        assert response.status_code == 409
        release.refresh_from_db()
        assert release.state == "failed_final"
        assert self.client.post(self.detail(release, "retry/"), {}, format="json").status_code == 409

    def test_manifest_checks_complete_artifact_inventory(self):
        release = self.frozen()
        self.generate(release)
        release.artifacts.get(format="JSON").delete()
        assert self.client.get(self.detail(release, "manifest/")).status_code == 409
        release.refresh_from_db()
        assert release.state == "failed_final"

    def test_download_rejects_valid_manifest_from_another_release(self):
        release = self.frozen()
        self.generate(release)
        response = self.create(key="other-release")
        other = Release.objects.get(pk=response.json()["release_id"])
        self.generate(other)
        Release.objects.filter(pk=release.pk).update(manifest=other.manifest, manifest_sha256=other.manifest_sha256)
        artifact = release.artifacts.get(format="JSON")
        assert self.client.get(self.detail(release, f"artifacts/{artifact.pk}/download/")).status_code == 409

    def test_private_artifact_pointer_cannot_cross_release_directories(self):
        release = self.frozen()
        self.generate(release)
        artifact = release.artifacts.get(format="JSON")
        foreign_key = f"{uuid.uuid4()}/{uuid.uuid4()}/{artifact.name}"
        foreign = service.artifact_path(foreign_key)
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(service.artifact_path(artifact.storage_key).read_bytes())
        type(artifact).objects.filter(pk=artifact.pk).update(storage_key=foreign_key)
        assert self.client.get(self.detail(release, f"artifacts/{artifact.pk}/download/")).status_code == 409

    def test_artifacts_must_belong_to_the_published_attempt(self):
        release = self.frozen()
        self.generate(release)
        artifact = release.artifacts.get(format="JSON")
        stale = Attempt.objects.create(release=release, number=2, state="ready")
        type(artifact).objects.filter(pk=artifact.pk).update(attempt=stale)
        assert self.client.get(self.detail(release, f"artifacts/{artifact.pk}/download/")).status_code == 409

    def test_revoked_creator_cannot_delegate_ready_content_to_another_manager(self):
        user = UserFactory(active_organization=self.organization)
        membership = ProjectMember.objects.create(project=self.project, user=user, role="manager")
        self.client.force_authenticate(user)
        release = self.frozen()
        self.generate(release)
        artifact = release.artifacts.get(format="JSON")
        membership.enabled = False
        membership.save(update_fields=["enabled"])
        self.client.force_authenticate(self.manager)
        for suffix in ("items/", "manifest/", f"artifacts/{artifact.pk}/download/"):
            for method in (self.client.get, self.client.head):
                assert method(self.detail(release, suffix), HTTP_RANGE="bytes=0-10").status_code == 403

    def test_legacy_single_release_is_not_an_organization_revocation_bypass(self):
        user = UserFactory(active_organization=self.organization)
        ProjectMember.objects.create(project=self.project, user=user, role="manager")
        self.client.force_authenticate(user)
        url = f"/api/submissions/{self.sources[0].pk}/release/"
        assert self.client.get(url).status_code == 200
        OrganizationMember.objects.get(user=user, organization=self.organization).soft_delete()
        user.refresh_from_db()
        self.client.force_authenticate(user)
        assert self.client.get(url).status_code in (403, 404)

    def test_removed_organization_reviewer_cannot_create_approval_for_batch_release(self):
        first = self.sources[0]
        pending = create_submission(
            assignment=first.assignment, annotation=first.annotation, actor=self.annotator
        )
        OrganizationMember.objects.get(user=self.reviewer, organization=self.organization).soft_delete()
        self.reviewer.refresh_from_db()
        self.client.force_authenticate(self.reviewer)
        response = self.client.post(
            f"/api/submissions/{pending.pk}/review/",
            {"decision": "approved", "result_hash": pending.result_hash}, format="json",
        )
        assert response.status_code in (403, 404)
        pending.refresh_from_db()
        assert pending.status == "pending" and not hasattr(pending, "review")

    def test_core_only_policy_switch_during_generation_never_publishes(self):
        release = self.frozen()
        original = service.integrity

        def switch_to_core_only(value):
            original(value)
            self.policy["projects"][f"{self.organization.pk}:{self.project.pk}"]["mode"] = "core_only_internal"

        with patch.object(service, "integrity", side_effect=switch_to_core_only):
            self.generate(release)
        assert release.state == "blocked_auth" and not release.artifacts.exists() and not release.manifest
        assert self.client.get(f"/api/submissions/{self.sources[0].pk}/").status_code == 200

    @contextmanager
    def localfiles_scope(self):
        from io_storages.localfiles.models import LocalFilesImportStorage

        with override_settings(
            APPROVED_EXPORT_ROOT=str(Path(self.root.name) / "private"),
            LOCAL_FILES_SERVING_ENABLED=True,
            LOCAL_FILES_DOCUMENT_ROOT=str(Path(self.root.name).parent),
        ):
            release = self.frozen()
            self.generate(release)
            assert release.state == "ready", release.error_code
            artifact = release.artifacts.get(format="JSON")
            file = service.artifact_path(artifact.storage_key)
            other = ProjectFactory(organization=self.organization)
            user = UserFactory(active_organization=self.organization)
            ProjectMember.objects.create(project=other, user=user, role="manager")
            storage = LocalFilesImportStorage.objects.create(
                project=other, path=self.root.name, recursive_scan=True, use_blob_urls=False
            )
            storage.validate_connection()
            self.client.force_authenticate(user)
            assert self.client.get(self.detail(release, f"artifacts/{artifact.pk}/download/")).status_code == 404
            yield release, storage, file

    def localfile_url(self, file):
        return "/data/local-files/?d=" + file.relative_to(Path(self.root.name).parent).as_posix()

    def test_empty_private_root_cannot_default_to_the_working_directory(self):
        from rest_framework.exceptions import APIException

        with override_settings(APPROVED_EXPORT_ROOT=""):
            with self.assertRaises(APIException) as caught:
                service.private_root()
        assert caught.exception.status_code == 503
        assert str(caught.exception.detail) == "PRIVATE_STORAGE_REQUIRED"

    def test_localfiles_cannot_serve_foreign_private_exports_or_cached_metadata(self):
        with self.localfiles_scope() as (release, storage, file):
            self.policy["projects"][f"{self.organization.pk}:{self.project.pk}"]["mode"] = "core_only_internal"
            for method in (self.client.get, self.client.head):
                for headers in ({}, {"HTTP_RANGE": "bytes=0-10"}, {"HTTP_IF_NONE_MATCH": "*"}):
                    response = method(self.localfile_url(file), **headers)
                    if response.streaming:
                        b"".join(response.streaming_content)
                    denied = (403, 404, 405) if method.__name__ == "head" else (403, 404)
                    assert response.status_code in denied

    def test_localfiles_import_cannot_scan_or_read_private_exports(self):
        from rest_framework.exceptions import PermissionDenied

        with self.localfiles_scope() as (release, storage, file):
            assert not list(storage.iter_keys())
            for blob in (False, True):
                storage.use_blob_urls = blob
                with self.assertRaises(PermissionDenied):
                    storage.get_data(str(file))

    def test_localfiles_configuration_cannot_target_private_export_directory(self):
        from rest_framework.exceptions import PermissionDenied

        with self.localfiles_scope() as (release, storage, file):
            storage.path = str(file.parent)
            with self.assertRaises(PermissionDenied):
                storage.validate_connection()
            with self.assertRaises(PermissionDenied):
                storage.save(update_fields=["path"])

    def test_localfiles_private_symlink_is_not_a_delivery_or_import_path(self):
        from rest_framework.exceptions import PermissionDenied

        with self.localfiles_scope() as (release, storage, file):
            alias = Path(self.root.name) / "alias.json"
            alias.symlink_to(file)
            response = self.client.get(self.localfile_url(alias))
            if response.streaming:
                b"".join(response.streaming_content)
            assert response.status_code in (403, 404)
            assert not list(storage.iter_keys())
            with self.assertRaises(PermissionDenied):
                storage.get_data(str(alias))

    def test_localfiles_storage_root_is_a_directory_boundary_not_a_string_prefix(self):
        with self.localfiles_scope() as (release, storage, file):
            owned = Path(self.root.name) / "owned"
            owned.mkdir()
            own_file = owned / "public.txt"
            own_file.write_bytes(b"allowed")
            storage.path = str(owned)
            storage.save(update_fields=["path"])
            valid = self.client.get(self.localfile_url(own_file))
            assert valid.status_code == 200 and b"".join(valid.streaming_content) == b"allowed"
            sibling = Path(self.root.name) / "owned-other"
            sibling.mkdir()
            forbidden = sibling / "foreign.txt"
            forbidden.write_bytes(b"outside the authorized directory")
            response = self.client.get(self.localfile_url(forbidden))
            if response.streaming:
                b"".join(response.streaming_content)
            assert response.status_code in (403, 404)


class TestApprovedExportConcurrency(TransactionTestCase):
    def fixture(self):
        from django.urls import get_resolver

        # Standalone TransactionTestCase selections must register the same lazy
        # storage models as application startup before PostgreSQL flush runs.
        get_resolver().url_patterns
        assert connection.vendor == "postgresql"
        organization = OrganizationFactory()
        actor = organization.created_by
        project = ProjectFactory(organization=organization, label_config=XML)
        annotator = UserFactory(active_organization=organization)
        reviewer = UserFactory(active_organization=organization)
        task = TaskFactory(project=project, data={"text": "Alpha sample"})
        annotation = Annotation.objects.create(project=project, task=task, completed_by=annotator, result=[])
        assignment = TaskAssignment.objects.create(
            project=project, task=task, assignee=annotator, assigned_by=actor, annotation=annotation
        )
        source = create_submission(assignment=assignment, annotation=annotation, actor=annotator)
        review_submission(submission=source, reviewer=reviewer, decision="approved")
        body = {
            "items": [{"submission_id": source.pk, "revision": source.revision, "result_hash": source.result_hash}],
            "output": {"format": "JSONL", "resource_mode": "references_only"},
            "filters": {},
        }
        policy = {
            "source_namespace": "00000000-0000-4000-8000-000000000079",
            "projects": {f"{organization.pk}:{project.pk}": {"mode": "fork_approved", "version": "concurrency/v1"}},
        }
        return organization, actor, project, body, policy

    def test_two_postgresql_connections_freeze_one_idempotent_receipt(self):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import close_old_connections
        from access_control.identity import Principal, LOCAL_IDENTITY_SOURCE
        from projects.models import Project
        from users.models import User

        organization, actor, project, body, policy = self.fixture()

        def freeze():
            close_old_connections()
            try:
                user = User.objects.get(pk=actor.pk)
                principal = Principal(f"{LOCAL_IDENTITY_SOURCE}:{user.pk}", LOCAL_IDENTITY_SOURCE, user.email, user.pk)
                return str(
                    service.freeze(Project.objects.get(pk=project.pk), principal, user, body, "same-concurrent-key").pk
                )
            finally:
                close_old_connections()

        with (
            override_settings(APPROVED_EXPORT_POLICY=policy),
            patch.object(service, "queue_available", return_value=True),
            patch.object(service, "dispatch"),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                ids = list(pool.map(lambda _: freeze(), range(2)))
        assert len(set(ids)) == 1
        assert Release.objects.count() == 1 and Release.objects.get().items.count() == 1

    def assert_publication_fences_revocation(self, kind):
        from concurrent.futures import ThreadPoolExecutor, TimeoutError
        from threading import Event as ThreadEvent
        from django.db import close_old_connections
        from access_control.identity import Principal, LOCAL_IDENTITY_SOURCE
        from rest_framework.test import APIClient
        from users.models import User

        organization, owner, project, body, policy = self.fixture()
        administrator = UserFactory(is_staff=True, is_superuser=True)
        actor = UserFactory(active_organization=organization)
        member = ProjectMember.objects.create(project=project, user=actor, role="manager")
        principal = Principal(
            f"{LOCAL_IDENTITY_SOURCE}:{actor.pk}",
            LOCAL_IDENTITY_SOURCE,
            actor.email,
            actor.pk,
        )
        started = ThreadEvent()
        observation = {}
        original = service.audit

        def revoke():
            close_old_connections()
            try:
                if kind == "account":
                    started.set()
                    assert User.objects.filter(pk=actor.pk).update(is_active=False, session_actor=administrator) == 1
                    return 200
                if kind == "organization":
                    membership = OrganizationMember.objects.get(user_id=actor.pk, organization=organization)
                    started.set()
                    membership.soft_delete()
                    return 200
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=owner.pk))
                started.set()
                return client.patch(
                    f"/api/projects/{project.pk}/members/{member.pk}/",
                    {"enabled": False},
                    format="json",
                ).status_code
            finally:
                close_old_connections()

        with (
            tempfile.TemporaryDirectory(prefix="issue79-fence-") as root,
            override_settings(APPROVED_EXPORT_POLICY=policy, APPROVED_EXPORT_ROOT=root),
            patch.object(service, "queue_available", return_value=True),
            patch.object(service, "dispatch"),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            release = service.freeze(project, principal, actor, body, "publication-fence")
            Release.objects.filter(pk=release.pk).update(state="queued", attempt_number=1)
            attempt = Attempt.objects.create(release=release, number=1)

            def publication_gate(*args, **kwargs):
                if args[3] == "publish":
                    future = pool.submit(revoke)
                    observation["future"] = future
                    assert started.wait(5)
                    try:
                        observation["status"] = future.result(timeout=2)
                        observation["committed_before_ready"] = True
                    except TimeoutError:
                        observation["committed_before_ready"] = False
                return original(*args, **kwargs)

            with patch.object(service, "audit", side_effect=publication_gate):
                service.generate(str(release.pk), str(attempt.token))
            assert observation["future"].result(timeout=10) == 200
            release.refresh_from_db()
            assert release.state == "ready", release.error_code
            assert not observation["committed_before_ready"]
            client = APIClient()
            client.force_authenticate(owner)
            artifact = release.artifacts.get(format="JSON")
            url = (
                f"/api/projects/{project.pk}/approved-dataset-releases/{release.pk}/artifacts/{artifact.pk}/download/"
            )
            assert client.get(url).status_code == 403

    def test_project_revocation_cannot_commit_between_publication_check_and_ready(self):
        self.assert_publication_fences_revocation("project")

    def test_account_disable_cannot_commit_between_publication_check_and_ready(self):
        self.assert_publication_fences_revocation("account")

    def test_organization_removal_cannot_commit_between_publication_check_and_ready(
        self,
    ):
        self.assert_publication_fences_revocation("organization")
