import copy
import hashlib
import json
import tempfile
import uuid
import zipfile
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
        for key in ("actor", "reviewer", "mode", "label_config", "result_snapshot", "namespace", "created_by"):
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


class TestApprovedExportConcurrency(TransactionTestCase):
    def test_two_postgresql_connections_freeze_one_idempotent_receipt(self):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import close_old_connections
        from access_control.identity import Principal, LOCAL_IDENTITY_SOURCE
        from projects.models import Project
        from users.models import User

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
