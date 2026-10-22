import copy
import hashlib
import json
import uuid
from pathlib import Path
from unittest.mock import patch

from data_export import approved as base
from data_export import snapshots as service
from data_export.snapshot_models import ReleaseSnapshot as Snapshot
from data_export.tests import test_approved_exports as fixture_module
from data_import.models import FileUpload
from django.core.files.base import ContentFile
from django.db import DatabaseError, transaction
from django.test import override_settings
from rest_framework.test import APITestCase
from tasks.models import Annotation, TaskAssignment
from tasks.submissions import create_submission, review_submission
from tasks.tests.factories import TaskFactory


class TestReleaseSnapshots(APITestCase):
    setUpTestData = classmethod(fixture_module.TestApprovedExports.setUpTestData.__func__)

    def setUp(self):
        fixture_module.TestApprovedExports.setUp(self)
        private = override_settings(APPROVED_EXPORT_ROOT=self.root.name + "/private")
        private.enable()
        self.addCleanup(private.disable)

    body, create, frozen, generate, detail = (
        fixture_module.TestApprovedExports.body,
        fixture_module.TestApprovedExports.create,
        fixture_module.TestApprovedExports.frozen,
        fixture_module.TestApprovedExports.generate,
        fixture_module.TestApprovedExports.detail,
    )

    def capture(self, release=None, key="snapshot-key"):
        release = release or self.frozen()
        if release.state != "ready":
            self.generate(release)
        response = self.client.post(self.snapshot_url(release), {}, format="json", HTTP_IDEMPOTENCY_KEY=key)
        assert response.status_code == 202, response.content
        snapshot = Snapshot.objects.get(pk=response.json()["snapshot_id"])
        Snapshot.objects.filter(pk=snapshot.pk).update(state="queued")
        service.generate(str(snapshot.pk), str(snapshot.token))
        snapshot.refresh_from_db()
        return snapshot

    def snapshot_url(self, release, snapshot=None, suffix=""):
        result = f"{self.url}{release.pk}/snapshots/"
        return result + (f"{snapshot.pk}/{suffix}" if snapshot else "")

    def read(self, snapshot, suffix=""):
        return self.client.get(self.snapshot_url(snapshot.release, snapshot, suffix))

    def test_snapshot_manifest_and_exact_download(self):
        snapshot = self.capture()
        assert snapshot.state == "ready", snapshot.error_code
        response = self.read(snapshot, "manifest/")
        assert response.status_code == 200, response.content
        manifest = json.loads(response.content)
        assert manifest["schema"] == "ReleaseManifest/v1"
        assert manifest["origin_mode"] == "fork_approved"
        assert manifest["content"]["media_profile"] == "no-media/v1"
        assert response["X-Manifest-SHA256"] == hashlib.sha256(response.content).hexdigest()
        assert "dataset_version_id" not in manifest
        assert len(manifest["content"]["items"]) == 3
        for artifact in manifest["artifacts"]:
            response = self.read(snapshot, f"assets/{artifact['id']}/download/")
            assert response.status_code == 200
            assert hashlib.sha256(b"".join(response.streaming_content)).hexdigest() == artifact["sha256"]
            [closer() for closer in response._resource_closers]

    def test_content_determinism_across_receipts_and_key_order(self):
        first = self.capture()
        second_release = self.frozen_from(self.sources, "another-receipt")
        second = self.capture(second_release, "another-snapshot")
        assert first.content_digest == second.content_digest
        assert first.manifest_sha256 != second.manifest_sha256
        assert service.canonical({"中文": "😀", "a": "9007199254740993"}) == service.canonical(
            {"a": "9007199254740993", "中文": "😀"}
        )
        with self.assertRaises(Exception):
            service.canonical({"id": 9007199254740993})

    def frozen_from(self, sources, key):
        from data_export.approved_models import ApprovedDatasetRelease

        response = self.create(self.body(sources), key)
        assert response.status_code == 202, response.content
        release = ApprovedDatasetRelease.objects.get(pk=response.json()["release_id"])
        self.generate(release)
        assert release.state == "ready", release.error_code
        return release

    def test_replay_and_forged_host_actor_mode_fields(self):
        snapshot = self.capture()
        url = self.snapshot_url(snapshot.release)
        response = self.client.post(url, {}, format="json", HTTP_IDEMPOTENCY_KEY="snapshot-key")
        assert response.status_code == 202
        assert response.json()["snapshot_id"] == str(snapshot.pk)
        assert Snapshot.objects.count() == 1
        for field in (
            "actor",
            "reviewer",
            "mode",
            "dataset_version_id",
            "release_manifest",
            "core_review_decision",
            "source_namespace",
            "media",
        ):
            response = self.client.post(url, {field: "forged"}, format="json", HTTP_IDEMPOTENCY_KEY="snapshot-key")
            assert response.status_code == 400, (field, response.content)

    def test_same_project_key_with_another_receipt_conflicts(self):
        self.capture()
        release = self.frozen_from(self.sources[:2], "different-selection")
        response = self.client.post(self.snapshot_url(release), {}, format="json", HTTP_IDEMPOTENCY_KEY="snapshot-key")
        assert response.status_code == 409
        assert response.json()["display_context"]["reason"] == "IDEMPOTENCY_CONFLICT"
        assert Snapshot.objects.count() == 1

    def test_ranges_and_if_range_use_verified_original_bytes(self):
        snapshot = self.capture()
        asset = service.verify(snapshot)["artifacts"][0]
        url = self.snapshot_url(snapshot.release, snapshot, f"assets/{asset['id']}/download/")
        for value in ("bytes=0-1,4-5", "bytes=999999999-", "not-a-range"):
            response = self.client.get(url, HTTP_RANGE=value)
            assert response.status_code == 416
        response = self.client.get(url, HTTP_RANGE="bytes=0-4", HTTP_IF_RANGE='"stale"')
        assert response.status_code == 200
        assert len(b"".join(response.streaming_content)) == asset["size_bytes"]
        [closer() for closer in response._resource_closers]
        response = self.client.get(url, HTTP_RANGE="bytes=0-4", HTTP_IF_RANGE=f'"{asset["sha256"]}"')
        assert response.status_code == 206
        assert len(b"".join(response.streaming_content)) == 5
        [closer() for closer in response._resource_closers]

    def test_native_artifact_corruption_prevents_manifest_and_download(self):
        snapshot = self.capture()
        other = self.capture(snapshot.release, "same-source-dependent")
        asset = next(iter(service.assets(snapshot, service.verify(snapshot)).values()))
        asset["path"].write_bytes(b"corrupt native records")
        assert self.read(snapshot, "manifest/").status_code == 409
        other.refresh_from_db()
        assert other.state == "quarantined"
        snapshot.release.refresh_from_db()
        assert snapshot.release.state == "failed_final"

    def test_frozen_authorization_receipt_loss_blocks_content(self):
        from data_export.approved_models import ApprovedDatasetReleaseAccessEvent

        snapshot = self.capture()
        manifest = service.verify(snapshot)
        ApprovedDatasetReleaseAccessEvent.objects.filter(pk=manifest["capture_authorization"]["receipt_id"]).delete()
        assert self.read(snapshot, "manifest/").status_code == 409

    def test_capture_audit_failure_cannot_commit_ready(self):
        release = self.frozen()
        self.generate(release)
        original = base.audit

        def failing(project, principal, actor, phase, **kwargs):
            if phase == "snapshot_finalize":
                raise OSError("synthetic audit unavailable")
            return original(project, principal, actor, phase, **kwargs)

        with patch.object(base, "audit", side_effect=failing):
            snapshot = self.capture(release)
        assert snapshot.state == "failed" and snapshot.manifest is None

    def test_exact_schema_hash_changes_content_and_diff(self):
        first = self.capture()
        self.project.label_config = self.project.label_config.replace(
            '<Label value="Entity"/>', '<Label value="Entity"/><Label value="Other"/>'
        )
        self.project.save(update_fields=["label_config"])
        fresh = self.make_source()
        second = self.capture(self.frozen_from([fresh], "new-schema"), "new-schema")
        assert first.content_digest != second.content_digest
        response = self.read(first, f"diff/?against={second.pk}")
        assert response.status_code == 200
        assert response.json()["schema_changed"] is True

    def test_media_upload_from_another_project_cannot_be_captured(self):
        from projects.tests.factories import ProjectFactory

        with override_settings(MEDIA_ROOT=self.root.name + "/uploads"):
            foreign = ProjectFactory(organization=self.organization)
            upload = FileUpload.objects.create(
                project=foreign, user=self.manager, file=ContentFile(b"foreign", name="foreign.png")
            )
            source = self.make_source(media=upload.url)
            snapshot = self.capture(self.frozen_from([source], "foreign-media"), "foreign-media")
            assert snapshot.state == "failed" and snapshot.error_code == "MEDIA_NOT_FOUND"

    def test_media_mutation_during_capture_is_rejected(self):
        with override_settings(MEDIA_ROOT=self.root.name + "/uploads"):
            upload = FileUpload.objects.create(
                project=self.project, user=self.manager, file=ContentFile(b"before", name="race.png")
            )
            source = self.make_source(media=upload.url)
            release = self.frozen_from([source], "race-media")
            original_digest = service.hashlib.file_digest

            def mutate(handle, algorithm):
                # file_digest is also used for #79 artifacts. Only mutate the native upload descriptor.
                import os

                if os.fstat(handle.fileno()).st_ino == Path(upload.file.path).stat().st_ino:
                    Path(upload.file.path).write_bytes(b"during")
                return original_digest(handle, algorithm)

            with patch.object(service.hashlib, "file_digest", side_effect=mutate):
                snapshot = self.capture(release, "race-media")
            assert snapshot.state == "failed"
            assert snapshot.error_code == "MEDIA_CHANGED"
            assert snapshot.manifest is None

    def test_symlinked_upload_and_pin_are_rejected(self):
        with override_settings(MEDIA_ROOT=self.root.name + "/uploads"):
            snapshot, upload = self.media_snapshot()
            pin = service.verify(snapshot)["media"][0]
            path = service.directory(snapshot) / pin["blob_id"]
            path.unlink()
            path.symlink_to(upload.file.path)
            assert self.read(snapshot, "manifest/").status_code == 409

    def test_diff_source_conflicts_and_namespace_are_not_new_versions(self):
        snapshot = self.capture()
        original = service.verify(snapshot)
        changed = copy.deepcopy(original)
        changed["content"]["items"][0]["record_sha256"] = "0" * 64
        with self.assertRaises(Exception) as conflict:
            service.difference(original, changed)
        assert str(conflict.exception.detail) == "SOURCE_IDENTITY_CONFLICT"
        changed = copy.deepcopy(original)
        changed["content"]["source_namespace"] = str(uuid.uuid4())
        with self.assertRaises(Exception) as conflict:
            service.difference(original, changed)
        assert str(conflict.exception.detail) == "SNAPSHOTS_NOT_COMPARABLE"

    def test_url_text_and_unknown_media_transforms_are_not_claimed_as_inline_text(self):
        for xml in (
            '<View><Text name="text" value="$text" valueType="url"/></View>',
            '<View><Image name="image" value="https://example.test/image.png"/></View>',
            '<View><HyperText name="html" value="$html"/></View>',
        ):
            with self.assertRaises(Exception) as denied:
                service.media_fields(xml)
            assert str(denied.exception.detail) == "MEDIA_SCHEMA_UNSUPPORTED"

    def test_filter_normalization_preserves_semantics_without_floating_ids(self):
        assert service.normalized_filters(
            {"task_id": 9223372036854775807, "submitted_from": "2026-10-22T08:00:00+08:00", "assignment_id": None}
        ) == {"task_id": "9223372036854775807", "submitted_from": "2026-10-22T00:00:00Z"}

    def test_capture_abandonment_fences_expired_worker_without_recapture(self):
        from datetime import timedelta

        from django.utils import timezone

        release = self.frozen()
        self.generate(release)
        response = self.client.post(self.snapshot_url(release), {}, format="json", HTTP_IDEMPOTENCY_KEY="interrupted")
        snapshot = Snapshot.objects.get(pk=response.json()["snapshot_id"])
        Snapshot.objects.filter(pk=snapshot.pk).update(
            state="running", started_at=timezone.now() - timedelta(minutes=32)
        )
        snapshot.refresh_from_db()
        assert self.read(snapshot).json()["retryable"] is True
        response = self.client.post(self.snapshot_url(release, snapshot, "retry/"), {}, format="json")
        assert response.status_code == 202
        snapshot.refresh_from_db()
        assert snapshot.state == "failed" and snapshot.error_code == "CAPTURE_INTERRUPTED"
        service.generate(str(snapshot.pk), str(snapshot.token))
        snapshot.refresh_from_db()
        assert snapshot.manifest is None

    def test_missing_original_upload_and_oversized_media_never_finalize(self):
        with override_settings(MEDIA_ROOT=self.root.name + "/uploads"):
            upload = FileUpload.objects.create(
                project=self.project, user=self.manager, file=ContentFile(b"media", name="missing.png")
            )
            source = self.make_source(media=upload.url)
            release = self.frozen_from([source], "original-missing")
            Path(upload.file.path).unlink()
            snapshot = self.capture(release, "original-missing")
            assert snapshot.error_code == "MEDIA_NOT_FOUND" and snapshot.manifest is None
            upload.file.save("oversized.png", ContentFile(b"media"), save=True)
            source = self.make_source(media=upload.url)
            release = self.frozen_from([source], "oversized")
            with patch.object(service, "MEDIA_LIMIT", 1):
                snapshot = self.capture(release, "oversized")
            assert snapshot.error_code == "MEDIA_LIMIT_EXCEEDED" and snapshot.manifest is None

    def test_permissions_all_content_paths_and_revocation(self):
        snapshot = self.capture()
        manifest = service.verify(snapshot)
        urls = [
            self.snapshot_url(snapshot.release),
            self.snapshot_url(snapshot.release, snapshot),
            self.snapshot_url(snapshot.release, snapshot, "manifest/"),
            self.snapshot_url(snapshot.release, snapshot, f"assets/{manifest['artifacts'][0]['id']}/download/"),
            self.snapshot_url(snapshot.release, snapshot, f"diff/?against={snapshot.pk}"),
        ]
        for user in (self.annotator, self.reviewer, self.admin):
            self.client.force_authenticate(user)
            for url in urls:
                assert self.client.get(url).status_code in (403, 404)
        self.client.force_authenticate(self.manager)
        self.manager.is_active = False
        self.manager.save(update_fields=["is_active"], session_actor=self.admin)
        for url in urls:
            assert self.client.head(url, HTTP_RANGE="bytes=0-1", HTTP_IF_NONE_MATCH='"old"').status_code in (
                401,
                403,
                404,
            )

    def test_another_creator_revoked_blocks_reader(self):
        from projects.models import ProjectMember

        snapshot = self.capture()
        ProjectMember.objects.create(
            project=self.project, user=self.annotator, role="manager", enabled=True
        ) if not ProjectMember.objects.filter(
            project=self.project, user=self.annotator
        ).exists() else ProjectMember.objects.filter(project=self.project, user=self.annotator).update(role="manager")
        self.client.force_authenticate(self.annotator)
        assert self.read(snapshot, "manifest/").status_code == 200
        self.manager.is_active = False
        self.manager.save(update_fields=["is_active"], session_actor=self.admin)
        assert self.read(snapshot, "manifest/").status_code == 403

    def test_cross_project_nested_snapshot_and_diff_ids(self):
        from projects.tests.factories import ProjectFactory

        snapshot = self.capture()
        other = ProjectFactory(organization=self.organization)
        bad = self.snapshot_url(snapshot.release, snapshot, "manifest/").replace(
            f"/projects/{self.project.pk}/", f"/projects/{other.pk}/"
        )
        assert self.client.get(bad).status_code in (403, 404)
        assert self.read(snapshot, f"diff/?against={uuid.uuid4()}").status_code == 404
        assert self.read(snapshot, "diff/?against=invalid").status_code == 400

    def test_core_only_mode_never_uses_batch_as_internal_read(self):
        snapshot = self.capture()
        policy = copy.deepcopy(self.policy)
        policy["projects"][f"{self.organization.pk}:{self.project.pk}"]["mode"] = "core_only_internal"
        with override_settings(APPROVED_EXPORT_POLICY=policy):
            for suffix in ("", "manifest/", f"diff/?against={snapshot.pk}", "retry/"):
                url = self.snapshot_url(snapshot.release, snapshot, suffix)
                for method in (self.client.get, self.client.head, self.client.options):
                    assert method(url).status_code == 403
                assert self.client.post(url, {}, format="json").status_code == 403
            assert self.client.get(f"/api/submissions/{self.sources[0].pk}/").status_code == 200
            assert (
                self.client.post(
                    self.snapshot_url(snapshot.release), {}, format="json", HTTP_IDEMPOTENCY_KEY="core"
                ).status_code
                == 403
            )

    def test_ready_and_failed_generation_cannot_be_rewritten(self):
        snapshot = self.capture()
        for update in (
            {"manifest_sha256": "0" * 64},
            {"token": uuid.uuid4()},
            {"state": "queued"},
            {"release_id": uuid.uuid4()},
            {"queued_at": service.timezone.now()},
        ):
            with self.assertRaises(DatabaseError), transaction.atomic():
                Snapshot.objects.filter(pk=snapshot.pk).update(**update)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Snapshot.objects.filter(pk=snapshot.pk).delete()
        before = bytes(snapshot.manifest)
        service.generate(str(snapshot.pk), str(snapshot.token))
        snapshot.refresh_from_db()
        assert bytes(snapshot.manifest) == before

    def test_direct_ready_insert_and_destructive_rollback_are_denied(self):
        from importlib import import_module

        from django.db import connection

        migration = import_module("data_export.migrations.0013_snapshot_freeze")
        snapshot = self.capture()
        with self.assertRaises(DatabaseError), transaction.atomic():
            Snapshot.objects.create(
                project=self.project,
                release=snapshot.release,
                actor_id=self.manager.pk,
                principal_id=snapshot.principal_id,
                idempotency_key="forged-ready",
                policy=self.policy,
                state="ready",
            )
        with self.assertRaisesRegex(RuntimeError, "forbid destructive rollback"):
            with connection.schema_editor() as editor:
                migration.uninstall(None, editor)
        assert self.read(snapshot, "manifest/").status_code == 200

    def test_empty_disposable_database_can_reverse_and_reinstall_guard(self):
        from importlib import import_module

        from django.db import connection

        migration = import_module("data_export.migrations.0013_snapshot_freeze")
        assert Snapshot.objects.count() == 0
        with connection.schema_editor() as editor:
            migration.uninstall(None, editor)
            migration.install(None, editor)
        snapshot = self.capture()
        assert snapshot.state == "ready", snapshot.error_code

    def test_queue_failure_is_recoverable_without_duplicate_capture(self):
        release = self.frozen()
        self.generate(release)
        with patch.object(base, "api_queue", side_effect=ConnectionError("isolated queue unavailable")):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    self.snapshot_url(release), {}, format="json", HTTP_IDEMPOTENCY_KEY="queue"
                )
        snapshot = Snapshot.objects.get(pk=response.json()["snapshot_id"])
        assert snapshot.state == "dispatch_pending"
        with patch.object(base, "api_queue") as queue:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(self.snapshot_url(release, snapshot, "retry/"), {}, format="json")
            assert response.status_code == 202
            assert queue.return_value.enqueue.call_count == 1
        snapshot.refresh_from_db()
        service.generate(str(snapshot.pk), str(snapshot.token))
        snapshot.refresh_from_db()
        assert snapshot.state == "ready", snapshot.error_code
        service.generate(str(snapshot.pk), str(snapshot.token))
        assert Snapshot.objects.count() == 1

    def make_source(self, *, media=None, text="New sample"):
        if media is not None:
            self.project.label_config = '<View><Image name="image" value="$image"/><Choices name="class" toName="image"><Choice value="A"/></Choices></View>'
            self.project.save(update_fields=["label_config"])
        task = TaskFactory(project=self.project, data={"image": media} if media is not None else {"text": text})
        annotation = Annotation.objects.create(task=task, project=self.project, completed_by=self.annotator, result=[])
        assignment = TaskAssignment.objects.create(
            task=task, project=self.project, annotation=annotation, assignee=self.annotator, assigned_by=self.manager
        )
        source = create_submission(assignment=assignment, annotation=annotation, actor=self.annotator)
        review_submission(submission=source, reviewer=self.reviewer, decision="approved")
        source.refresh_from_db()
        return source

    def media_snapshot(self):
        upload = FileUpload.objects.create(
            project=self.project,
            user=self.manager,
            file=ContentFile(b"synthetic-media-bytes\x00\xff", name="image.png"),
        )
        source = self.make_source(media=upload.url)
        return self.capture(self.frozen_from([source], "media-release")), upload

    def test_media_copy_hash_and_origin_mutation_after_capture(self):
        with override_settings(MEDIA_ROOT=self.root.name + "/uploads"):
            snapshot, upload = self.media_snapshot()
            assert snapshot.state == "ready", snapshot.error_code
            manifest = service.verify(snapshot)
            pin = manifest["media"][0]
            assert pin["sha256"] == hashlib.sha256(b"synthetic-media-bytes\x00\xff").hexdigest()
            assert manifest["content"]["media_freeze_semantics"] == "snapshot_capture_not_submission_time"
            Path(upload.file.path).write_bytes(b"replaced source")
            Path(upload.file.path).unlink()
            response = self.read(snapshot, f"assets/{pin['blob_id']}/download/")
            assert response.status_code == 200
            assert b"".join(response.streaming_content) == b"synthetic-media-bytes\x00\xff"
            [closer() for closer in response._resource_closers]

    def test_media_missing_foreign_remote_and_traversal_fail_closed(self):
        for index, reference in enumerate(
            (
                "/data/upload/999/missing.png",
                "https://example.test/image.png",
                "/data/upload/../secret",
                "/data/local-files/?d=image.png",
            )
        ):
            source = self.make_source(media=reference)
            snapshot = self.capture(self.frozen_from([source], f"bad-media-{index}"), f"bad-pin-{index}")
            assert snapshot.state == "failed", snapshot.error_code
            assert snapshot.manifest is None
            assert self.read(snapshot, "manifest/").status_code == 409

    def test_missing_pinned_media_quarantines_all_snapshot_content(self):
        with override_settings(MEDIA_ROOT=self.root.name + "/uploads"):
            snapshot, _ = self.media_snapshot()
            pin = service.verify(snapshot)["media"][0]
            (service.directory(snapshot) / pin["blob_id"]).unlink()
            response = self.read(snapshot, "manifest/")
            assert response.status_code == 409
            snapshot.refresh_from_db()
            assert snapshot.state == "quarantined"
            assert (
                self.client.post(
                    self.snapshot_url(snapshot.release, snapshot, "retry/"), {}, format="json"
                ).status_code
                == 409
            )

    def test_modified_pinned_media_and_symlink_refused(self):
        with override_settings(MEDIA_ROOT=self.root.name + "/uploads"):
            snapshot, upload = self.media_snapshot()
            pin = service.verify(snapshot)["media"][0]
            path = service.directory(snapshot) / pin["blob_id"]
            path.write_bytes(b"forged")
            assert self.read(snapshot, f"assets/{pin['blob_id']}/download/").status_code == 409

    def test_diff_add_remove_revision_and_schema_summary(self):
        first = self.capture()
        original = self.sources[0]
        annotation = original.assignment.annotation
        annotation.result = []
        annotation.save(update_fields=["result"])
        revised = create_submission(assignment=original.assignment, annotation=annotation, actor=self.annotator)
        review_submission(submission=revised, reviewer=self.reviewer, decision="approved")
        revised.refresh_from_db()
        added = self.make_source()
        second = self.capture(self.frozen_from([revised, self.sources[1], added], "changed"), "changed")
        response = self.read(first, f"diff/?against={second.pk}")
        assert response.status_code == 200, response.content
        assert response.json()["counts"] == {"added": 1, "removed": 1, "revised": 1}
        assert response.json()["schema_changed"] is False
        assert len(response.json()["changes"]) == 3
        assert self.read(first, f"diff/?against={first.pk}").json()["counts"] == {
            "added": 0,
            "removed": 0,
            "revised": 0,
        }

    def test_mutable_task_and_prediction_changes_do_not_change_content(self):
        snapshot = self.capture()
        blob = bytes(snapshot.manifest)
        from tasks.models import Prediction, Task

        Task.objects.filter(pk=self.sources[0].assignment.task_id).update(data={"text": "changed"})
        Prediction.objects.create(task=self.sources[0].assignment.task, project=self.project, result=[])
        assert self.read(snapshot, "manifest/").content == blob

    def test_revocation_before_worker_prevents_ready(self):
        release = self.frozen()
        self.generate(release)
        response = self.client.post(
            self.snapshot_url(release), {}, format="json", HTTP_IDEMPOTENCY_KEY="before-worker"
        )
        snapshot = Snapshot.objects.get(pk=response.json()["snapshot_id"])
        Snapshot.objects.filter(pk=snapshot.pk).update(state="queued")
        self.manager.is_active = False
        self.manager.save(update_fields=["is_active"], session_actor=self.admin)
        service.generate(str(snapshot.pk), str(snapshot.token))
        snapshot.refresh_from_db()
        assert snapshot.state == "failed"
        assert snapshot.manifest is None

    def test_diff_checks_the_other_capture_creator_after_role_revocation(self):
        from projects.models import ProjectMember

        first = self.capture()
        ProjectMember.objects.filter(project=self.project, user=self.annotator).update(role="manager")
        self.client.force_authenticate(self.annotator)
        second = self.capture(first.release, "other-capture-creator")
        self.client.force_authenticate(self.manager)
        assert self.read(first, f"diff/?against={second.pk}").status_code == 200
        ProjectMember.objects.filter(project=self.project, user=self.annotator).update(role="annotator")
        assert self.read(first, f"diff/?against={second.pk}").status_code == 403
        assert self.read(second, f"diff/?against={first.pk}").status_code == 403
        assert self.read(first, "manifest/").status_code == 200
        second.refresh_from_db()
        assert second.state == "ready"  # Authority loss is not payload corruption.

    def test_diff_reports_capture_time_media_change_and_checks_both_payloads(self):
        with override_settings(MEDIA_ROOT=self.root.name + "/uploads"):
            first, upload = self.media_snapshot()
            Path(upload.file.path).write_bytes(b"different capture-time media")
            second = self.capture(first.release, "later-media-capture")
            assert second.state == "ready", second.error_code
            assert first.content_digest != second.content_digest
            response = self.read(first, f"diff/?against={second.pk}")
            assert response.status_code == 200, response.content
            assert response.json()["counts"] == {"added": 0, "removed": 0, "revised": 1}
            change = response.json()["changes"][0]
            assert change["before"]["record_sha256"] == change["after"]["record_sha256"]
            pin = service.verify(second)["media"][0]
            (service.directory(second) / pin["blob_id"]).unlink()
            assert self.read(first, f"diff/?against={second.pk}").status_code == 409
            assert self.read(first, "manifest/").status_code == 200

    def test_native_upload_under_configured_script_prefix_preserves_original_reference(self):
        with override_settings(
            MEDIA_ROOT=self.root.name + "/uploads", FORCE_SCRIPT_NAME="/label-studio", CLOUD_FILE_STORAGE_ENABLED=False
        ):
            snapshot, upload = self.media_snapshot()
            assert snapshot.state == "ready", snapshot.error_code
            manifest = service.verify(snapshot)
            assert manifest["media"][0]["reference"] == upload.url
            assert upload.url.startswith("/label-studio/data/upload/")
            assert service.upload_for(upload.url.removeprefix("/label-studio"), self.project)[0].pk == upload.pk
            for reference in (
                upload.url.replace("/label-studio/", "/another-prefix/"),
                upload.url + "?token=secret",
                "/label-studio/data/upload/../secret",
                "/label-studio/data/upload/%2e%2e/secret",
                "https://example.test" + upload.url,
            ):
                with self.assertRaises(Exception) as denied:
                    service.upload_for(reference, self.project)
                assert str(denied.exception.detail) == "MEDIA_SOURCE_UNSUPPORTED"

    def test_corrupt_source_receipt_quarantines_ready_snapshot(self):
        from data_export.approved_models import ApprovedDatasetRelease

        snapshot = self.capture()
        ApprovedDatasetRelease.objects.filter(pk=snapshot.release_id).update(manifest=b"{}")
        assert self.read(snapshot, "manifest/").status_code == 409
        snapshot.refresh_from_db()
        assert snapshot.state == "quarantined" and snapshot.error_code == "SNAPSHOT_INTEGRITY_FAILED"
        assert self.read(snapshot, f"diff/?against={snapshot.pk}").status_code == 409

    def test_history_integrity_failure_quarantines_all_ready_dependents(self):
        from data_export.approved_models import ApprovedDatasetRelease

        first = self.capture()
        second = self.capture(first.release, "another-dependent")
        ApprovedDatasetRelease.objects.filter(pk=first.release_id).update(manifest=b"{}")
        assert self.client.get(self.snapshot_url(first.release)).status_code == 409
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.state == second.state == "quarantined"
        first.release.refresh_from_db()
        assert first.release.state == "failed_final"

    def test_queue_outage_age_does_not_expire_a_newly_enqueued_claim(self):
        from datetime import timedelta

        release = self.frozen()
        self.generate(release)
        response = self.client.post(self.snapshot_url(release), {}, format="json", HTTP_IDEMPOTENCY_KEY="outage-age")
        snapshot = Snapshot.objects.get(pk=response.json()["snapshot_id"])
        # The request was created during an outage, before any job was enqueued.
        created = snapshot.created_at
        with patch.object(service.timezone, "now", return_value=created + timedelta(minutes=40)), patch.object(base, "api_queue"):
            service.dispatch(snapshot.pk)
            snapshot.refresh_from_db()
            assert snapshot.state == "queued"
            assert self.read(snapshot).json()["retryable"] is False
            token = snapshot.token
            assert self.client.post(self.snapshot_url(release, snapshot, "retry/"), {}, format="json").status_code == 409
            snapshot.refresh_from_db()
            assert snapshot.token == token
        with patch.object(service.timezone, "now", return_value=created + timedelta(minutes=71)), patch.object(base, "api_queue"):
            assert self.read(snapshot).json()["retryable"] is True
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(self.snapshot_url(release, snapshot, "retry/"), {}, format="json")
            assert response.status_code == 202
            snapshot.refresh_from_db()
            assert snapshot.token != token
            assert snapshot.queued_at == created + timedelta(minutes=71)
            assert self.read(snapshot).json()["retryable"] is False

    def test_source_quarantined_by_legacy_export_invalidates_snapshot_history(self):
        from data_export.approved_models import ApprovedDatasetRelease

        snapshot = self.capture()
        ApprovedDatasetRelease.objects.filter(pk=snapshot.release_id).update(
            state="failed_final", error_code="ARTIFACT_INTEGRITY_FAILED"
        )
        assert self.client.get(self.snapshot_url(snapshot.release)).status_code == 409
        snapshot.refresh_from_db()
        assert snapshot.state == "quarantined"

    def test_queue_timestamp_migration_preserves_frozen_bytes_and_fences_reverse(self):
        from importlib import import_module

        from django.db import connection

        migration = import_module("data_export.migrations.0014_snapshot_queued_at")
        ready = self.capture()
        original = bytes(ready.manifest)
        response = self.client.post(self.snapshot_url(ready.release), {}, format="json", HTTP_IDEMPOTENCY_KEY="legacy-queue")
        queued = Snapshot.objects.get(pk=response.json()["snapshot_id"])
        Snapshot.objects.filter(pk=queued.pk).update(state="queued", queued_at=None)
        with connection.schema_editor() as editor:
            migration.install(None, editor)
        queued.refresh_from_db()
        assert queued.queued_at is not None
        ready.refresh_from_db()
        assert bytes(ready.manifest) == original and ready.queued_at is None
        with self.assertRaisesRegex(RuntimeError, "forbid destructive rollback"):
            with connection.schema_editor() as editor:
                migration.uninstall(None, editor)
        assert self.read(ready, "manifest/").content == original

    def test_empty_queue_timestamp_guard_can_reverse_and_reinstall(self):
        from importlib import import_module

        from django.db import connection

        migration = import_module("data_export.migrations.0014_snapshot_queued_at")
        assert Snapshot.objects.count() == 0
        with connection.schema_editor() as editor:
            migration.uninstall(None, editor)
            migration.install(None, editor)
        assert self.capture().state == "ready"
