"""Storage identity is frozen at capture and checked by background publication."""
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from data_export import audit_files, audit_format
from data_export import review_audit as audit
from data_export.audit_models import AuditArtifact, AuditPackage
from data_export.tests.test_review_audit import AuditFixtures
from django.conf import settings
from rest_framework.test import APITestCase


class TestAuditStorageBinding(AuditFixtures, APITestCase):
    def setUp(self):
        self.audit_setup()

    def queue_captured(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        with patch.object(audit.approved, 'api_queue', return_value=MagicMock()):
            self.real_enqueue(str(package.pk))
        package.refresh_from_db()
        assert package.state == 'queued'
        return package

    def test_new_queue_job_uses_versioned_worker_entry(self):
        _, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        queue = MagicMock()
        with patch.object(audit.approved, 'api_queue', return_value=queue):
            self.real_enqueue(str(package.pk))
        package.refresh_from_db()
        assert package.state == 'queued'
        args, _ = queue.enqueue.call_args
        assert args == (audit.generate_bound_v1, str(package.pk), str(package.token))

    def test_changed_root_is_blocked_before_worker_writes_and_retry_recovers(self):
        package = self.queue_captured()
        old = (package.input_sha256, package.projection_sha256)
        wrong = Path(self.private.name) / 'other-private-root'
        with self.settings(APPROVED_EXPORT_ROOT=str(wrong)):
            audit.generate(str(package.pk), str(package.token))
        package.refresh_from_db()
        assert package.state == 'blocked_runtime' and package.error_code == 'AUDIT_RUNTIME_CHANGED'
        assert not AuditArtifact.objects.filter(package=package).exists()
        assert not list(wrong.rglob('review-audit.zip'))
        with self.settings(APPROVED_EXPORT_ROOT=str(wrong)):
            rejected = self.client.post(self.audit_base + f'{package.pk}/retry/', {}, format='json',
                                        HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        assert rejected.status_code == 409
        package.refresh_from_db()
        assert package.state == 'blocked_runtime'
        response = self.client.post(self.audit_base + f'{package.pk}/retry/', {}, format='json',
                                    HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        assert response.status_code == 200, response.data
        self.ready(package)
        package.refresh_from_db()
        assert package.state == 'ready' and (package.input_sha256, package.projection_sha256) == old

    def test_changed_root_after_formatting_blocks_publish(self):
        package = self.queue_captured()
        wrong = str(Path(self.private.name) / 'other-private-root')
        original = audit_format.write
        def write_then_drift(*args, **kwargs):
            result = original(*args, **kwargs)
            settings.APPROVED_EXPORT_ROOT = wrong
            return result
        with patch.object(settings, 'APPROVED_EXPORT_ROOT', settings.APPROVED_EXPORT_ROOT), \
             patch.object(audit_format, 'write', side_effect=write_then_drift):
            audit.generate(str(package.pk), str(package.token))
        package.refresh_from_db()
        assert package.state == 'blocked_runtime' and package.error_code == 'AUDIT_RUNTIME_CHANGED'
        assert not AuditArtifact.objects.filter(package=package).exists()

    def test_binding_change_after_reservation_releases_scratch_slot(self):
        package = self.queue_captured()
        root = audit_files.private_root()
        original = audit_files.storage_binding
        calls = 0

        def drift(*args, **kwargs):
            nonlocal calls
            calls += 1
            return 'changed' if calls == 2 else original(*args, **kwargs)

        with patch.object(audit_files, 'storage_binding', side_effect=drift):
            audit.generate(str(package.pk), str(package.token))
        package.refresh_from_db()
        assert calls == 2
        assert package.state == 'blocked_runtime' and package.error_code == 'AUDIT_RUNTIME_CHANGED'
        assert not AuditArtifact.objects.filter(package=package).exists()
        assert not list((root / str(package.pk)).glob('.scratch-*'))

    def test_copied_namespace_marker_cannot_make_another_root_equivalent(self):
        package = self.queue_captured()
        original_root = audit_files.private_root()
        wrong = Path(self.private.name) / 'copied-private-root'
        with self.settings(APPROVED_EXPORT_ROOT=str(wrong)):
            copied_root = audit_files.private_root()
            shutil.copy2(original_root / '.review-audit-storage-v1', copied_root / '.review-audit-storage-v1')
            audit.generate(str(package.pk), str(package.token))
        package.refresh_from_db()
        assert package.state == 'blocked_runtime' and package.error_code == 'AUDIT_RUNTIME_CHANGED'
        assert not AuditArtifact.objects.filter(package=package).exists()

    def test_moving_the_same_directory_to_another_path_blocks_publish(self):
        package = self.queue_captured()
        original = Path(settings.APPROVED_EXPORT_ROOT)
        renamed = original.with_name('moved-private-root')
        original.rename(renamed)
        try:
            with self.settings(APPROVED_EXPORT_ROOT=str(renamed)):
                audit.generate(str(package.pk), str(package.token))
        finally:
            renamed.rename(original)
        package.refresh_from_db()
        assert package.state == 'blocked_runtime' and package.error_code == 'AUDIT_RUNTIME_CHANGED'
        assert not AuditArtifact.objects.filter(package=package).exists()

    def test_legacy_bundle_without_binding_cannot_generate_but_ready_reads_remain(self):
        package = self.queue_captured()
        actual = audit.integrity
        def legacy(row):
            value = dict(actual(row))
            value.pop('storage_binding', None)
            return value
        with patch.object(audit, 'integrity', side_effect=legacy):
            audit.generate(str(package.pk), str(package.token))
        package.refresh_from_db()
        assert package.state == 'blocked_runtime' and package.error_code == 'AUDIT_RUNTIME_CHANGED'
        assert not AuditArtifact.objects.filter(package=package).exists()
        response = self.client.post(self.audit_base + f'{package.pk}/retry/', {}, format='json',
                                    HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        assert response.status_code == 200
        self.ready(package)
        with patch.object(audit, 'integrity', side_effect=legacy):
            response = self.client.get(self.audit_base + f'{package.pk}/manifest/')
        assert response.status_code == 200
