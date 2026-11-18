"""Database admission, drain and unresolved outcome behavior."""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

from access_control.project_operations import local_principal
from data_export.approved_models import ApprovedDatasetRelease, ApprovedDatasetReleaseAttempt
from data_export.audit_models import AuditActorBinding, AuditAttempt, AuditPackage
from data_export.models import ConvertedFormat, Export
from data_export.snapshot_models import ReleaseSnapshot
from django.apps import apps
from django.conf import settings
from django.core import signing
from django.core.management import call_command
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import get_resolver
from django.utils import timezone
from io_storages.admission import (
    CLOUD_STORAGE_MODELS,
    LEGACY_STORAGE_MODELS,
    QUEUE_OWNER_META_KEY,
    STORAGE_EFFECT_STARTED,
    STORAGE_PRE_EFFECT,
    admitted_storage_operation,
    begin_storage_operation,
    cancel_queued_storage_dispatch,
    cancel_unsubmitted_s3_preflight,
    claim_storage_dispatch,
    finish_storage_operation,
    legacy_export_blocker_counts,
    lock_drained_project,
    prepare_storage_dispatch,
    reconcile_pending_deletion,
    reconcile_uncertain_storage_operation,
    record_storage_dispatch_job,
    recover_unstarted_storage,
)
from io_storages.admission_models import StorageOperation, StorageOperationRecovery
from io_storages.annotation_outbox import (
    annotation_export_v2,
    annotation_export_v3,
    annotation_export_v4,
    dispatch_queued_annotation_export,
)
from io_storages.azure_blob.models import AzureBlobExportStorage
from io_storages.base_models import (
    import_sync_background,
    storage_background_failure,
    storage_sync_v2,
    storage_sync_v2_failure,
    storage_sync_v3,
)
from io_storages.dispatch_recovery import redispatch_queued_operation
from io_storages.gcs.models import GCSExportStorage
from io_storages.localfiles.models import LocalFilesExportStorage, LocalFilesExportStorageLink, LocalFilesImportStorage
from io_storages.redis.models import RedisExportStorage
from io_storages.s3.models import S3ExportStorage, S3ExportStorageLink, S3ImportStorage
from io_storages.s3.version_provenance import S3ExportTargetBusy
from organizations.tests.factories import OrganizationFactory
from projects.lifecycle import transition_project
from projects.models import ProjectLifecycleEvent, ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIClient
from rq.job import Job
from tasks.models import Annotation
from tasks.tests.factories import AnnotationFactory, TaskFactory
from users.product_tours.models import UserProductTour  # noqa: F401
from users.tests.factories import UserFactory


@override_settings(ANNOTATION_PROJECT_OPERATION_PROVIDER='projects.tests.test_lifecycle_task_fence.TitlePolicy')
class StorageAdmissionTests(TransactionTestCase):
    def setUp(self):
        # Load URL modules before TransactionTestCase flush discovers tables;
        # this project registers some models lazily from its API modules.
        get_resolver().url_patterns
        self.org = OrganizationFactory()
        self.manager = self.org.created_by
        self.project = ProjectFactory(organization=self.org, title='test-active')

    def require_pg(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires PostgreSQL row locks')

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_local_export_reloads_source_after_an_older_worker_was_delayed(self):
        self.require_pg()
        task = TaskFactory(project=self.project)
        annotation = AnnotationFactory(task=task, project=self.project, result=[{'value': 'old'}])
        storage = LocalFilesExportStorage.objects.create(project=self.project, path='/tmp/issue86-ordered-export')
        loaded, release = threading.Event(), threading.Event()
        writes = []

        def old_worker():
            close_old_connections()
            try:
                old = Annotation.objects.get(pk=annotation.pk)
                loaded.set()
                if not release.wait(20):
                    raise TimeoutError('newer export did not finish')
                LocalFilesExportStorage.objects.get(pk=storage.pk).save_annotation(old)
            finally:
                connection.close()

        with patch.object(LocalFilesExportStorage, '_get_storage_path_or_raise', return_value='/tmp/issue86-ordered-export'), \
             patch.object(LocalFilesExportStorage, '_get_serialized_data', side_effect=lambda item: {'result': item.result}), \
             patch('io_storages.localfiles.models.open_local_file_write', mock_open()), \
             patch('io_storages.localfiles.models.is_private_local_path', return_value=False), \
             patch('io_storages.localfiles.models.json.dump', side_effect=lambda payload, *a, **kw: writes.append(payload)), \
             ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(old_worker)
            try:
                self.assertTrue(loaded.wait(15))
                with transaction.atomic():
                    type(self.project).objects.select_for_update().get(pk=self.project.pk)
                    Annotation.objects.filter(pk=annotation.pk).update(result=[{'value': 'new'}])
                storage.save_annotation(Annotation.objects.get(pk=annotation.pk))
            finally:
                release.set()
            future.result(30)
        self.assertEqual(writes, [{'result': [{'value': 'new'}]}, {'result': [{'value': 'new'}]}])
        self.assertEqual(StorageOperation.objects.filter(project=self.project, state='completed').count(), 2)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_local_export_does_not_write_after_annotation_is_deleted(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        Annotation.objects.filter(pk=annotation.pk).delete()
        storage = LocalFilesExportStorage.objects.create(project=self.project, path='/tmp/issue86-deleted-export')
        with patch('io_storages.localfiles.models.open_local_file_write') as write:
            storage.save_annotation(annotation)
        write.assert_not_called()
        self.assertFalse(LocalFilesExportStorageLink.objects.filter(storage=storage).exists())

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_non_s3_providers_serialize_current_annotation_after_project_lock(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project, result=[{'value': 'old'}])
        Annotation.objects.filter(pk=annotation.pk).update(result=[{'value': 'new'}])
        providers = (
            (GCSExportStorage, {'bucket': 'synthetic'}, 'get_bucket'),
            (AzureBlobExportStorage, {'container': 'synthetic'}, 'get_container'),
            (RedisExportStorage, {'host': 'synthetic'}, 'get_client'),
        )
        for model, fields, client_method in providers:
            with self.subTest(provider=model.__name__):
                storage = model.objects.create(project=self.project, **fields)
                seen = []
                with patch.object(model, client_method, return_value=MagicMock()), \
                     patch.object(model, '_get_serialized_data', side_effect=lambda item: seen.append(item.result) or {}):
                    storage.save_annotation(annotation)
                self.assertEqual(seen, [[{'value': 'new'}]])

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_non_s3_providers_do_not_open_clients_for_deleted_annotations(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        Annotation.objects.filter(pk=annotation.pk).delete()
        for model, fields, client_method in (
            (GCSExportStorage, {'bucket': 'synthetic'}, 'get_bucket'),
            (AzureBlobExportStorage, {'container': 'synthetic'}, 'get_container'),
            (RedisExportStorage, {'host': 'synthetic'}, 'get_client'),
        ):
            with self.subTest(provider=model.__name__):
                storage = model.objects.create(project=self.project, **fields)
                with patch.object(model, client_method) as client:
                    storage.save_annotation(annotation)
                client.assert_not_called()

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_storage_recovery_fences_old_claim_after_new_worker_reclaims(self):
        self.require_pg()
        from io_storages.admission import begin_claimed_storage_effect

        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-recover-sync')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        first_ready, second_ready = threading.Event(), threading.Event()
        release_first, release_second = threading.Event(), threading.Event()
        tokens = []
        mutex = threading.Lock()

        def pause(claim):
            with mutex:
                tokens.append(claim.effect_claim_token)
                first = len(tokens) == 1
            (first_ready if first else second_ready).set()
            if not (release_first if first else release_second).wait(20):
                raise TimeoutError('test did not release storage claim')
            return begin_claimed_storage_effect(claim)

        def worker(attempt):
            close_old_connections()
            try:
                storage_sync_v3(LocalFilesImportStorage, storage.pk, record.pk, 'import_sync', attempt)
            finally:
                connection.close()

        with patch('io_storages.base_models.begin_claimed_storage_effect', side_effect=pause), \
             patch('io_storages.dispatch_recovery.redispatch_queued_operation', side_effect=OSError('broker unavailable')), \
             patch.object(LocalFilesImportStorage, 'scan_and_create_links') as scan, \
             ThreadPoolExecutor(max_workers=2) as pool:
            old = pool.submit(worker, 0)
            try:
                self.assertTrue(first_ready.wait(15))
                recover_unstarted_storage(self.project.pk, record.pk, local_principal(self.manager), reason='worker stopped')
                record.refresh_from_db()
                self.assertEqual((record.state, record.dispatch_attempts), (StorageOperation.State.QUEUED, 1))
                new = pool.submit(worker, 1)
                self.assertTrue(second_ready.wait(15))
                self.assertNotEqual(tokens[0], tokens[1])
                release_first.set()
                with self.assertRaises(ValidationError):
                    old.result(15)
                scan.assert_not_called()
                release_second.set()
                new.result(15)
            finally:
                release_first.set()
                release_second.set()
        scan.assert_called_once()
        self.assertEqual(StorageOperationRecovery.objects.filter(operation=record).count(), 1)
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.COMPLETED)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_annotation_export_recovery_fences_old_claim_and_preserves_delivery(self):
        self.require_pg()
        from io_storages.admission import begin_claimed_storage_effect

        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = LocalFilesExportStorage.objects.create(project=self.project, path='/tmp/issue86-recover-annotation')
        record = StorageOperation.objects.create(
            project=self.project, storage_model=storage._meta.label_lower, storage_id=storage.pk,
            operation='annotation_export_v3_queued', lifecycle_revision=0, subject_id=annotation.pk, state='queued',
        )
        first_ready, second_ready = threading.Event(), threading.Event()
        release_first, release_second = threading.Event(), threading.Event()
        tokens = []
        mutex = threading.Lock()

        def pause(claim):
            with mutex:
                tokens.append(claim.effect_claim_token)
                first = len(tokens) == 1
            (first_ready if first else second_ready).set()
            if not (release_first if first else release_second).wait(20):
                raise TimeoutError('test did not release export claim')
            return begin_claimed_storage_effect(claim)

        def worker():
            close_old_connections()
            try:
                annotation_export_v4(record.pk)
            finally:
                connection.close()

        with patch('io_storages.annotation_outbox.begin_claimed_storage_effect', side_effect=pause), \
             patch('io_storages.dispatch_recovery.redispatch_queued_operation', side_effect=OSError('broker unavailable')), \
             patch.object(LocalFilesExportStorage, 'save_annotation') as save, \
             ThreadPoolExecutor(max_workers=2) as pool:
            old = pool.submit(worker)
            try:
                self.assertTrue(first_ready.wait(15))
                recover_unstarted_storage(self.project.pk, record.pk, local_principal(self.manager), reason='worker stopped')
                new = pool.submit(worker)
                self.assertTrue(second_ready.wait(15))
                self.assertNotEqual(tokens[0], tokens[1])
                release_first.set()
                with self.assertRaises(ValidationError):
                    old.result(15)
                save.assert_not_called()
                release_second.set()
                new.result(15)
            finally:
                release_first.set()
                release_second.set()
        save.assert_called_once()
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.COMPLETED)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_storage_recovery_refuses_legacy_started_wrong_owner_and_cross_project(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-recover-negative')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        principal = local_principal(self.manager)
        for phase, token in ((None, None), (STORAGE_EFFECT_STARTED, uuid.uuid4())):
            StorageOperation.objects.filter(pk=record.pk).update(state='running', effect_phase=phase, effect_claim_token=token)
            with self.assertRaises(ValidationError):
                recover_unstarted_storage(self.project.pk, record.pk, principal, reason='recover')
        StorageOperation.objects.filter(pk=record.pk).update(effect_phase=STORAGE_PRE_EFFECT, effect_claim_token=uuid.uuid4())
        other = ProjectFactory(organization=self.org)
        with self.assertRaises(ValidationError):
            recover_unstarted_storage(other.pk, record.pk, principal, reason='recover')
        storage.refresh_from_db()
        storage.meta = {QUEUE_OWNER_META_KEY: 'different-owner'}
        storage.save(update_fields=['meta'])
        with self.assertRaises(ValidationError):
            recover_unstarted_storage(self.project.pk, record.pk, principal, reason='recover')
        self.assertFalse(StorageOperationRecovery.objects.filter(operation=record).exists())

    @override_settings(
        ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED=True,
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_public_storage_recovery_enforces_scope_actor_and_stale_receipt(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-public-recover')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        StorageOperation.objects.filter(pk=record.pk).update(
            state='running', effect_phase=STORAGE_PRE_EFFECT, effect_claim_token=uuid.uuid4()
        )
        client = APIClient()
        client.force_authenticate(self.manager)
        url = f'/api/projects/{self.project.pk}/storage-operations/{record.pk}/resolve/'
        body = {'action': 'recover_storage_pre_effect', 'reason': 'worker stopped', 'attested': False}
        inventory = client.get(f'/api/projects/{self.project.pk}/storage-operations/')
        self.assertEqual(inventory.status_code, 200, inventory.content)
        self.assertTrue(inventory.data['operations'][0]['can_recover_storage_pre_effect'])
        self.assertNotIn('effect_claim_token', inventory.data['operations'][0])
        self.assertEqual(client.post(url, {**body, 'attested': True}, format='json').status_code, 400)
        other = ProjectFactory(organization=self.org)
        wrong_url = f'/api/projects/{other.pk}/storage-operations/{record.pk}/resolve/'
        self.assertEqual(client.post(wrong_url, body, format='json').status_code, 400)
        annotator = UserFactory(active_organization=self.org)
        self.org.add_user(annotator)
        ProjectMember.objects.create(project=self.project, user=annotator, role='annotator')
        client.force_authenticate(annotator)
        self.assertIn(client.post(url, body, format='json').status_code, (403, 404))
        client.force_authenticate(self.manager)
        with patch('io_storages.dispatch_recovery.redispatch_queued_operation', side_effect=OSError('broker unavailable')):
            self.assertEqual(client.post(url, body, format='json').status_code, 204)
        self.assertEqual(client.post(url, body, format='json').status_code, 400)
        record.refresh_from_db()
        self.assertEqual((record.state, record.dispatch_attempts), (StorageOperation.State.QUEUED, 1))
        self.assertEqual(StorageOperationRecovery.objects.filter(operation=record).count(), 1)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_killed_storage_worker_pre_effect_is_requeued_without_repeating_effect(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-killed-pre-effect')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        code = """
import os, time, django
from unittest.mock import patch
django.setup()
from io_storages.base_models import storage_sync_v3
from io_storages.localfiles.models import LocalFilesImportStorage
def pause(*args):
    open(os.environ['ISSUE86_MARKER'], 'w').write('pre-effect')
    time.sleep(300)
with patch('io_storages.base_models.begin_claimed_storage_effect', pause):
    storage_sync_v3(LocalFilesImportStorage, int(os.environ['ISSUE86_STORAGE']), os.environ['ISSUE86_OPERATION'], 'import_sync')
"""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'started'
            environment = os.environ.copy()
            environment.update(
                POSTGRE_NAME=connection.settings_dict['NAME'], SECRET_KEY=settings.SECRET_KEY,
                ANNOTATION_STORAGE_ADMISSION_ENABLED='true',
                ANNOTATION_PROJECT_OPERATION_PROVIDER='projects.tests.test_lifecycle_task_fence.TitlePolicy',
                ISSUE86_MARKER=str(marker), ISSUE86_OPERATION=str(record.pk), ISSUE86_STORAGE=str(storage.pk),
                DJANGO_SETTINGS_MODULE='core.settings.label_studio', LATEST_VERSION_CHECK='false',
            )
            process = subprocess.Popen(
                [sys.executable, '-c', code], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            try:
                deadline = time.monotonic() + 45
                while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(0.05)
                if not marker.exists():
                    if process.poll() is None:
                        process.kill()
                    _, error = process.communicate(timeout=10)
                    self.fail(f'worker did not reach pre-effect: {error.decode(errors="replace")[-2000:]}')
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
        record.refresh_from_db()
        self.assertEqual((record.state, record.effect_phase), (StorageOperation.State.RUNNING, STORAGE_PRE_EFFECT))
        with patch('io_storages.dispatch_recovery.redispatch_queued_operation', side_effect=OSError('broker unavailable')):
            recover_unstarted_storage(self.project.pk, record.pk, local_principal(self.manager), reason='worker died')
        record.refresh_from_db()
        self.assertEqual((record.state, record.dispatch_attempts), (StorageOperation.State.QUEUED, 1))
        StorageOperation.objects.filter(pk=record.pk).update(started_at=timezone.now() - timedelta(minutes=2))
        with patch('core.redis.start_job_async_or_sync', return_value=MagicMock(id='retried-job')) as enqueue:
            call_command('replay_storage_outbox', project_id=self.project.pk)
        self.assertEqual(enqueue.call_args.args[0], storage_sync_v3)
        record.refresh_from_db()
        with patch.object(LocalFilesImportStorage, 'scan_and_create_links') as scan:
            storage_sync_v3(LocalFilesImportStorage, storage.pk, record.pk, 'import_sync', record.dispatch_attempts)
        scan.assert_called_once()
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.COMPLETED)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_storage_recovery_rejects_an_effect_already_started(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-running-effect')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        entered, release = threading.Event(), threading.Event()

        def effect():
            entered.set()
            if not release.wait(15):
                raise TimeoutError('test did not release effect')

        def worker():
            close_old_connections()
            try:
                storage_sync_v3(LocalFilesImportStorage, storage.pk, record.pk, 'import_sync')
            finally:
                connection.close()

        with patch.object(LocalFilesImportStorage, 'scan_and_create_links', side_effect=effect) as scan, \
             ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            try:
                self.assertTrue(entered.wait(15))
                record.refresh_from_db()
                self.assertEqual(record.effect_phase, STORAGE_EFFECT_STARTED)
                with self.assertRaises(ValidationError):
                    recover_unstarted_storage(self.project.pk, record.pk, local_principal(self.manager), reason='recover')
            finally:
                release.set()
            future.result(15)
        scan.assert_called_once()

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_stale_health_check_cannot_fail_a_recovered_storage_attempt(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-stale-health')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        self.assertEqual(record_storage_dispatch_job(storage, record.pk, 'old-failed-job'), 1)
        stale = LocalFilesImportStorage.objects.get(pk=storage.pk)
        StorageOperation.objects.filter(pk=record.pk).update(
            state='running', effect_phase=STORAGE_PRE_EFFECT, effect_claim_token=uuid.uuid4()
        )
        pids = []

        def check():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pids.append(cursor.fetchone()[0])
                stale.job_health_check()
            finally:
                connection.close()

        with patch('io_storages.dispatch_recovery.redispatch_queued_operation', side_effect=OSError('broker unavailable')), \
             patch('io_storages.base_models.django_rq.get_queue'), \
             patch('io_storages.base_models.Job.fetch', return_value=MagicMock(get_status=lambda: 'failed')) as fetch, \
             ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                recover_unstarted_storage(self.project.pk, record.pk, local_principal(self.manager), reason='recover')
                future = pool.submit(check)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if pids:
                        with connection.cursor() as cursor:
                            cursor.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s', [pids[0]])
                            row = cursor.fetchone()
                        if row and row[0] == 'Lock':
                            break
                    time.sleep(0.02)
                else:
                    self.fail('health check did not wait for recovery')
            future.result(15)
        fetch.assert_called_once()
        storage.refresh_from_db()
        record.refresh_from_db()
        self.assertEqual((storage.status, storage.last_sync_job), ('queued', None))
        self.assertEqual((record.state, record.dispatch_attempts), (StorageOperation.State.QUEUED, 1))
        # An old in-memory ping cannot mark a recovered queued attempt failed
        # even when Redis is unavailable and job_health_check is skipped.
        stale.status = 'in_progress'
        stale.meta = {'time_last_ping': (timezone.now() - timedelta(days=1)).isoformat()}
        with patch('io_storages.base_models.redis_connected', return_value=False):
            stale.health_check()
        storage.refresh_from_db()
        self.assertEqual(storage.status, 'queued')

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_storage_recovery_clears_failed_old_receipt_after_health_check_wins(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-health-won')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        self.assertEqual(record_storage_dispatch_job(storage, record.pk, 'old-failed-job'), 1)
        storage.refresh_from_db()
        StorageOperation.objects.filter(pk=record.pk).update(
            state='running', effect_phase=STORAGE_PRE_EFFECT, effect_claim_token=uuid.uuid4()
        )
        with patch('io_storages.base_models.django_rq.get_queue'), \
             patch('io_storages.base_models.Job.fetch', return_value=MagicMock(get_status=lambda: 'failed')):
            storage.job_health_check()
        storage.refresh_from_db()
        self.assertEqual(storage.status, 'failed')
        with patch('io_storages.dispatch_recovery.redispatch_queued_operation', side_effect=OSError('broker unavailable')):
            recover_unstarted_storage(self.project.pk, record.pk, local_principal(self.manager), reason='recover')
        storage.refresh_from_db()
        self.assertEqual((storage.status, storage.last_sync_job, storage.traceback), ('queued', None, None))

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_stalled_redis_observation_cannot_block_or_fail_recovered_reused_job_id(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-stalled-health')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        self.assertEqual(record_storage_dispatch_job(storage, record.pk, 'reused-job'), 1)
        stale = LocalFilesImportStorage.objects.get(pk=storage.pk)
        StorageOperation.objects.filter(pk=record.pk).update(
            state='running', effect_phase=STORAGE_PRE_EFFECT, effect_claim_token=uuid.uuid4()
        )
        entered, release = threading.Event(), threading.Event()

        def fetch(*args, **kwargs):
            entered.set()
            if not release.wait(20):
                raise TimeoutError('test did not release Redis observation')
            return MagicMock(get_status=lambda: 'failed')

        def check():
            close_old_connections()
            try:
                stale.job_health_check()
            finally:
                connection.close()

        with patch('io_storages.base_models.django_rq.get_queue'), \
             patch('io_storages.base_models.Job.fetch', side_effect=fetch), \
             patch('io_storages.dispatch_recovery.redispatch_queued_operation', side_effect=OSError('broker unavailable')), \
             ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(check)
            try:
                self.assertTrue(entered.wait(15))
                recover_unstarted_storage(self.project.pk, record.pk, local_principal(self.manager), reason='recover')
                self.assertEqual(record_storage_dispatch_job(storage, record.pk, 'reused-job', dispatch_attempt=1), 1)
            finally:
                release.set()
            future.result(15)
        storage.refresh_from_db()
        record.refresh_from_db()
        self.assertEqual((storage.status, storage.last_sync_job), ('queued', 'reused-job'))
        self.assertEqual((record.state, record.dispatch_attempts), (StorageOperation.State.QUEUED, 1))

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_timer_new_attempt_invalidates_inflight_health_observation(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-timer-health')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        self.assertEqual(record_storage_dispatch_job(storage, record.pk, 'same-job'), 1)
        stale = LocalFilesImportStorage.objects.get(pk=storage.pk)
        entered, release = threading.Event(), threading.Event()

        def fetch(*args, **kwargs):
            entered.set()
            if not release.wait(20):
                raise TimeoutError('test did not release Redis observation')
            return MagicMock(get_status=lambda: 'failed')

        def check():
            close_old_connections()
            try:
                stale.job_health_check()
            finally:
                connection.close()

        with patch('io_storages.base_models.django_rq.get_queue'), \
             patch('io_storages.base_models.Job.fetch', side_effect=fetch), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(check)
            try:
                self.assertTrue(entered.wait(15))
                StorageOperation.objects.filter(pk=record.pk).update(dispatch_attempts=1)
            finally:
                release.set()
            future.result(15)
        storage.refresh_from_db()
        self.assertEqual((storage.status, storage.last_sync_job), ('queued', 'same-job'))

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_timer_reservation_before_health_check_does_not_reuse_old_receipt(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-timer-first')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        self.assertEqual(record_storage_dispatch_job(storage, record.pk, 'old-job', dispatch_attempt=0), 1)
        StorageOperation.objects.filter(pk=record.pk).update(dispatch_attempts=1)
        storage.refresh_from_db()
        with patch('io_storages.base_models.django_rq.get_queue'), \
             patch('io_storages.base_models.Job.fetch', return_value=MagicMock(get_status=lambda: 'failed')):
            storage.job_health_check()
        storage.refresh_from_db()
        self.assertEqual((storage.status, storage.last_sync_job), ('queued', 'old-job'))

    def test_sqlite_refuses_to_claim_durable_admission(self):
        if connection.vendor == 'postgresql':
            self.skipTest('SQLite-specific behavior')
        with self.assertRaises(RuntimeError):
            begin_storage_operation(self.project.pk, 'io_storages.test', 1, 'export')

    def test_standalone_process_inventories_legacy_storage_without_loading_urls(self):
        self.require_pg()
        code = """
import os, django
django.setup()
from projects.models import Project
from io_storages.admission import legacy_export_blocker_counts
project = Project.objects.get(pk=int(os.environ['TEST_PROJECT_ID']))
counts = legacy_export_blocker_counts(project)
assert counts['storage_local_import'] == 0
assert counts['storage_local_export'] == 0
"""
        environment = os.environ.copy()
        environment.update(
            POSTGRE_NAME=connection.settings_dict['NAME'],
            TEST_PROJECT_ID=str(self.project.pk),
            DJANGO_SETTINGS_MODULE='core.settings.label_studio',
            LATEST_VERSION_CHECK='false',
        )
        result = subprocess.run(
            [sys.executable, '-c', code],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace')[-3000:])

    def test_running_failure_and_reconciliation_keep_auditable_states(self):
        self.require_pg()
        with self.assertRaisesRegex(RuntimeError, 'unknown'):
            with admitted_storage_operation(
                self.project.pk, 'io_storages.localfilesexportstorage', 1, 'export_sync'
            ) as record:
                with transaction.atomic(), self.assertRaises(ValidationError):
                    lock_drained_project(self.project.pk)
                raise RuntimeError('unknown')
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.UNCERTAIN)
        with transaction.atomic(), self.assertRaises(ValidationError):
            lock_drained_project(self.project.pk)
        with self.assertRaises(ValidationError):
            reconcile_uncertain_storage_operation(
                self.project.pk, record.pk, local_principal(self.manager), reason='checked', attested_quiescent=False
            )
        reviewer = UserFactory(active_organization=self.org)
        self.org.add_user(reviewer)
        ProjectMember.objects.create(project=self.project, user=reviewer, role='reviewer')
        with self.assertRaises(PermissionDenied):
            reconcile_uncertain_storage_operation(
                self.project.pk, record.pk, local_principal(reviewer), reason='checked', attested_quiescent=True
            )
        other_project = ProjectFactory(organization=self.org)
        with self.assertRaises(ValidationError):
            reconcile_uncertain_storage_operation(
                other_project.pk, record.pk, local_principal(self.manager), reason='checked', attested_quiescent=True
            )
        reconcile_uncertain_storage_operation(
            self.project.pk,
            record.pk,
            local_principal(self.manager),
            reason='worker stopped and provider checked',
            attested_quiescent=True,
        )
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.RECONCILED)
        self.assertEqual(record.resolved_by_id_snapshot, self.manager.pk)
        with transaction.atomic():
            lock_drained_project(self.project.pk)
        with self.assertRaises(ValidationError):
            finish_storage_operation(record.pk, succeeded=True)

    def test_crashed_worker_record_keeps_drain_blocked(self):
        self.require_pg()
        record = begin_storage_operation(self.project.pk, 'io_storages.test', 1, 'import')
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.RUNNING)
        with transaction.atomic(), self.assertRaises(ValidationError):
            lock_drained_project(self.project.pk)

    def test_legacy_export_receipts_block_drain_until_successful(self):
        self.require_pg()
        other = ProjectFactory(organization=self.org)
        Export.objects.create(project=other, status=Export.Status.IN_PROGRESS)
        export = Export.objects.create(project=self.project, status=Export.Status.COMPLETED)
        converted = ConvertedFormat.objects.create(export=export, export_type='CSV', status='completed')
        release = ApprovedDatasetRelease.objects.create(
            project=self.project,
            organization_id_frozen=self.org.pk,
            actor_id_frozen=self.manager.pk,
            principal_id='synthetic',
            idempotency_key='synthetic',
            request_hash='x',
            frozen_input={},
            input_sha256='x',
            content_sha256='x',
            state='ready',
        )
        for obj, field, terminal in (
            (export, 'status', 'completed'),
            (converted, 'status', 'completed'),
            (release, 'state', 'ready'),
        ):
            for unsafe in ('queued', 'running', 'failed', 'unknown'):
                with self.subTest(model=type(obj).__name__, state=unsafe):
                    type(obj).objects.filter(pk=obj.pk).update(**{field: unsafe})
                    with self.assertRaisesRegex(ValidationError, 'EXPORT_NOT_DRAINED'):
                        with transaction.atomic():
                            lock_drained_project(self.project.pk)
            type(obj).objects.filter(pk=obj.pk).update(**{field: terminal})
        with transaction.atomic():
            lock_drained_project(self.project.pk)
        old_attempt = ApprovedDatasetReleaseAttempt.objects.create(release=release, number=1, state='expired')
        with self.assertRaisesRegex(ValidationError, 'EXPORT_NOT_DRAINED'):
            with transaction.atomic():
                lock_drained_project(self.project.pk)
        self.assertEqual(legacy_export_blocker_counts(self.project)['release_attempts'], 1)
        old_attempt.state = 'ready'
        old_attempt.save(update_fields=['state'])
        snapshot = ReleaseSnapshot.objects.create(
            project=self.project,
            release=release,
            actor_id=self.manager.pk,
            principal_id='synthetic',
            idempotency_key='synthetic',
            policy={},
        )
        for state in ('dispatch_pending', 'queued', 'running', 'failed', 'unknown'):
            ReleaseSnapshot.objects.filter(pk=snapshot.pk).update(state=state)
            with self.assertRaisesRegex(ValidationError, 'EXPORT_NOT_DRAINED'):
                with transaction.atomic():
                    lock_drained_project(self.project.pk)
        ReleaseSnapshot.objects.filter(pk=snapshot.pk).update(state='running')
        ReleaseSnapshot.objects.filter(pk=snapshot.pk).update(
            state='ready',
            manifest=b'{}',
            manifest_sha256='a' * 64,
            content_digest='b' * 64,
        )
        with transaction.atomic():
            lock_drained_project(self.project.pk)

        instance_id, package_id = uuid.uuid4(), uuid.uuid4()
        binding = AuditActorBinding.objects.create(instance_id=instance_id, live_user=self.manager)
        digest = 'a' * 64
        bundle = {
            'schema': 'review-audit-source/v1',
            'package_id': str(package_id),
            'creator_generation': str(binding.pk),
            'owner_hash': digest,
            'request_hash': digest,
            'projection_sha256': digest,
            'project_id': self.project.pk,
            'organization_id': self.org.pk,
        }
        projection = {
            'schema': 'review-audit-source/v1',
            'profile': 'review-audit-redacted/v1',
            'effect': 'supervisory_only',
            'package_id': str(package_id),
        }
        package = AuditPackage.objects.create(
            id=package_id,
            project=self.project,
            organization_id_frozen=self.org.pk,
            instance_id=instance_id,
            creator=binding,
            creator_principal='synthetic',
            creator_source='synthetic',
            owner_hash=digest,
            idempotency_key=uuid.uuid4(),
            request_hash=digest,
            access_policy='{}',
            summary_record='{}',
            input_bundle=json.dumps(bundle),
            input_sha256=digest,
            projection=json.dumps(projection),
            projection_sha256=digest,
        )
        AuditAttempt.objects.create(package=package, number=1)
        self.assertEqual(legacy_export_blocker_counts(self.project)['audit_attempts'], 1)
        with self.assertRaisesRegex(ValidationError, 'EXPORT_NOT_DRAINED'):
            with transaction.atomic():
                lock_drained_project(self.project.pk)

    @override_settings(
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_legacy_export_blocks_actual_transition_and_preserves_state(self):
        self.require_pg()
        principal = local_principal(self.manager)
        transition_project(self.project.pk, principal, expected_revision=0, target='active', reason='start')
        export = Export.objects.create(project=self.project, status=Export.Status.IN_PROGRESS)
        with self.assertRaisesRegex(ValidationError, 'EXPORT_NOT_DRAINED'):
            transition_project(self.project.pk, principal, expected_revision=1, target='paused', reason='pause')
        self.project.refresh_from_db()
        self.assertEqual((self.project.lifecycle_state, self.project.lifecycle_revision), ('active', 1))
        self.assertEqual(ProjectLifecycleEvent.objects.filter(project=self.project).count(), 1)
        export.status = Export.Status.COMPLETED
        export.save(update_fields=['status'])
        transition_project(self.project.pk, principal, expected_revision=1, target='paused', reason='pause')
        self.project.refresh_from_db()
        self.assertEqual((self.project.lifecycle_state, self.project.lifecycle_revision), ('paused', 2))

    @override_settings(
        ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED=True,
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_legacy_storage_statuses_block_transition_and_are_scoped_in_inventory(self):
        self.require_pg()
        principal = local_principal(self.manager)
        transition_project(self.project.pk, principal, expected_revision=0, target='active', reason='start')
        storage = S3ExportStorage.objects.create(project=self.project, bucket='synthetic', status='queued')
        other = ProjectFactory(organization=self.org)
        S3ExportStorage.objects.create(project=other, bucket='other', status='queued')
        client = APIClient()
        client.force_authenticate(self.manager)
        url = f'/api/projects/{self.project.pk}/storage-operations/'
        for state in ('queued', 'in_progress', 'failed', 'completed_with_errors', 'unknown'):
            with self.subTest(state=state):
                S3ExportStorage.objects.filter(pk=storage.pk).update(status=state)
                self.assertFalse(StorageOperation.objects.filter(project=self.project).exists())
                with self.assertRaisesRegex(ValidationError, 'LEGACY_STORAGE_NOT_DRAINED'):
                    transition_project(
                        self.project.pk, principal, expected_revision=1, target='paused', reason='pause'
                    )
                response = client.get(url)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.data['legacy_export_blockers']['storage_s3_export'], 1)
                self.assertIn(
                    {'kind': 'storage_s3_export', 'id': str(storage.pk), 'state': state, 'parent_id': None},
                    response.data['legacy_export_details']['items'],
                )
        self.project.refresh_from_db()
        self.assertEqual((self.project.lifecycle_state, self.project.lifecycle_revision), ('active', 1))
        # A terminal state is merely absence of this particular blocker; it is
        # not evidence that old credentials or in-flight requests are isolated.
        S3ExportStorage.objects.filter(pk=storage.pk).update(status='completed')
        with transaction.atomic():
            lock_drained_project(self.project.pk)

    @unittest.skipUnless(os.environ.get('ISSUE86_OLD_LABEL_STUDIO_DIR'), 'requires an isolated old checkout')
    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_old_sync_worker_blocks_new_drain_without_new_ledger(self):
        self.require_pg()
        storage = S3ImportStorage.objects.create(project=self.project, bucket='synthetic', status='queued', meta={})
        code = """
import os, time, django
django.setup()
import io_storages.base_models as legacy_base
assert os.path.realpath(legacy_base.__file__).startswith(os.path.realpath(os.environ['ISSUE86_OLD_LABEL_STUDIO_DIR']) + os.sep)
from io_storages.base_models import import_sync_background
from io_storages.s3.models import S3ImportStorage
def paused_scan(storage):
    storage.info_set_in_progress()
    open(os.environ['TEST_MARKER_PATH'], 'w').close()
    while not os.path.exists(os.environ['TEST_RELEASE_PATH']):
        time.sleep(0.05)
    storage.info_set_completed(0)
S3ImportStorage.scan_and_create_links = paused_scan
import_sync_background(S3ImportStorage, int(os.environ['TEST_STORAGE_ID']))
"""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'started'
            release = Path(directory) / 'release'
            environment = os.environ.copy()
            environment.update(
                POSTGRE_NAME=connection.settings_dict['NAME'],
                TEST_STORAGE_ID=str(storage.pk),
                TEST_MARKER_PATH=str(marker),
                TEST_RELEASE_PATH=str(release),
                LATEST_VERSION_CHECK='false',
                DJANGO_SETTINGS_MODULE='core.settings.label_studio',
                PYTHONPATH=os.pathsep.join(
                    (
                        str(Path(os.environ['ISSUE86_OLD_LABEL_STUDIO_DIR']).parent),
                        os.environ['ISSUE86_OLD_LABEL_STUDIO_DIR'],
                    )
                ),
            )
            process = subprocess.Popen(
                [sys.executable, '-c', code],
                cwd=os.environ['ISSUE86_OLD_LABEL_STUDIO_DIR'],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 45
                while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(0.05)
                if not marker.exists():
                    if process.poll() is None:
                        process.kill()
                    _, error = process.communicate(timeout=10)
                    self.fail(f'old worker did not start: {error.decode(errors="replace")[-3000:]}')
                storage.refresh_from_db()
                self.assertEqual(storage.status, 'in_progress')
                self.assertFalse(StorageOperation.objects.filter(project=self.project).exists())
                with transaction.atomic(), self.assertRaisesRegex(ValidationError, 'LEGACY_STORAGE_NOT_DRAINED'):
                    lock_drained_project(self.project.pk)
                client = APIClient()
                client.force_authenticate(self.manager)
                url = f'/api/storages/s3/{storage.pk}'
                self.assertEqual(client.patch(url, {'title': 'changed'}, format='json').status_code, 400)
                self.assertEqual(client.delete(url).status_code, 400)
                self.assertTrue(S3ImportStorage.objects.filter(pk=storage.pk).exists())
                if os.environ.get('ISSUE86_OLD_WORKER_CRASH') == '1':
                    process.kill()
                    process.wait(timeout=10)
                    storage.refresh_from_db()
                    self.assertEqual(storage.status, 'in_progress')
                    with transaction.atomic(), self.assertRaisesRegex(ValidationError, 'LEGACY_STORAGE_NOT_DRAINED'):
                        lock_drained_project(self.project.pk)
                    # Upstream health checks may mark a stale job failed; this
                    # must not be mistaken for a proven provider outcome.
                    S3ImportStorage.objects.filter(pk=storage.pk).update(status='failed')
                    with transaction.atomic(), self.assertRaisesRegex(ValidationError, 'LEGACY_STORAGE_NOT_DRAINED'):
                        lock_drained_project(self.project.pk)
                    return
                release.touch()
                _, error = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, error.decode(errors='replace')[-3000:])
                storage.refresh_from_db()
                self.assertEqual(storage.status, 'completed')
                with transaction.atomic():
                    lock_drained_project(self.project.pk)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)

    def test_killed_process_leaves_committed_record_blocking(self):
        self.require_pg()
        code = (
            'import os,time,django; django.setup(); '
            'from io_storages.admission import begin_storage_operation; '
            'r=begin_storage_operation(int(os.environ["TEST_PROJECT_ID"]),"io_storages.localfilesimportstorage",1,"import_sync"); '
            'open(os.environ["TEST_MARKER_PATH"],"w").write(str(r.pk)); time.sleep(300)'
        )
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'operation'
            environment = os.environ.copy()
            environment.update(
                POSTGRE_NAME=connection.settings_dict['NAME'],
                TEST_PROJECT_ID=str(self.project.pk),
                TEST_MARKER_PATH=str(marker),
                LATEST_VERSION_CHECK='false',
                DJANGO_SETTINGS_MODULE='core.settings.label_studio',
            )
            process = subprocess.Popen(
                [sys.executable, '-c', code], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            try:
                deadline = time.monotonic() + 45
                while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(0.05)
                if not marker.exists():
                    if process.poll() is None:
                        process.kill()
                    _, error = process.communicate(timeout=10)
                    self.fail(f'worker did not commit admission: {error.decode(errors="replace")[-3000:]}')
                operation_id = marker.read_text()
            finally:
                process.kill()
                process.wait(timeout=10)
            record = StorageOperation.objects.get(pk=operation_id)
            self.assertEqual(record.state, StorageOperation.State.RUNNING)
            with transaction.atomic(), self.assertRaises(ValidationError):
                lock_drained_project(self.project.pk)
            with self.assertRaisesRegex(ValidationError, 'uncertain operation'):
                reconcile_uncertain_storage_operation(
                    self.project.pk,
                    record.pk,
                    local_principal(self.manager),
                    reason='test worker stopped and provider outcome checked',
                    attested_quiescent=True,
                )
            record.refresh_from_db()
            self.assertEqual(record.state, StorageOperation.State.RUNNING)
            with transaction.atomic(), self.assertRaises(ValidationError):
                lock_drained_project(self.project.pk)

    def test_unknown_ledger_state_blocks_drain(self):
        self.require_pg()
        record = begin_storage_operation(self.project.pk, 'io_storages.test', 1, 'import')
        StorageOperation.objects.filter(pk=record.pk).update(state='unknown')
        with transaction.atomic(), self.assertRaises(ValidationError):
            lock_drained_project(self.project.pk)

    def test_admission_waits_for_transition_and_observes_paused_policy(self):
        self.require_pg()
        pid = []

        def worker():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pid.append(cursor.fetchone()[0])
                return begin_storage_operation(self.project.pk, 'io_storages.test', 1, 'import')
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                project = lock_drained_project(self.project.pk)
                project.title = 'test-paused'
                project.save(update_fields=['title'])
                future = pool.submit(worker)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if pid:
                        with connection.cursor() as cursor:
                            cursor.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s', [pid[0]])
                            result = cursor.fetchone()
                        if result and result[0] == 'Lock':
                            break
                    time.sleep(0.02)
                else:
                    self.fail('admission did not wait for transition lock')
            with self.assertRaises(PermissionDenied):
                future.result(30)
        self.assertFalse(StorageOperation.objects.exists())

    def test_transition_checks_committed_admission(self):
        self.require_pg()
        started, release = threading.Event(), threading.Event()

        def worker():
            close_old_connections()
            try:
                with admitted_storage_operation(self.project.pk, 'io_storages.test', 1, 'export'):
                    started.set()
                    if not release.wait(15):
                        raise TimeoutError('test did not release worker')
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            try:
                self.assertTrue(started.wait(15))
                with transaction.atomic(), self.assertRaises(ValidationError):
                    lock_drained_project(self.project.pk)
            finally:
                release.set()
            future.result(30)
        with transaction.atomic():
            lock_drained_project(self.project.pk)

    def test_internal_reconciliation_cannot_release_a_running_worker(self):
        self.require_pg()
        started, release = threading.Event(), threading.Event()
        operation_ids = []

        def worker():
            close_old_connections()
            try:
                with admitted_storage_operation(
                    self.project.pk, 'io_storages.localfilesexportstorage', 1, 'export_sync'
                ) as record:
                    operation_ids.append(record.pk)
                    started.set()
                    if not release.wait(15):
                        raise TimeoutError('test did not release worker')
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            try:
                self.assertTrue(started.wait(15))
                with self.assertRaisesRegex(ValidationError, 'uncertain operation'):
                    reconcile_uncertain_storage_operation(
                        self.project.pk,
                        operation_ids[0],
                        local_principal(self.manager),
                        reason='claimed stopped',
                        attested_quiescent=True,
                    )
                self.assertEqual(
                    StorageOperation.objects.get(pk=operation_ids[0]).state, StorageOperation.State.RUNNING
                )
                with transaction.atomic(), self.assertRaises(ValidationError):
                    lock_drained_project(self.project.pk)
            finally:
                release.set()
            future.result(30)
        self.assertEqual(StorageOperation.objects.get(pk=operation_ids[0]).state, StorageOperation.State.COMPLETED)
        with transaction.atomic():
            lock_drained_project(self.project.pk)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_import_worker_failure_leaves_uncertain_record(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-input')
        with patch.object(LocalFilesImportStorage, 'scan_and_create_links', side_effect=OSError('provider timeout')):
            with self.assertRaisesRegex(OSError, 'provider timeout'):
                import_sync_background(LocalFilesImportStorage, storage.pk)
        record = StorageOperation.objects.get(project=self.project)
        self.assertEqual(record.state, StorageOperation.State.UNCERTAIN)
        with transaction.atomic(), self.assertRaises(ValidationError):
            lock_drained_project(self.project.pk)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_export_admission_is_committed_before_external_write(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = LocalFilesExportStorage.objects.create(project=self.project, path='/tmp/issue86-output')
        observed = []

        def open_file(*args, **kwargs):
            # This read uses another connection and cannot see uncommitted rows.
            def inspect():
                close_old_connections()
                try:
                    return list(StorageOperation.objects.filter(project=self.project).values_list('state', flat=True))
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=1) as pool:
                observed.extend(pool.submit(inspect).result(10))
            return mock_open()(*args, **kwargs)

        with (
            patch.object(LocalFilesExportStorage, '_get_storage_path_or_raise', return_value='/tmp/issue86-output'),
            patch('io_storages.localfiles.models.open_local_file_write', side_effect=open_file),
            patch('io_storages.localfiles.models.is_private_local_path', return_value=False),
        ):
            storage.save_annotation(annotation)
        self.assertEqual(observed, [StorageOperation.State.RUNNING])
        self.assertEqual(StorageOperation.objects.get(project=self.project).state, StorageOperation.State.COMPLETED)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_transactional_delete_persists_intent_before_manual_reconciliation(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = LocalFilesExportStorage.objects.create(
            project=self.project,
            path='/tmp/issue86-output',
            can_delete_objects=True,
        )
        link = LocalFilesExportStorageLink.objects.create(storage=storage, annotation=annotation)
        with patch('io_storages.localfiles.models.unlink_local_file') as unlink:
            annotation.delete()
        unlink.assert_not_called()
        self.assertFalse(LocalFilesExportStorageLink.objects.filter(pk=link.pk).exists())
        record = StorageOperation.objects.get(project=self.project, operation='annotation_delete_pending')
        self.assertEqual(record.state, StorageOperation.State.QUEUED)
        with transaction.atomic(), self.assertRaises(ValidationError):
            lock_drained_project(self.project.pk)
        with self.assertRaises(ValidationError):
            cancel_queued_storage_dispatch(self.project.pk, record.pk, local_principal(self.manager), reason='cancel')
        with self.assertRaises(ValidationError):
            reconcile_pending_deletion(
                self.project.pk,
                record.pk,
                local_principal(self.manager),
                reason='checked',
                attested_complete=False,
            )
        reconcile_pending_deletion(
            self.project.pk,
            record.pk,
            local_principal(self.manager),
            reason='operator verified deletion',
            attested_complete=True,
        )
        with transaction.atomic():
            lock_drained_project(self.project.pk)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_rolled_back_delete_leaves_no_external_effect_or_intent(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = LocalFilesExportStorage.objects.create(
            project=self.project, path='/tmp/issue86-output', can_delete_objects=True
        )
        link = LocalFilesExportStorageLink.objects.create(storage=storage, annotation=annotation)
        annotation_id = annotation.pk
        with patch('io_storages.localfiles.models.unlink_local_file') as unlink:
            with self.assertRaisesRegex(RuntimeError, 'rollback'):
                with transaction.atomic():
                    annotation.delete()
                    self.assertTrue(StorageOperation.objects.filter(operation='annotation_delete_pending').exists())
                    raise RuntimeError('rollback')
        unlink.assert_not_called()
        self.assertTrue(LocalFilesExportStorageLink.objects.filter(pk=link.pk).exists())
        self.assertTrue(Annotation.objects.filter(pk=annotation_id).exists())
        self.assertFalse(StorageOperation.objects.filter(operation='annotation_delete_pending').exists())

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_s3_delete_records_exact_key_without_calling_provider(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(
            project=self.project,
            bucket='issue86-test',
            prefix='data',
            can_delete_objects=True,
        )
        S3ExportStorageLink.objects.create(storage=storage, annotation=annotation)
        expected_key = 'data/' + S3ExportStorageLink.get_key(annotation)
        with patch.object(S3ExportStorage, 'delete_annotation') as delete:
            annotation.delete()
        delete.assert_not_called()
        record = StorageOperation.objects.get(project=self.project, operation='annotation_delete_pending')
        self.assertEqual(record.target_key, expected_key)
        self.assertEqual(record.state, StorageOperation.State.QUEUED)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_unversioned_direct_deletion_is_denied_before_provider_access(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(project=self.project, bucket='issue86-test', prefix='data')
        with patch.object(S3ExportStorage, 'get_client_and_resource') as provider:
            with self.assertRaises(PermissionDenied):
                storage.delete_annotation(annotation)
        provider.assert_not_called()
        self.assertFalse(StorageOperation.objects.filter(operation='annotation_delete').exists())

    @override_settings(
        ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED=True,
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_cloud_attestation_alone_cannot_clear_deletion_or_unknown_outcome(self):
        self.require_pg()
        actual_models = {
            apps.get_model('io_storages', name)._meta.label_lower
            for name in (
                'S3ImportStorage',
                'S3ExportStorage',
                'GCSImportStorage',
                'GCSExportStorage',
                'AzureBlobImportStorage',
                'AzureBlobExportStorage',
            )
        }
        self.assertEqual(actual_models, CLOUD_STORAGE_MODELS)
        principal = local_principal(self.manager)
        client = APIClient()
        client.force_authenticate(self.manager)
        for model in (
            'io_storages.s3importstorage',
            'io_storages.s3exportstorage',
            'io_storages.gcsimportstorage',
            'io_storages.gcsexportstorage',
            'io_storages.azureblobimportstorage',
            'io_storages.azureblobexportstorage',
            'io_storages.redisexportstorage',
            'plugin.unknownstorage',
        ):
            for operation, state, action in (
                ('annotation_delete_pending', StorageOperation.State.QUEUED, 'confirm_deletion'),
                ('export_sync', StorageOperation.State.UNCERTAIN, 'confirm_uncertain'),
            ):
                with self.subTest(model=model, operation=operation):
                    record = StorageOperation.objects.create(
                        project=self.project,
                        storage_model=model,
                        storage_id=1,
                        operation=operation,
                        lifecycle_revision=0,
                        state=state,
                    )
                    url = f'/api/projects/{self.project.pk}/storage-operations/{record.pk}/resolve/'
                    response = client.post(
                        url, {'action': action, 'reason': 'checked', 'attested': True}, format='json'
                    )
                    self.assertEqual(response.status_code, 400, response.content)
                    expected = (
                        'CLOUD_RECONCILIATION_UNAVAILABLE'
                        if model in CLOUD_STORAGE_MODELS
                        else 'VERIFIED_RECONCILIATION_UNAVAILABLE'
                    )
                    self.assertIn(expected, str(response.data))
                    with self.assertRaisesRegex(ValidationError, expected):
                        if action == 'confirm_deletion':
                            reconcile_pending_deletion(
                                self.project.pk, record.pk, principal, reason='checked', attested_complete=True
                            )
                        else:
                            reconcile_uncertain_storage_operation(
                                self.project.pk, record.pk, principal, reason='checked', attested_quiescent=True
                            )
                    record.refresh_from_db()
                    self.assertEqual(record.state, state)
        with self.assertRaisesRegex(ValidationError, 'STORAGE_NOT_DRAINED'):
            with transaction.atomic():
                lock_drained_project(self.project.pk)

    @override_settings(
        ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED=True,
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_unknown_queued_worker_cannot_be_cancelled_as_if_version_fenced(self):
        self.require_pg()
        client = APIClient()
        client.force_authenticate(self.manager)
        for model, operation in (
            ('plugin.unknownstorage', 'legacy_unversioned_worker'),
            ('plugin.unknownstorage', 'import_sync'),
            ('io_storages.s3exportstorage', 'import_sync'),
            ('plugin.unknownstorage', 'webhook_project_updated'),
        ):
            with self.subTest(model=model, operation=operation):
                record = StorageOperation.objects.create(
                    project=self.project,
                    storage_model=model,
                    storage_id=1,
                    operation=operation,
                    lifecycle_revision=0,
                    state=StorageOperation.State.QUEUED,
                )
                response = client.post(
                    f'/api/projects/{self.project.pk}/storage-operations/{record.pk}/resolve/',
                    {'action': 'cancel_queued', 'reason': 'checked', 'attested': False},
                    format='json',
                )
                self.assertEqual(response.status_code, 400, response.content)
                with self.assertRaises(ValidationError):
                    cancel_queued_storage_dispatch(
                        self.project.pk, record.pk, local_principal(self.manager), reason='checked'
                    )
                record.refresh_from_db()
                self.assertEqual(record.state, StorageOperation.State.QUEUED)
        with self.assertRaisesRegex(ValidationError, 'STORAGE_NOT_DRAINED'):
            with transaction.atomic():
                lock_drained_project(self.project.pk)

    @override_settings(
        ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED=True,
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_unknown_uncertain_worker_cannot_borrow_known_model_attestation(self):
        self.require_pg()
        client = APIClient()
        client.force_authenticate(self.manager)
        for model in ('io_storages.localfilesexportstorage', 'webhooks.webhook'):
            with self.subTest(model=model):
                record = StorageOperation.objects.create(
                    project=self.project,
                    storage_model=model,
                    storage_id=1,
                    operation='unknown_worker',
                    lifecycle_revision=0,
                    state=StorageOperation.State.UNCERTAIN,
                )
                response = client.post(
                    f'/api/projects/{self.project.pk}/storage-operations/{record.pk}/resolve/',
                    {'action': 'confirm_uncertain', 'reason': 'checked', 'attested': True},
                    format='json',
                )
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn('VERIFIED_RECONCILIATION_UNAVAILABLE', str(response.data))
                record.refresh_from_db()
                self.assertEqual(record.state, StorageOperation.State.UNCERTAIN)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_s3_export_refreshes_stale_annotation_before_serialization(self):
        self.require_pg()
        annotation = AnnotationFactory(
            task=TaskFactory(project=self.project), project=self.project, result=[{'value': 'old'}]
        )
        storage = S3ExportStorage.objects.create(project=self.project, bucket='bucket')
        Annotation.objects.filter(pk=annotation.pk).update(result=[{'value': 'new'}])
        resource = MagicMock()
        resource.Object.return_value.put.return_value = {'VersionId': 'fixture-version'}
        with patch.object(
            S3ExportStorage, 'get_client_and_resource', return_value=(MagicMock(), resource)
        ), patch.object(S3ExportStorage, '_get_serialized_data', side_effect=lambda item: {'result': item.result}):
            storage.save_annotation(annotation)
        request = resource.Object.return_value.put.call_args.kwargs
        self.assertEqual(json.loads(request['Body']), {'result': [{'value': 'new'}]})
        self.assertEqual(
            StorageOperation.objects.get(project=self.project, operation='annotation_export').state,
            StorageOperation.State.COMPLETED,
        )

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True, FUTURE_SAVE_TASK_TO_STORAGE=True)
    def test_s3_export_rejects_a_changed_annotation_task_key(self):
        self.require_pg()
        first_task = TaskFactory(project=self.project)
        annotation = AnnotationFactory(task=first_task, project=self.project)
        storage = S3ExportStorage.objects.create(project=self.project, bucket='bucket')
        next_task = TaskFactory(project=self.project)
        # Preserve the old task in this worker's instance, as an outbox worker
        # loaded before a concurrent committed task reassignment would do.
        self.assertEqual(annotation.task.pk, first_task.pk)
        Annotation.objects.filter(pk=annotation.pk).update(task=next_task)
        with patch.object(S3ExportStorage, 'get_client_and_resource') as provider:
            with self.assertRaisesRegex(ValueError, 'durable S3 export request identity'):
                storage.save_annotation(annotation)
        provider.assert_not_called()
        record = StorageOperation.objects.get(project=self.project, operation='annotation_export')
        self.assertEqual(record.state, StorageOperation.State.UNCERTAIN)
        self.assertTrue(record.target_object_token)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_s3_put_version_is_bound_to_deletion_intent(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(
            project=self.project,
            bucket='issue86-test',
            prefix='data',
            can_delete_objects=True,
            aws_access_key_id='synthetic-access',
            aws_secret_access_key='synthetic-secret',
        )
        with (
            patch.object(S3ExportStorage, 'get_client_and_resource') as provider,
            patch.object(S3ExportStorage, '_get_serialized_data', return_value={'test': True}),
        ):
            resource = MagicMock()
            provider.return_value = (MagicMock(), resource)
            resource.Object.return_value.put.return_value = {'VersionId': 'version-one'}
            storage.save_annotation(annotation)
            provider.assert_called_once_with(single_attempt=True)
        link = S3ExportStorageLink.objects.get(storage=storage, annotation=annotation)
        export_record = StorageOperation.objects.get(project=self.project, operation='annotation_export')
        self.assertEqual(link.pinned_version_id, 'version-one')
        self.assertTrue(link.pinned_storage_binding)
        self.assertEqual(
            resource.Object.return_value.put.call_args.kwargs['Metadata']['ls-export-token'], link.pinned_object_token
        )
        self.assertEqual(
            (
                export_record.state,
                export_record.subject_id,
                export_record.target_key,
                export_record.target_object_token,
                export_record.target_version_id,
            ),
            (
                StorageOperation.State.COMPLETED,
                annotation.pk,
                link.pinned_key,
                link.pinned_object_token,
                'version-one',
            ),
        )
        expected_binding = link.pinned_storage_binding
        with patch.object(S3ExportStorage, 'delete_annotation') as delete:
            annotation.delete()
        delete.assert_not_called()
        record = StorageOperation.objects.get(project=self.project, operation='annotation_delete_pending')
        self.assertEqual(record.target_object_token, link.pinned_object_token)
        self.assertEqual((record.target_version_id, record.target_storage_binding), ('version-one', expected_binding))

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_s3_request_identity_is_committed_before_put_and_survives_lost_response(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(
            project=self.project,
            bucket='issue86-test',
            prefix='data',
            aws_access_key_id='synthetic-access',
            aws_secret_access_key='synthetic-secret',
        )
        observed = []

        def inspect_from_another_connection():
            close_old_connections()
            try:
                return (
                    StorageOperation.objects.filter(project=self.project, operation='annotation_export')
                    .values(
                        'state',
                        'subject_id',
                        'target_key',
                        'target_object_token',
                        'target_storage_binding',
                    )
                    .get()
                )
            finally:
                connection.close()

        def lost_response(*args, **kwargs):
            with ThreadPoolExecutor(max_workers=1) as pool:
                observed.append((pool.submit(inspect_from_another_connection).result(timeout=10), kwargs))
            raise TimeoutError('synthetic lost response')

        with patch.object(S3ExportStorage, 'get_client_and_resource') as provider, patch.object(
            S3ExportStorage, '_get_serialized_data', return_value={'test': True}
        ):
            resource = MagicMock()
            provider.return_value = (MagicMock(), resource)
            resource.Object.return_value.put.side_effect = lost_response
            with self.assertRaisesRegex(TimeoutError, 'synthetic lost response'):
                storage.save_annotation(annotation)
        self.assertEqual(len(observed), 1)
        before_send, request = observed[0]
        record = StorageOperation.objects.get(project=self.project, operation='annotation_export')
        self.assertEqual(before_send['state'], StorageOperation.State.RUNNING)
        self.assertEqual(before_send['subject_id'], annotation.pk)
        self.assertEqual(before_send['target_key'], record.target_key)
        self.assertTrue(before_send['target_storage_binding'])
        self.assertEqual(before_send['target_object_token'], request['Metadata']['ls-export-token'])
        self.assertEqual(record.target_object_token, before_send['target_object_token'])
        self.assertEqual(record.state, StorageOperation.State.UNCERTAIN)
        self.assertIsNone(record.target_version_id)
        self.assertFalse(S3ExportStorageLink.objects.filter(storage=storage, annotation=annotation).exists())
        with transaction.atomic(), self.assertRaisesRegex(ValidationError, 'STORAGE_NOT_DRAINED'):
            lock_drained_project(self.project.pk)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_s3_identity_persistence_failure_prevents_provider_request(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(project=self.project, bucket='issue86-test')
        with patch(
            'io_storages.s3.version_provenance.prepare_s3_export_request', side_effect=OSError('database unavailable')
        ), patch.object(S3ExportStorage, 'get_client_and_resource') as provider:
            with self.assertRaisesRegex(OSError, 'database unavailable'):
                storage.save_annotation(annotation)
            provider.assert_not_called()
        record = StorageOperation.objects.get(project=self.project, operation='annotation_export')
        self.assertEqual(record.state, StorageOperation.State.UNCERTAIN)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_s3_response_record_failure_keeps_original_request_blocking(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(
            project=self.project,
            bucket='issue86-test',
            aws_access_key_id='synthetic-access',
            aws_secret_access_key='synthetic-secret',
        )
        resource = MagicMock()
        resource.Object.return_value.put.return_value = {'VersionId': 'version-one'}
        with patch.object(
            S3ExportStorage, 'get_client_and_resource', return_value=(MagicMock(), resource)
        ), patch.object(S3ExportStorage, '_get_serialized_data', return_value={'test': True}), patch(
            'io_storages.s3.version_provenance.record_s3_export_response', side_effect=OSError('write failed')
        ):
            with self.assertRaisesRegex(OSError, 'write failed'):
                storage.save_annotation(annotation)
        record = StorageOperation.objects.get(project=self.project, operation='annotation_export')
        link = S3ExportStorageLink.objects.get(storage=storage, annotation=annotation)
        self.assertEqual(record.state, StorageOperation.State.UNCERTAIN)
        self.assertEqual(record.target_object_token, link.pinned_object_token)
        self.assertEqual(link.pinned_version_id, 'version-one')
        self.assertIsNone(record.target_version_id)
        resource.Object.return_value.put.assert_called_once()
        with transaction.atomic(), self.assertRaisesRegex(ValidationError, 'STORAGE_NOT_DRAINED'):
            lock_drained_project(self.project.pk)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_s3_binding_change_after_identity_commit_prevents_provider_request(self):
        self.require_pg()
        from io_storages.s3.version_provenance import prepare_s3_export_request

        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(
            project=self.project,
            bucket='issue86-test',
            aws_access_key_id='synthetic-access',
            aws_secret_access_key='synthetic-secret',
        )

        def change_binding(instance, subject, record):
            identity = prepare_s3_export_request(instance, subject, record)
            instance.bucket = 'changed-after-identity'
            return identity

        with patch(
            'io_storages.s3.version_provenance.prepare_s3_export_request', side_effect=change_binding
        ), patch.object(S3ExportStorage, 'get_client_and_resource') as provider:
            with self.assertRaisesRegex(ValueError, 'binding changed'):
                storage.save_annotation(annotation)
            provider.assert_not_called()
        record = StorageOperation.objects.get(project=self.project, operation='annotation_export')
        self.assertEqual(record.state, StorageOperation.State.UNCERTAIN)
        self.assertTrue(record.target_object_token)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_s3_preflight_cancel_does_not_reconcile_a_submitted_request(self):
        self.require_pg()
        record = StorageOperation.objects.create(
            project=self.project,
            storage_model='io_storages.s3exportstorage',
            storage_id=1,
            operation='annotation_export',
            lifecycle_revision=0,
            state=StorageOperation.State.UNCERTAIN,
            subject_id=1,
            target_key='synthetic/key',
            target_object_token='a' * 32,
            effect_payload={'issue86_s3_preflight': 'v2'},
        )
        with self.assertRaises(ValidationError):
            cancel_unsubmitted_s3_preflight(self.project.pk, record.pk, local_principal(self.manager), reason='stop')
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.UNCERTAIN)

    @override_settings(
        ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED=True,
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_storage_inventory_exposes_only_safe_s3_preflight_capability(self):
        self.require_pg()
        common = dict(
            project=self.project,
            storage_model='io_storages.s3exportstorage',
            storage_id=1,
            operation='annotation_export',
            lifecycle_revision=0,
            subject_id=1,
            target_key='synthetic/key',
            effect_payload={'issue86_s3_preflight': 'v2'},
        )
        allowed = StorageOperation.objects.create(**common, state=StorageOperation.State.RUNNING)
        sent = StorageOperation.objects.create(
            **common, state=StorageOperation.State.UNCERTAIN, target_object_token='a' * 32
        )
        versioned = StorageOperation.objects.create(
            **common, state=StorageOperation.State.UNCERTAIN, target_version_id='synthetic-version'
        )
        old = StorageOperation.objects.create(
            **{**common, 'effect_payload': {'issue86_s3_preflight': 'v1'}}, state=StorageOperation.State.RUNNING
        )
        client = APIClient()
        client.force_authenticate(self.manager)
        response = client.get(f'/api/projects/{self.project.pk}/storage-operations/')
        self.assertEqual(response.status_code, 200, response.content)
        entries = {str(entry['id']): entry for entry in response.data['operations']}
        self.assertTrue(entries[str(allowed.pk)]['can_cancel_s3_preflight'])
        for record in (sent, versioned, old):
            self.assertFalse(entries[str(record.pk)]['can_cancel_s3_preflight'])
        for entry in entries.values():
            self.assertNotIn('effect_payload', entry)
            self.assertNotIn('target_object_token', entry)
            self.assertNotIn('target_version_id', entry)
        self.assertNotIn('a' * 32, str(response.data))

    @override_settings(
        ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED=True,
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_stale_s3_preflight_capability_cannot_cancel_a_submitted_request(self):
        self.require_pg()
        record = StorageOperation.objects.create(
            project=self.project,
            storage_model='io_storages.s3exportstorage',
            storage_id=1,
            operation='annotation_export',
            lifecycle_revision=0,
            state=StorageOperation.State.RUNNING,
            subject_id=1,
            target_key='synthetic/key',
            effect_payload={'issue86_s3_preflight': 'v2'},
        )
        client = APIClient()
        client.force_authenticate(self.manager)
        inventory = client.get(f'/api/projects/{self.project.pk}/storage-operations/')
        self.assertTrue(inventory.data['operations'][0]['can_cancel_s3_preflight'])
        StorageOperation.objects.filter(pk=record.pk).update(target_object_token='a' * 32)
        response = client.post(
            f'/api/projects/{self.project.pk}/storage-operations/{record.pk}/resolve/',
            {'action': 'cancel_s3_preflight', 'reason': 'stale page', 'attested': False},
            format='json',
        )
        self.assertEqual(response.status_code, 400, response.content)
        record.refresh_from_db()
        self.assertEqual((record.state, record.target_object_token), (StorageOperation.State.RUNNING, 'a' * 32))

    @override_settings(
        ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED=True,
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_s3_preflight_cancel_public_api_requires_manager_and_no_attestation(self):
        self.require_pg()
        record = StorageOperation.objects.create(
            project=self.project,
            storage_model='io_storages.s3exportstorage',
            storage_id=1,
            operation='annotation_export',
            lifecycle_revision=0,
            state=StorageOperation.State.RUNNING,
            subject_id=1,
            target_key='synthetic/key',
            effect_payload={'issue86_s3_preflight': 'v2'},
        )
        url = f'/api/projects/{self.project.pk}/storage-operations/{record.pk}/resolve/'
        body = {'action': 'cancel_s3_preflight', 'reason': 'stop', 'attested': False}
        reviewer = UserFactory(active_organization=self.org)
        self.org.add_user(reviewer)
        ProjectMember.objects.create(project=self.project, user=reviewer, role='reviewer')
        client = APIClient()
        client.force_authenticate(reviewer)
        self.assertIn(client.post(url, body, format='json').status_code, (403, 404))
        client.force_authenticate(self.manager)
        self.assertEqual(client.post(url, {**body, 'attested': True}, format='json').status_code, 400)
        self.assertEqual(client.post(url, body, format='json').status_code, 204)
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.CANCELLED)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_cancel_s3_preflight_checks_scope_authorization_and_submission(self):
        self.require_pg()
        reviewer = UserFactory(active_organization=self.org)
        self.org.add_user(reviewer)
        ProjectMember.objects.create(project=self.project, user=reviewer, role='reviewer')
        record = StorageOperation.objects.create(
            project=self.project,
            storage_model='io_storages.s3exportstorage',
            storage_id=1,
            operation='annotation_export',
            lifecycle_revision=0,
            state=StorageOperation.State.RUNNING,
            subject_id=1,
            target_key='synthetic/key',
            effect_payload={'issue86_s3_preflight': 'v2'},
        )
        with self.assertRaises(PermissionDenied):
            cancel_unsubmitted_s3_preflight(self.project.pk, record.pk, local_principal(reviewer), reason='stop')
        other = ProjectFactory(organization=self.org, title='test-active')
        with self.assertRaises(ValidationError):
            cancel_unsubmitted_s3_preflight(other.pk, record.pk, local_principal(self.manager), reason='stop')
        for payload, token in (({'issue86_s3_preflight': 'v1'}, None), ({'issue86_s3_preflight': 'v2'}, 'a' * 32)):
            record.effect_payload, record.target_object_token = payload, token
            record.save(update_fields=['effect_payload', 'target_object_token'])
            with self.assertRaises(ValidationError):
                cancel_unsubmitted_s3_preflight(
                    self.project.pk, record.pk, local_principal(self.manager), reason='stop'
                )
        record.effect_payload, record.target_object_token = {'issue86_s3_preflight': 'v2'}, None
        record.save(update_fields=['effect_payload', 'target_object_token'])
        cancel_unsubmitted_s3_preflight(self.project.pk, record.pk, local_principal(self.manager), reason='stop')
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.CANCELLED)
        self.assertEqual((record.resolved_by_id, record.resolution_reason), (self.manager.pk, 'stop'))

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_cancel_s3_preflight_prevents_paused_worker_from_sending(self):
        self.require_pg()
        from io_storages.s3.version_provenance import prepare_s3_export_request

        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(project=self.project, bucket='bucket')
        paused, release = threading.Event(), threading.Event()

        def prepare(instance, subject, record):
            paused.set()
            if not release.wait(20):
                raise TimeoutError('fixture release timed out')
            return prepare_s3_export_request(instance, subject, record)

        def send():
            close_old_connections()
            try:
                storage.save_annotation(annotation)
            finally:
                connection.close()

        with patch('io_storages.s3.version_provenance.prepare_s3_export_request', side_effect=prepare), patch.object(
            S3ExportStorage, 'get_client_and_resource'
        ) as provider, ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(send)
            try:
                self.assertTrue(paused.wait(20))
                record = StorageOperation.objects.get(project=self.project, operation='annotation_export')
                cancel_unsubmitted_s3_preflight(
                    self.project.pk, record.pk, local_principal(self.manager), reason='stop'
                )
            finally:
                release.set()
            with self.assertRaises(S3ExportTargetBusy):
                future.result(timeout=20)
        provider.assert_not_called()
        record.refresh_from_db()
        self.assertEqual((record.state, record.resolution_reason), (StorageOperation.State.CANCELLED, 'stop'))
        self.assertIsNone(record.target_object_token)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_cancel_paused_s3_outbox_preflight_requeues_parent(self):
        self.require_pg()
        from io_storages.s3.version_provenance import prepare_s3_export_request

        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(project=self.project, bucket='bucket')
        parent = StorageOperation.objects.create(
            project=self.project,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
            operation='annotation_export_v3_queued',
            lifecycle_revision=0,
            subject_id=annotation.pk,
            state=StorageOperation.State.QUEUED,
        )
        paused, release = threading.Event(), threading.Event()

        def prepare(instance, subject, record):
            paused.set()
            if not release.wait(20):
                raise TimeoutError('fixture release timed out')
            return prepare_s3_export_request(instance, subject, record)

        def run():
            close_old_connections()
            try:
                annotation_export_v3(parent.pk)
            finally:
                connection.close()

        with patch('io_storages.s3.version_provenance.prepare_s3_export_request', side_effect=prepare), patch.object(
            S3ExportStorage, 'get_client_and_resource'
        ) as provider, ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run)
            try:
                self.assertTrue(paused.wait(20))
                record = StorageOperation.objects.get(project=self.project, operation='annotation_export')
                cancel_unsubmitted_s3_preflight(
                    self.project.pk, record.pk, local_principal(self.manager), reason='stop'
                )
            finally:
                release.set()
            future.result(timeout=20)
        provider.assert_not_called()
        parent.refresh_from_db()
        record.refresh_from_db()
        self.assertEqual(
            (parent.state, record.state), (StorageOperation.State.QUEUED, StorageOperation.State.CANCELLED)
        )
        self.assertGreater(parent.next_dispatch_at, timezone.now())

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_s3_changed_preflight_key_is_cancelled_before_submission(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(project=self.project, bucket='bucket')
        with patch.object(S3ExportStorageLink, 'get_key', side_effect=['old-key', 'new-key']), patch.object(
            S3ExportStorage, 'get_client_and_resource'
        ) as provider:
            with self.assertRaisesRegex(S3ExportTargetBusy, 'target changed'):
                storage.save_annotation(annotation)
        provider.assert_not_called()
        record = StorageOperation.objects.get(project=self.project, operation='annotation_export')
        self.assertEqual(record.state, StorageOperation.State.CANCELLED)
        self.assertEqual(record.target_key, 'old-key')
        self.assertIsNone(record.target_object_token)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_stale_s3_storage_configuration_does_not_write_old_destination(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(project=self.project, bucket='bucket', prefix='old')
        S3ExportStorage.objects.filter(pk=storage.pk).update(prefix='new')
        with patch.object(S3ExportStorage, 'get_client_and_resource') as provider:
            with self.assertRaisesRegex(ValueError, 'configuration changed'):
                storage.save_annotation(annotation)
        provider.assert_not_called()
        record = StorageOperation.objects.get(project=self.project, operation='annotation_export')
        self.assertEqual(record.state, StorageOperation.State.CANCELLED)
        self.assertTrue(record.target_key.startswith('old/'))
        self.assertIsNone(record.target_object_token)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_uncertain_s3_key_in_another_project_blocks_before_provider(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(project=self.project, bucket='bucket', prefix='synthetic')
        other = ProjectFactory(organization=self.org, title='test-active')
        key = 'synthetic/' + S3ExportStorageLink.get_key(annotation)
        blocker = StorageOperation.objects.create(
            project=other,
            storage_model='io_storages.s3exportstorage',
            storage_id=999,
            operation='annotation_export',
            lifecycle_revision=0,
            state=StorageOperation.State.UNCERTAIN,
            target_key=key,
        )
        with patch.object(S3ExportStorage, 'get_client_and_resource') as provider:
            with self.assertRaises(S3ExportTargetBusy):
                storage.save_annotation(annotation)
        provider.assert_not_called()
        attempt = StorageOperation.objects.get(project=self.project, operation='annotation_export')
        self.assertEqual(attempt.state, StorageOperation.State.CANCELLED)
        self.assertEqual(attempt.target_key, key)
        self.assertIsNone(attempt.target_object_token)
        blocker.refresh_from_db()
        self.assertEqual(blocker.state, StorageOperation.State.UNCERTAIN)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_uncertain_unidentified_s3_export_blocks_new_keys(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(project=self.project, bucket='bucket')
        StorageOperation.objects.create(
            project=self.project,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
            operation='annotation_export',
            lifecycle_revision=0,
            state=StorageOperation.State.UNCERTAIN,
        )
        with patch.object(S3ExportStorage, 'get_client_and_resource') as provider:
            with self.assertRaises(S3ExportTargetBusy):
                storage.save_annotation(annotation)
        provider.assert_not_called()
        self.assertEqual(StorageOperation.objects.filter(state=StorageOperation.State.UNCERTAIN).count(), 1)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_unidentified_running_legacy_s3_export_blocks_new_keys(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(project=self.project, bucket='bucket')
        StorageOperation.objects.create(
            project=self.project,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
            operation='annotation_export',
            lifecycle_revision=0,
            state=StorageOperation.State.RUNNING,
        )
        with patch.object(S3ExportStorage, 'get_client_and_resource') as provider:
            with self.assertRaises(S3ExportTargetBusy):
                storage.save_annotation(annotation)
        provider.assert_not_called()
        self.assertEqual(StorageOperation.objects.filter(state=StorageOperation.State.RUNNING).count(), 1)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_previous_v1_unbound_running_record_still_blocks_new_export(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(project=self.project, bucket='bucket')
        StorageOperation.objects.create(
            project=self.project,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
            operation='annotation_export',
            lifecycle_revision=0,
            state=StorageOperation.State.RUNNING,
            effect_payload={'issue86_s3_preflight': 'v1'},
        )
        with patch.object(S3ExportStorage, 'get_client_and_resource') as provider:
            with self.assertRaises(S3ExportTargetBusy):
                storage.save_annotation(annotation)
        provider.assert_not_called()

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_blocked_s3_outbox_is_requeued_and_succeeds_after_blocker_resolves(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(project=self.project, bucket='bucket', prefix='synthetic')
        key = 'synthetic/' + S3ExportStorageLink.get_key(annotation)
        blocker = StorageOperation.objects.create(
            project=self.project,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
            operation='annotation_export',
            lifecycle_revision=0,
            state=StorageOperation.State.UNCERTAIN,
            target_key=key,
        )
        queued = StorageOperation.objects.create(
            project=self.project,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
            operation='annotation_export_v3_queued',
            lifecycle_revision=0,
            subject_id=annotation.pk,
            state=StorageOperation.State.QUEUED,
        )
        with patch.object(S3ExportStorage, 'get_client_and_resource') as provider:
            annotation_export_v3(queued.pk)
            provider.assert_not_called()
        queued.refresh_from_db()
        self.assertEqual(queued.state, StorageOperation.State.QUEUED)
        self.assertGreater(queued.next_dispatch_at, timezone.now())
        self.assertEqual(
            StorageOperation.objects.filter(
                project=self.project, state=StorageOperation.State.CANCELLED, operation='annotation_export'
            ).count(),
            1,
        )
        # Only the isolated fixture may resolve this synthetic blocker directly.
        blocker.state = StorageOperation.State.COMPLETED
        blocker.save(update_fields=['state'])
        resource = MagicMock()
        resource.Object.return_value.put.return_value = {'VersionId': 'fixture-version'}
        with patch.object(
            S3ExportStorage, 'get_client_and_resource', return_value=(MagicMock(), resource)
        ), patch.object(S3ExportStorage, '_get_serialized_data', return_value={'synthetic': True}):
            annotation_export_v3(queued.pk)
        queued.refresh_from_db()
        self.assertEqual(queued.state, StorageOperation.State.COMPLETED)
        resource.Object.return_value.put.assert_called_once()

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_busy_exception_after_provider_submission_is_not_requeued(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(project=self.project, bucket='bucket')
        queued = StorageOperation.objects.create(
            project=self.project,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
            operation='annotation_export_v3_queued',
            lifecycle_revision=0,
            subject_id=annotation.pk,
            state=StorageOperation.State.QUEUED,
        )
        resource = MagicMock()
        resource.Object.return_value.put.side_effect = S3ExportTargetBusy('after provider submission')
        with patch.object(
            S3ExportStorage, 'get_client_and_resource', return_value=(MagicMock(), resource)
        ), patch.object(S3ExportStorage, '_get_serialized_data', return_value={'synthetic': True}):
            with self.assertRaises(S3ExportTargetBusy):
                annotation_export_v3(queued.pk)
        queued.refresh_from_db()
        self.assertEqual(queued.state, StorageOperation.State.UNCERTAIN)
        child = StorageOperation.objects.get(project=self.project, operation='annotation_export')
        self.assertEqual(child.state, StorageOperation.State.UNCERTAIN)
        self.assertTrue(child.target_key)
        resource.Object.return_value.put.assert_called_once()

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_s3_paused_before_binding_blocks_later_same_key(self):
        self.require_pg()
        from io_storages.s3.version_provenance import prepare_s3_export_request

        other = ProjectFactory(organization=self.org, title='test-active')
        first = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        second = AnnotationFactory(task=TaskFactory(project=other), project=other)
        first_storage = S3ExportStorage.objects.create(project=self.project, bucket='bucket-one')
        second_storage = S3ExportStorage.objects.create(project=other, bucket='bucket-two')
        paused, release = threading.Event(), threading.Event()

        def prepare(storage, annotation, record):
            if annotation.pk == first.pk:
                paused.set()
                if not release.wait(20):
                    raise TimeoutError('fixture release timed out')
            return prepare_s3_export_request(storage, annotation, record)

        def send(storage, annotation):
            close_old_connections()
            try:
                storage.save_annotation(annotation)
            finally:
                connection.close()

        resource = MagicMock()
        resource.Object.return_value.put.return_value = {'VersionId': 'fixture-version'}
        with patch.object(S3ExportStorageLink, 'get_key', return_value='shared-key'), patch(
            'io_storages.s3.version_provenance.prepare_s3_export_request', side_effect=prepare
        ), patch.object(
            S3ExportStorage, 'get_client_and_resource', return_value=(MagicMock(), resource)
        ), patch.object(
            S3ExportStorage, '_get_serialized_data', return_value={'synthetic': True}
        ), ThreadPoolExecutor(
            max_workers=2
        ) as pool:
            future = pool.submit(send, first_storage, first)
            try:
                self.assertTrue(paused.wait(20))
                first_record = StorageOperation.objects.get(project=self.project, operation='annotation_export')
                self.assertEqual(first_record.target_key, 'shared-key')
                self.assertIsNone(first_record.target_object_token)
                blocked = pool.submit(send, second_storage, second)
                with self.assertRaises(S3ExportTargetBusy):
                    blocked.result(timeout=20)
                resource.Object.return_value.put.assert_not_called()
            finally:
                release.set()
            future.result(timeout=20)
        resource.Object.return_value.put.assert_called_once()

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_parallel_s3_same_key_across_projects_sends_at_most_one_request(self):
        self.require_pg()
        other = ProjectFactory(organization=self.org, title='test-active')
        first = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        second = AnnotationFactory(task=TaskFactory(project=other), project=other)
        first_storage = S3ExportStorage.objects.create(project=self.project, bucket='bucket-one')
        second_storage = S3ExportStorage.objects.create(project=other, bucket='bucket-two')
        entered, release = threading.Event(), threading.Event()

        def delayed_put(**kwargs):
            entered.set()
            if not release.wait(20):
                raise TimeoutError('fixture release timed out')
            return {'VersionId': 'fixture-version'}

        resource = MagicMock()
        resource.Object.return_value.put.side_effect = delayed_put

        def send(storage, annotation):
            close_old_connections()
            try:
                storage.save_annotation(annotation)
            finally:
                connection.close()

        with patch.object(S3ExportStorageLink, 'get_key', return_value='shared-key'), patch.object(
            S3ExportStorage, 'get_client_and_resource', return_value=(MagicMock(), resource)
        ), patch.object(S3ExportStorage, '_get_serialized_data', return_value={'synthetic': True}), ThreadPoolExecutor(
            max_workers=2
        ) as pool:
            future = pool.submit(send, first_storage, first)
            try:
                self.assertTrue(entered.wait(20))
                blocked = pool.submit(send, second_storage, second)
                with self.assertRaises(S3ExportTargetBusy):
                    blocked.result(timeout=20)
            finally:
                release.set()
            future.result(timeout=20)
        resource.Object.return_value.put.assert_called_once()
        states = list(StorageOperation.objects.filter(operation='annotation_export').values_list('state', flat=True))
        self.assertCountEqual(states, [StorageOperation.State.CANCELLED, StorageOperation.State.COMPLETED])

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_parallel_s3_exports_keep_each_request_identity_separate(self):
        self.require_pg()
        annotations = [
            AnnotationFactory(task=TaskFactory(project=self.project), project=self.project) for _ in range(2)
        ]
        storage = S3ExportStorage.objects.create(
            project=self.project,
            bucket='issue86-test',
            aws_access_key_id='synthetic-access',
            aws_secret_access_key='synthetic-secret',
        )
        start = threading.Barrier(2)

        def export(annotation_id):
            close_old_connections()
            try:
                annotation = Annotation.objects.get(pk=annotation_id)
                start.wait(timeout=10)
                storage.save_annotation(annotation)
            finally:
                connection.close()

        resource = MagicMock()
        resource.Object.side_effect = lambda bucket, key: MagicMock(
            put=MagicMock(return_value={'VersionId': 'v-' + key})
        )
        with patch.object(
            S3ExportStorage, 'get_client_and_resource', return_value=(MagicMock(), resource)
        ), patch.object(S3ExportStorage, '_get_serialized_data', return_value={'test': True}), ThreadPoolExecutor(
            max_workers=2
        ) as pool:
            futures = [pool.submit(export, annotation.pk) for annotation in annotations]
            for future in futures:
                future.result(timeout=30)
        records = list(StorageOperation.objects.filter(project=self.project, operation='annotation_export'))
        self.assertEqual(len(records), 2)
        self.assertEqual(len({record.target_object_token for record in records}), 2)
        for record in records:
            link = S3ExportStorageLink.objects.get(storage=storage, annotation_id=record.subject_id)
            self.assertEqual(
                (record.state, record.target_key, record.target_object_token, record.target_version_id),
                (StorageOperation.State.COMPLETED, link.pinned_key, link.pinned_object_token, link.pinned_version_id),
            )

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_s3_null_version_overwrite_invalidates_previous_pin(self):
        self.require_pg()
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(
            project=self.project,
            bucket='issue86-test',
            aws_access_key_id='synthetic-access',
            aws_secret_access_key='synthetic-secret',
        )
        with (
            patch.object(S3ExportStorage, 'get_client_and_resource') as provider,
            patch.object(S3ExportStorage, '_get_serialized_data', return_value={'test': True}),
        ):
            resource = MagicMock()
            provider.return_value = (MagicMock(), resource)
            resource.Object.return_value.put.side_effect = [
                {'VersionId': 'version-one'},
                {'VersionId': 'null'},
            ]
            storage.save_annotation(annotation)
            storage.save_annotation(annotation)
        link = S3ExportStorageLink.objects.get(storage=storage, annotation=annotation)
        self.assertIsNone(link.pinned_version_id)
        self.assertIsNone(link.pinned_storage_binding)
        self.assertIsNone(link.pinned_object_token)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_replay_command_excludes_manual_deletions(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-input')
        queued = prepare_storage_dispatch(storage, 'import_sync')
        StorageOperation.objects.filter(pk=queued.pk).update(started_at=timezone.now() - timedelta(minutes=2))
        StorageOperation.objects.create(
            project=self.project,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
            operation='annotation_delete_pending',
            lifecycle_revision=0,
            state=StorageOperation.State.QUEUED,
        )
        with patch('io_storages.management.commands.replay_storage_outbox.redispatch_queued_operation') as replay:
            call_command('replay_storage_outbox', project_id=self.project.pk, limit=10, stdout=io.StringIO())
        replay.assert_called_once_with(queued.pk, expected_attempt=1)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_concurrent_replay_reserves_one_attempt(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-input')
        queued = prepare_storage_dispatch(storage, 'import_sync')
        StorageOperation.objects.filter(pk=queued.pk).update(started_at=timezone.now() - timedelta(minutes=2))
        attempted = []

        def run():
            close_old_connections()
            try:
                call_command('replay_storage_outbox', project_id=self.project.pk, limit=10, stdout=io.StringIO())
            finally:
                connection.close()

        def capture_attempt(operation_id, *, expected_attempt):
            attempted.append((operation_id, expected_attempt))

        with patch(
            'io_storages.management.commands.replay_storage_outbox.redispatch_queued_operation',
            side_effect=capture_attempt,
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(run), pool.submit(run)]
                for future in futures:
                    future.result(30)
        self.assertEqual(attempted, [(queued.pk, 1)])
        queued.refresh_from_db()
        self.assertEqual(queued.dispatch_attempts, 1)
        self.assertIsNotNone(queued.next_dispatch_at)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_annotation_outbox_v3_is_fenced_from_legacy_operation_and_entrypoint(self):
        self.require_pg()
        task = TaskFactory(project=self.project)
        annotation = AnnotationFactory(task=task, project=self.project)
        storage = LocalFilesExportStorage.objects.create(project=self.project, path='/tmp/issue86-output')
        legacy = StorageOperation.objects.create(
            project=self.project,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
            operation='annotation_export_queued',
            lifecycle_revision=0,
            subject_id=annotation.pk,
            state=StorageOperation.State.QUEUED,
        )
        current = StorageOperation.objects.create(
            project=self.project,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
            operation='annotation_export_v3_queued',
            lifecycle_revision=0,
            subject_id=annotation.pk,
            state=StorageOperation.State.QUEUED,
        )
        with patch('io_storages.localfiles.models.open_local_file_write') as output:
            with self.assertRaises(ValueError):
                annotation_export_v2(current.pk)
            with self.assertRaises(ValueError):
                annotation_export_v2(legacy.pk)
            with self.assertRaises(ValidationError):
                annotation_export_v3(legacy.pk)
            # A previous v2 worker uses this exact operation when claiming.
            with self.assertRaises(ValidationError):
                claim_storage_dispatch(storage, current.pk, 'annotation_export_queued')
            with self.assertRaises(ValidationError):
                redispatch_queued_operation(legacy.pk)
            with self.assertRaises(ValueError):
                dispatch_queued_annotation_export(legacy.pk)
        output.assert_not_called()
        for record in (legacy, current):
            record.refresh_from_db()
            self.assertEqual(record.state, StorageOperation.State.QUEUED)
        with transaction.atomic(), self.assertRaisesRegex(ValidationError, 'STORAGE_NOT_DRAINED'):
            lock_drained_project(self.project.pk)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_replay_does_not_convert_legacy_annotation_outbox(self):
        self.require_pg()
        storage = LocalFilesExportStorage.objects.create(project=self.project, path='/tmp/issue86-output')
        records = [
            StorageOperation.objects.create(
                project=self.project,
                storage_model=storage._meta.label_lower,
                storage_id=storage.pk,
                operation=operation,
                lifecycle_revision=0,
                subject_id=1,
                state=StorageOperation.State.QUEUED,
            )
            for operation in ('annotation_export_queued', 'annotation_export_v3_queued')
        ]
        StorageOperation.objects.filter(pk__in=[record.pk for record in records]).update(
            started_at=timezone.now() - timedelta(minutes=3),
        )
        output = io.StringIO()
        with patch('io_storages.management.commands.replay_storage_outbox.redispatch_queued_operation') as dispatch:
            call_command('replay_storage_outbox', project_id=self.project.pk, stdout=output)
        dispatch.assert_called_once_with(records[1].pk, expected_attempt=1)
        self.assertIn('Legacy annotation exports remain unresolved', output.getvalue())
        records[0].refresh_from_db()
        self.assertEqual(
            (records[0].operation, records[0].state, records[0].dispatch_attempts),
            ('annotation_export_queued', StorageOperation.State.QUEUED, 0),
        )

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_annotation_signal_records_automatic_export(self):
        self.require_pg()
        task = TaskFactory(project=self.project)
        LocalFilesExportStorage.objects.create(project=self.project, path='/tmp/issue86-output')
        with (
            patch.object(LocalFilesExportStorage, '_get_storage_path_or_raise', return_value='/tmp/issue86-output'),
            patch('io_storages.localfiles.models.open_local_file_write', mock_open()) as output,
            patch('io_storages.localfiles.models.is_private_local_path', return_value=False),
        ):
            AnnotationFactory(task=task, project=self.project)
        output.assert_called_once()
        records = list(StorageOperation.objects.filter(project=self.project).values_list('operation', 'state'))
        self.assertCountEqual(
            records,
            [
                ('annotation_export_v3_queued', StorageOperation.State.COMPLETED),
                ('annotation_export', StorageOperation.State.COMPLETED),
            ],
        )

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_rolled_back_annotation_and_outbox_are_atomic(self):
        self.require_pg()
        task = TaskFactory(project=self.project)
        LocalFilesExportStorage.objects.create(project=self.project, path='/tmp/issue86-output')
        with patch('io_storages.localfiles.models.open_local_file_write') as output:
            with self.assertRaisesRegex(RuntimeError, 'rollback'):
                with transaction.atomic():
                    AnnotationFactory(task=task, project=self.project)
                    self.assertEqual(
                        StorageOperation.objects.filter(operation='annotation_export_v3_queued').count(), 1
                    )
                    raise RuntimeError('rollback')
        output.assert_not_called()
        self.assertFalse(Annotation.objects.filter(task=task).exists())
        self.assertFalse(StorageOperation.objects.filter(operation='annotation_export_v3_queued').exists())

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_automatic_export_survives_dispatch_failure_and_can_be_cancelled(self):
        self.require_pg()
        task = TaskFactory(project=self.project)
        LocalFilesExportStorage.objects.create(project=self.project, path='/tmp/issue86-output')
        with patch(
            'io_storages.annotation_outbox.dispatch_queued_annotation_export', side_effect=OSError('queue offline')
        ):
            annotation = AnnotationFactory(task=task, project=self.project)
        record = StorageOperation.objects.get(project=self.project, operation='annotation_export_v3_queued')
        self.assertEqual((record.state, record.subject_id), (StorageOperation.State.QUEUED, annotation.pk))
        with transaction.atomic(), self.assertRaises(ValidationError):
            lock_drained_project(self.project.pk)
        cancel_queued_storage_dispatch(
            self.project.pk, record.pk, local_principal(self.manager), reason='cancel offline job'
        )
        with patch('io_storages.localfiles.models.open_local_file_write') as output:
            with self.assertRaises(ValidationError):
                annotation_export_v3(record.pk)
            output.assert_not_called()

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_queued_automatic_export_can_be_redispatched_once(self):
        self.require_pg()
        task = TaskFactory(project=self.project)
        LocalFilesExportStorage.objects.create(project=self.project, path='/tmp/issue86-output')
        with patch('io_storages.annotation_outbox.dispatch_queued_annotation_export'):
            AnnotationFactory(task=task, project=self.project)
        record = StorageOperation.objects.get(project=self.project, operation='annotation_export_v3_queued')
        with (
            patch.object(LocalFilesExportStorage, '_get_storage_path_or_raise', return_value='/tmp/issue86-output'),
            patch('io_storages.localfiles.models.open_local_file_write', mock_open()) as output,
            patch('io_storages.localfiles.models.is_private_local_path', return_value=False),
            patch(
                'io_storages.annotation_outbox.start_job_async_or_sync',
                side_effect=lambda fn, *args, **kwargs: fn(*args),
            ),
        ):
            dispatch_queued_annotation_export(record.pk)
            with self.assertRaises(ValidationError):
                annotation_export_v3(record.pk)
        output.assert_called_once()
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.COMPLETED)

    @override_settings(
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_transition_requires_drain_and_records_generation(self):
        self.require_pg()
        record = begin_storage_operation(self.project.pk, 'io_storages.test', 1, 'import', expected_revision=0)
        with self.assertRaises(ValidationError):
            transition_project(
                self.project.pk, local_principal(self.manager), expected_revision=0, target='active', reason='start'
            )
        self.project.refresh_from_db()
        self.assertEqual((self.project.lifecycle_state, self.project.lifecycle_revision), ('legacy', 0))
        self.assertFalse(ProjectLifecycleEvent.objects.exists())
        finish_storage_operation(record.pk, succeeded=True)
        transition_project(
            self.project.pk, local_principal(self.manager), expected_revision=0, target='active', reason='start'
        )
        self.project.refresh_from_db()
        self.assertEqual((self.project.lifecycle_state, self.project.lifecycle_revision), ('active', 1))
        event = ProjectLifecycleEvent.objects.get(project=self.project)
        self.assertEqual(
            (event.previous_state, event.next_state, event.actor_id_snapshot), ('legacy', 'active', self.manager.pk)
        )
        with self.assertRaises(ValidationError):
            begin_storage_operation(self.project.pk, 'io_storages.test', 1, 'import', expected_revision=0)
        with self.assertRaises(ValidationError):
            begin_storage_operation(self.project.pk, 'io_storages.test', 1, 'import')
        with self.assertRaises(ValidationError):
            transition_project(
                self.project.pk, local_principal(self.manager), expected_revision=0, target='paused', reason='stale'
            )
        with override_settings(
            ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LegacyProjectOperationProvider'
        ):
            with self.assertRaises(PermissionDenied):
                begin_storage_operation(self.project.pk, 'ioou.test', 1, 'import', expected_revision=1)

    def test_transition_disabled_by_default(self):
        with self.assertRaises(PermissionDenied):
            transition_project(
                self.project.pk, local_principal(self.manager), expected_revision=0, target='active', reason='start'
            )

    def test_public_lifecycle_api_is_off_by_default(self):
        client = APIClient()
        client.force_authenticate(self.manager)
        response = client.get(f'/api/projects/{self.project.pk}/lifecycle/')
        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(response['Cache-Control'], 'private, no-store')

    @override_settings(
        ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED=True,
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_public_transition_checks_actor_payload_and_revision(self):
        self.require_pg()
        client = APIClient()
        client.force_authenticate(self.manager)
        url = f'/api/projects/{self.project.pk}/lifecycle/'
        body = {'target': 'active', 'expected_revision': 0, 'reason': 'start'}
        denied = client.post(url, {**body, 'actor_id': self.manager.pk}, format='json')
        self.assertEqual(denied.status_code, 400, denied.content)
        actor = UserFactory(active_organization=self.org)
        self.org.add_user(actor)
        ProjectMember.objects.create(project=self.project, user=actor, role='annotator')
        client.force_authenticate(actor)
        denied = client.post(url, body, format='json')
        self.assertEqual(denied.status_code, 403, denied.content)
        client.force_authenticate(self.manager)
        accepted = client.post(url, body, format='json')
        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertEqual(accepted.json(), {'state': 'active', 'revision': 1})
        self.assertEqual(accepted['Cache-Control'], 'private, no-store')
        self.assertEqual(ProjectLifecycleEvent.objects.get(project=self.project).actor_id_snapshot, self.manager.pk)
        stale = client.post(url, {'target': 'paused', 'expected_revision': 0, 'reason': 'pause'}, format='json')
        self.assertEqual(stale.status_code, 400, stale.content)

    @override_settings(
        ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED=True,
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_public_reconciliation_cannot_clear_running_or_cross_project(self):
        self.require_pg()
        record = begin_storage_operation(self.project.pk, 'io_storages.localfilesexportstorage', 1, 'export_sync')
        client = APIClient()
        client.force_authenticate(self.manager)
        payload = {'action': 'confirm_uncertain', 'reason': 'checked', 'attested': True}
        url = f'/api/projects/{self.project.pk}/storage-operations/{record.pk}/resolve/'
        denied = client.post(url, payload, format='json')
        self.assertEqual(denied.status_code, 400, denied.content)
        other = ProjectFactory(organization=self.org)
        denied = client.post(
            f'/api/projects/{other.pk}/storage-operations/{record.pk}/resolve/', payload, format='json'
        )
        self.assertEqual(denied.status_code, 400, denied.content)
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.RUNNING)
        finish_storage_operation(record.pk, succeeded=False)
        denied = client.post(url, {**payload, 'attested': False}, format='json')
        self.assertEqual(denied.status_code, 400, denied.content)
        done = client.post(url, payload, format='json')
        self.assertEqual(done.status_code, 204, done.content)
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.RECONCILED)

    @override_settings(
        ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED=True,
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_public_storage_list_is_manager_scoped_and_no_store(self):
        self.require_pg()
        record = begin_storage_operation(self.project.pk, 'io_storages.test', 1, 'export')
        export = Export.objects.create(project=self.project, status=Export.Status.FAILED, title='private title')
        other = ProjectFactory(organization=self.org)
        Export.objects.create(project=other, status=Export.Status.IN_PROGRESS)
        client = APIClient()
        client.force_authenticate(self.manager)
        url = f'/api/projects/{self.project.pk}/storage-operations/'
        response = client.get(url)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertEqual(response.json()['operations'][0]['id'], str(record.pk))
        self.assertEqual(
            response.json()['legacy_export_blockers'],
            {
                'exports': 1,
                'conversions': 0,
                'releases': 0,
                'release_attempts': 0,
                'snapshots': 0,
                'audit_packages': 0,
                'audit_attempts': 0,
                **{name: 0 for name in LEGACY_STORAGE_MODELS},
            },
        )
        self.assertEqual(
            response.json()['legacy_export_details'],
            {
                'items': [{'kind': 'exports', 'id': str(export.pk), 'state': 'failed', 'parent_id': None}],
                'limit': 50,
                'possibly_truncated': False,
                'next_cursor': None,
            },
        )
        self.assertNotIn('private title', response.content.decode())
        actor = UserFactory(active_organization=self.org)
        self.org.add_user(actor)
        ProjectMember.objects.create(project=self.project, user=actor, role='reviewer')
        client.force_authenticate(actor)
        denied = client.get(url)
        self.assertEqual(denied.status_code, 403, denied.content)

    @override_settings(
        ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED=True,
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_public_export_blocker_details_are_bounded(self):
        self.require_pg()
        Export.objects.bulk_create([Export(project=self.project, status=Export.Status.FAILED) for _ in range(51)])
        client = APIClient()
        client.force_authenticate(self.manager)
        response = client.get(f'/api/projects/{self.project.pk}/storage-operations/')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data['legacy_export_blockers']['exports'], 51)
        self.assertEqual(len(response.data['legacy_export_details']['items']), 50)
        self.assertTrue(response.data['legacy_export_details']['possibly_truncated'])
        self.assertTrue(
            all(
                set(item) == {'kind', 'id', 'state', 'parent_id'}
                for item in response.data['legacy_export_details']['items']
            )
        )
        first = response.data['legacy_export_details']
        cursor = first['next_cursor']
        self.assertTrue(cursor)
        following = client.get(f'/api/projects/{self.project.pk}/storage-operations/', {'legacy_cursor': cursor})
        self.assertEqual(following.status_code, 200, following.content)
        second = following.data['legacy_export_details']
        self.assertEqual(len(second['items']), 1)
        self.assertFalse(second['possibly_truncated'])
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(len({item['id'] for item in first['items'] + second['items']}), 51)
        invalid = client.get(f'/api/projects/{self.project.pk}/storage-operations/', {'legacy_cursor': cursor + 'x'})
        self.assertEqual(invalid.status_code, 400, invalid.content)
        now = time.time()
        with patch('django.core.signing.time.time', return_value=now - 7200):
            expired = signing.dumps(
                {
                    'version': 1,
                    'project': self.project.pk,
                    'kind': 'exports',
                    'id': first['items'][-1]['id'],
                },
                salt='issue86.legacy-export-page.v1',
            )
        stale = client.get(f'/api/projects/{self.project.pk}/storage-operations/', {'legacy_cursor': expired})
        self.assertEqual(stale.status_code, 400, stale.content)
        duplicate = client.get(f'/api/projects/{self.project.pk}/storage-operations/?legacy_cursor=a&legacy_cursor=b')
        self.assertEqual(duplicate.status_code, 400, duplicate.content)
        other = ProjectFactory(organization=self.org, created_by=self.manager)
        cross_project = client.get(f'/api/projects/{other.pk}/storage-operations/', {'legacy_cursor': cursor})
        self.assertEqual(cross_project.status_code, 400, cross_project.content)

    @override_settings(
        ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED=True,
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_legacy_storage_blockers_paginate_across_storage_types(self):
        self.require_pg()
        for i in range(51):
            LocalFilesImportStorage.objects.create(
                project=self.project, path=f'/tmp/issue86-import-{i}', status='failed'
            )
        exported = LocalFilesExportStorage.objects.create(
            project=self.project, path='/tmp/issue86-export', status='queued'
        )
        other = ProjectFactory(organization=self.org)
        LocalFilesImportStorage.objects.create(project=other, path='/tmp/issue86-other', status='failed')
        client = APIClient()
        client.force_authenticate(self.manager)
        url = f'/api/projects/{self.project.pk}/storage-operations/'
        first = client.get(url).data
        self.assertEqual(first['legacy_export_blockers']['storage_local_import'], 51)
        self.assertEqual(first['legacy_export_blockers']['storage_local_export'], 1)
        page_one = first['legacy_export_details']
        self.assertEqual(len(page_one['items']), 50)
        self.assertTrue(page_one['possibly_truncated'])
        second_response = client.get(url, {'legacy_cursor': page_one['next_cursor']})
        self.assertEqual(second_response.status_code, 200, second_response.content)
        page_two = second_response.data['legacy_export_details']
        self.assertEqual(len(page_two['items']), 2)
        self.assertFalse(page_two['possibly_truncated'])
        self.assertEqual(
            page_two['items'][-1],
            {
                'kind': 'storage_local_export',
                'id': str(exported.pk),
                'state': 'queued',
                'parent_id': None,
            },
        )
        entries = page_one['items'] + page_two['items']
        self.assertEqual(len({(item['kind'], item['id']) for item in entries}), 52)

    @override_settings(
        ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED=True,
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_storage_operation_cursor_pages_and_rejects_invalid_scope(self):
        self.require_pg()
        StorageOperation.objects.bulk_create(
            [
                StorageOperation(
                    project=self.project,
                    storage_model='io_storages.test',
                    storage_id=1,
                    operation='import_sync',
                    lifecycle_revision=0,
                    state=StorageOperation.State.UNCERTAIN,
                )
                for _ in range(101)
            ]
        )
        Export.objects.bulk_create([Export(project=self.project, status=Export.Status.FAILED) for _ in range(51)])
        other = ProjectFactory(organization=self.org, created_by=self.manager)
        StorageOperation.objects.create(
            project=other,
            storage_model='io_storages.test',
            storage_id=1,
            operation='import_sync',
            lifecycle_revision=0,
            state=StorageOperation.State.UNCERTAIN,
        )
        client = APIClient()
        client.force_authenticate(self.manager)
        url = f'/api/projects/{self.project.pk}/storage-operations/'
        first_response = client.get(url)
        self.assertEqual(first_response.status_code, 200, first_response.content)
        first = first_response.data
        self.assertEqual(len(first['operations']), 100)
        self.assertTrue(first['possibly_truncated'])
        cursor = first['operations_next_cursor']
        self.assertTrue(cursor)
        legacy_cursor = first['legacy_export_details']['next_cursor']
        self.assertTrue(legacy_cursor)
        # Resolving the last record on page one must not invalidate the cursor.
        StorageOperation.objects.filter(pk=first['operations'][-1]['id']).update(
            state=StorageOperation.State.RECONCILED
        )
        second_response = client.get(url, {'storage_cursor': cursor, 'legacy_cursor': legacy_cursor})
        self.assertEqual(second_response.status_code, 200, second_response.content)
        second = second_response.data
        self.assertEqual(len(second['operations']), 1)
        self.assertFalse(second['possibly_truncated'])
        self.assertIsNone(second['operations_next_cursor'])
        self.assertEqual(len(second['legacy_export_details']['items']), 1)
        self.assertIsNone(second['legacy_export_details']['next_cursor'])
        self.assertEqual(len({str(item['id']) for item in first['operations'] + second['operations']}), 101)
        invalid = client.get(url, {'storage_cursor': cursor + 'x'})
        self.assertEqual(invalid.status_code, 400, invalid.content)
        now = time.time()
        with patch('django.core.signing.time.time', return_value=now - 7200):
            stale_cursor = signing.dumps(
                {
                    'version': 1,
                    'project': self.project.pk,
                    'started_at': first['operations'][-1]['started_at'].isoformat(),
                    'id': str(first['operations'][-1]['id']),
                },
                salt='issue86.storage-operation-page.v1',
            )
        expired = client.get(url, {'storage_cursor': stale_cursor})
        self.assertEqual(expired.status_code, 400, expired.content)
        duplicate = client.get(url + '?storage_cursor=a&storage_cursor=b')
        self.assertEqual(duplicate.status_code, 400, duplicate.content)
        wrong_kind = client.get(url, {'legacy_cursor': cursor})
        self.assertEqual(wrong_kind.status_code, 400, wrong_kind.content)
        cross = client.get(f'/api/projects/{other.pk}/storage-operations/', {'storage_cursor': cursor})
        self.assertEqual(cross.status_code, 400, cross.content)

    @override_settings(
        ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED=True,
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_export_blocker_cursor_crosses_integer_and_uuid_kinds(self):
        self.require_pg()
        Export.objects.bulk_create([Export(project=self.project, status=Export.Status.FAILED) for _ in range(49)])
        releases = []
        for number in (1, 2):
            releases.append(
                ApprovedDatasetRelease.objects.create(
                    project=self.project,
                    organization_id_frozen=self.org.pk,
                    actor_id_frozen=self.manager.pk,
                    principal_id='synthetic',
                    idempotency_key=f'page-{number}',
                    request_hash='x',
                    frozen_input={},
                    input_sha256='x',
                    content_sha256='x',
                    state='dispatch_pending',
                )
            )
        attempt = ApprovedDatasetReleaseAttempt.objects.create(release=releases[0], number=1, state='expired')
        client = APIClient()
        client.force_authenticate(self.manager)
        url = f'/api/projects/{self.project.pk}/storage-operations/'
        first = client.get(url).data['legacy_export_details']
        self.assertEqual((len(first['items']), first['items'][-1]['kind']), (50, 'releases'))
        response = client.get(url, {'legacy_cursor': first['next_cursor']})
        self.assertEqual(response.status_code, 200, response.content)
        second = response.data['legacy_export_details']
        self.assertEqual(len(second['items']), 2)
        self.assertEqual(second['items'][0]['kind'], 'releases')
        self.assertNotEqual(first['items'][-1]['id'], second['items'][0]['id'])
        self.assertEqual(
            second['items'][1],
            {
                'kind': 'release_attempts',
                'id': str(attempt.pk),
                'state': 'expired',
                'parent_id': str(releases[0].pk),
            },
        )

    def test_invalid_lifecycle_state_is_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            type(self.project).objects.filter(pk=self.project.pk).update(lifecycle_state='invalid')

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_versioned_sync_persists_before_enqueue_and_cancel_blocks_late_worker(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-input')
        with (
            patch('io_storages.base_models.redis_connected', return_value=True),
            patch('io_storages.base_models.start_job_async_or_sync') as enqueue,
        ):
            enqueue.return_value.id = 'test-job'
            storage.sync()
        function, klass, storage_id, operation_id, operation, attempt = enqueue.call_args.args
        self.assertEqual(function.__name__, 'storage_sync_v3')
        self.assertEqual(enqueue.call_args.kwargs['on_failure'].__name__, 'storage_sync_v2_failure')
        self.assertEqual((klass, storage_id, operation), (LocalFilesImportStorage, storage.pk, 'import_sync'))
        self.assertEqual(attempt, 0)
        record = StorageOperation.objects.get(pk=operation_id)
        self.assertEqual(record.state, StorageOperation.State.QUEUED)
        with transaction.atomic(), self.assertRaises(ValidationError):
            lock_drained_project(self.project.pk)
        cancel_queued_storage_dispatch(self.project.pk, record.pk, local_principal(self.manager), reason='pause')
        with patch.object(LocalFilesImportStorage, 'scan_and_create_links') as scan:
            with self.assertRaises(ValidationError):
                storage_sync_v2(LocalFilesImportStorage, storage.pk, operation_id, operation)
            scan.assert_not_called()
        with transaction.atomic():
            lock_drained_project(self.project.pk)

    def test_dispatch_claim_rejects_cross_storage_and_duplicate(self):
        self.require_pg()
        first = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-one')
        second = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-two')
        record = prepare_storage_dispatch(first, 'import_sync', mark_queued=True)
        with self.assertRaises(ValidationError):
            claim_storage_dispatch(second, record.pk, 'import_sync')
        claim_storage_dispatch(first, record.pk, 'import_sync')
        with self.assertRaises(ValidationError):
            claim_storage_dispatch(first, record.pk, 'import_sync')
        finish_storage_operation(record.pk, succeeded=True)

    def test_queue_status_and_admission_roll_back_together(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-input')
        with patch.object(StorageOperation.objects, 'create', side_effect=RuntimeError('ledger unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'ledger unavailable'):
                prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        storage.refresh_from_db()
        self.assertEqual(storage.status, storage.Status.INITIALIZED)
        self.assertFalse(StorageOperation.objects.filter(project=self.project).exists())
        with patch('io_storages.admission.bind_queued_storage_status', side_effect=RuntimeError('owner unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'owner unavailable'):
                prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        storage.refresh_from_db()
        self.assertEqual(storage.status, storage.Status.INITIALIZED)
        self.assertFalse(StorageOperation.objects.filter(project=self.project).exists())

    def test_cancelled_queued_sync_can_be_queued_again(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-input')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        storage.refresh_from_db()
        self.assertEqual(storage.meta[QUEUE_OWNER_META_KEY], str(record.pk))
        cancel_queued_storage_dispatch(self.project.pk, record.pk, local_principal(self.manager), reason='cancel')
        storage.refresh_from_db()
        self.assertEqual(storage.status, storage.Status.INITIALIZED)
        self.assertNotIn(QUEUE_OWNER_META_KEY, storage.meta)
        second = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        self.assertNotEqual(second.pk, record.pk)

    def test_cancelled_dispatch_preserves_unowned_legacy_queue_status(self):
        self.require_pg()
        for meta in ({'attempts': 2}, {QUEUE_OWNER_META_KEY: str(uuid.uuid4())}):
            with self.subTest(meta=meta):
                storage = LocalFilesImportStorage.objects.create(
                    project=self.project, path='/tmp/issue86-legacy-owner'
                )
                record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
                # An older producer can replace StorageInfo metadata without
                # creating or claiming this newer ledger entry.
                LocalFilesImportStorage.objects.filter(pk=storage.pk).update(meta=meta)
                cancel_queued_storage_dispatch(
                    self.project.pk, record.pk, local_principal(self.manager), reason='cancel'
                )
                storage.refresh_from_db()
                record.refresh_from_db()
                self.assertEqual(record.state, StorageOperation.State.CANCELLED)
                self.assertEqual(storage.status, storage.Status.QUEUED)
                self.assertEqual(storage.meta, meta)
                with transaction.atomic(), self.assertRaisesRegex(ValidationError, 'LEGACY_STORAGE_NOT_DRAINED'):
                    lock_drained_project(self.project.pk)
                storage.delete()

    def test_cancelled_dispatch_cannot_write_job_id_when_legacy_worker_is_running(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-legacy-running')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        # An old worker can change the shared status without claiming the v2
        # ledger; cancellation must not let a late enqueue reply write its ID.
        LocalFilesImportStorage.objects.filter(pk=storage.pk).update(status='in_progress', last_sync_job='legacy-job')
        cancel_queued_storage_dispatch(self.project.pk, record.pk, local_principal(self.manager), reason='cancel')
        self.assertEqual(record_storage_dispatch_job(storage, record.pk, 'stale-job'), 0)
        storage.refresh_from_db()
        self.assertEqual((storage.status, storage.last_sync_job), ('in_progress', 'legacy-job'))
        with transaction.atomic(), self.assertRaisesRegex(ValidationError, 'LEGACY_STORAGE_NOT_DRAINED'):
            lock_drained_project(self.project.pk)

    def test_dispatch_job_id_can_be_recorded_after_same_operation_is_claimed(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-claimed-job')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        claim_storage_dispatch(storage, record.pk, 'import_sync')
        LocalFilesImportStorage.objects.filter(pk=storage.pk).update(status='in_progress')
        self.assertEqual(record_storage_dispatch_job(storage, record.pk, 'current-job'), 1)
        storage.refresh_from_db()
        self.assertEqual(storage.last_sync_job, 'current-job')
        finish_storage_operation(record.pk, succeeded=True)
        self.assertEqual(record_storage_dispatch_job(storage, record.pk, 'late-job'), 0)
        storage.refresh_from_db()
        self.assertEqual(storage.last_sync_job, 'current-job')

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_sync_claim_rejects_unowned_or_nonqueued_storage_without_external_work(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-unowned-claim')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        cases = (
            ('queued', {}),
            ('queued', {QUEUE_OWNER_META_KEY: str(uuid.uuid4())}),
            ('in_progress', {QUEUE_OWNER_META_KEY: str(record.pk)}),
            ('failed', {QUEUE_OWNER_META_KEY: str(record.pk)}),
        )
        for status, meta in cases:
            with self.subTest(status=status, meta=meta):
                LocalFilesImportStorage.objects.filter(pk=storage.pk).update(status=status, meta=meta)
                with patch.object(LocalFilesImportStorage, 'scan_and_create_links') as scan:
                    with self.assertRaisesRegex(ValidationError, 'STORAGE_QUEUE_OWNERSHIP_UNVERIFIED'):
                        storage_sync_v2(LocalFilesImportStorage, storage.pk, record.pk, 'import_sync')
                    scan.assert_not_called()
                record.refresh_from_db()
                self.assertEqual(record.state, StorageOperation.State.QUEUED)
                with transaction.atomic(), self.assertRaisesRegex(ValidationError, 'STORAGE_NOT_DRAINED'):
                    lock_drained_project(self.project.pk)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_upgrade_unmarked_sync_queue_fails_closed(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(
            project=self.project, path='/tmp/issue86-old-v2', status='queued'
        )
        record = StorageOperation.objects.create(
            project=self.project,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
            operation='import_sync',
            lifecycle_revision=0,
            state=StorageOperation.State.QUEUED,
        )
        with patch.object(LocalFilesImportStorage, 'scan_and_create_links') as scan:
            with self.assertRaisesRegex(ValidationError, 'STORAGE_QUEUE_OWNERSHIP_UNVERIFIED'):
                storage_sync_v2(LocalFilesImportStorage, storage.pk, record.pk, 'import_sync')
            scan.assert_not_called()
        with patch('core.redis.start_job_async_or_sync') as enqueue:
            with self.assertRaisesRegex(ValidationError, 'STORAGE_QUEUE_OWNERSHIP_UNVERIFIED'):
                redispatch_queued_operation(record.pk, expected_attempt=0)
            enqueue.assert_not_called()
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.QUEUED)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_redispatch_preflight_is_rechecked_by_worker_after_queue_owner_changes(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-redispatch-race')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)

        def legacy_overwrite(*args, **kwargs):
            LocalFilesImportStorage.objects.filter(pk=storage.pk).update(meta={'attempts': 2})
            return MagicMock(id='stale-redispatch-job')

        with patch('core.redis.start_job_async_or_sync', side_effect=legacy_overwrite) as enqueue:
            redispatch_queued_operation(record.pk, expected_attempt=0)
            enqueue.assert_called_once()
        with patch.object(LocalFilesImportStorage, 'scan_and_create_links') as scan:
            with self.assertRaisesRegex(ValidationError, 'STORAGE_QUEUE_OWNERSHIP_UNVERIFIED'):
                storage_sync_v2(LocalFilesImportStorage, storage.pk, record.pk, 'import_sync')
            scan.assert_not_called()
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.QUEUED)
        storage.refresh_from_db()
        self.assertIsNone(storage.last_sync_job)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_redispatch_records_job_id_for_matching_queue_owner(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-redispatch-job')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        with patch('core.redis.start_job_async_or_sync', return_value=MagicMock(id='replayed-job')) as enqueue:
            redispatch_queued_operation(record.pk, expected_attempt=0)
            enqueue.assert_called_once()
            self.assertEqual(enqueue.call_args.kwargs['on_failure'].__name__, 'storage_sync_v2_failure')
        storage.refresh_from_db()
        self.assertEqual(storage.last_sync_job, 'replayed-job')
        self.assertEqual(storage.meta[QUEUE_OWNER_META_KEY], str(record.pk))

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_reserved_redispatch_fences_older_queued_worker_and_job_receipt(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-attempt')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        StorageOperation.objects.filter(pk=record.pk).update(started_at=timezone.now() - timedelta(minutes=2))
        with patch('io_storages.management.commands.replay_storage_outbox.redispatch_queued_operation') as replay:
            call_command('replay_storage_outbox', project_id=self.project.pk, limit=10, stdout=io.StringIO())
        replay.assert_called_once_with(record.pk, expected_attempt=1)
        record.refresh_from_db()
        self.assertEqual(record.dispatch_attempts, 1)
        self.assertEqual(record_storage_dispatch_job(storage, record.pk, 'old-job', dispatch_attempt=0), 0)
        with patch('core.redis.start_job_async_or_sync', return_value=MagicMock(id='current-job')) as enqueue:
            with self.assertRaisesRegex(ValidationError, 'STALE_STORAGE_DISPATCH_ATTEMPT'):
                redispatch_queued_operation(record.pk, expected_attempt=0)
            enqueue.assert_not_called()
            redispatch_queued_operation(record.pk, expected_attempt=1)
            enqueue.assert_called_once()
            self.assertEqual(enqueue.call_args.args[-1], 1)
        with patch.object(LocalFilesImportStorage, 'scan_and_create_links') as scan:
            with self.assertRaisesRegex(ValidationError, 'STALE_STORAGE_DISPATCH_ATTEMPT'):
                storage_sync_v2(LocalFilesImportStorage, storage.pk, record.pk, 'import_sync', 0)
            scan.assert_not_called()
            storage_sync_v2(LocalFilesImportStorage, storage.pk, record.pk, 'import_sync', 1)
            scan.assert_called_once()
        storage.refresh_from_db()
        record.refresh_from_db()
        self.assertEqual(storage.last_sync_job, 'current-job')
        self.assertEqual(record.state, StorageOperation.State.COMPLETED)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_slow_redispatch_cannot_record_job_for_superseded_attempt(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-slow-attempt')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        StorageOperation.objects.filter(pk=record.pk).update(dispatch_attempts=1)

        def later_reservation(*args, **kwargs):
            StorageOperation.objects.filter(pk=record.pk).update(dispatch_attempts=2)
            return MagicMock(id='late-attempt-one')

        with patch('core.redis.start_job_async_or_sync', side_effect=later_reservation):
            redispatch_queued_operation(record.pk, expected_attempt=1)
        storage.refresh_from_db()
        record.refresh_from_db()
        self.assertIsNone(storage.last_sync_job)
        self.assertEqual(record.dispatch_attempts, 2)
        with patch.object(LocalFilesImportStorage, 'scan_and_create_links') as scan:
            with self.assertRaisesRegex(ValidationError, 'STALE_STORAGE_DISPATCH_ATTEMPT'):
                storage_sync_v2(LocalFilesImportStorage, storage.pk, record.pk, 'import_sync', 1)
            scan.assert_not_called()

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_failure_callback_cannot_overwrite_a_different_attempt_or_legacy_job(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-failure-owner')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        storage.refresh_from_db()
        storage.meta['time_in_progress'] = timezone.now().isoformat()
        storage.status = 'in_progress'
        storage.last_sync_job = 'current-job'
        storage.save(update_fields=['meta', 'status', 'last_sync_job'])
        StorageOperation.objects.filter(pk=record.pk).update(
            state=StorageOperation.State.UNCERTAIN, dispatch_attempts=1
        )
        job = MagicMock(spec=Job)
        job.func_name = 'io_storages.base_models.storage_sync_v2'
        job.id = 'current-job'
        for job_args, func_name, job_id in (
            ((LocalFilesImportStorage, storage.pk, str(record.pk), 'import_sync', 0), job.func_name, 'current-job'),
            ((LocalFilesImportStorage, storage.pk, str(record.pk), 'import_sync', 0),
             'io_storages.base_models.storage_sync_v3', 'current-job'),
            ((LocalFilesImportStorage, storage.pk, str(record.pk), 'import_sync', 1), job.func_name, 'different-job'),
            ((LocalFilesImportStorage, storage.pk), 'io_storages.base_models.import_sync_background', 'current-job'),
        ):
            with self.subTest(job_args=job_args, func_name=func_name, job_id=job_id):
                job.args, job.func_name, job.id = job_args, func_name, job_id
                storage_sync_v2_failure(job)
                storage_background_failure(job)
                storage.refresh_from_db()
                self.assertEqual(storage.status, 'in_progress')
        job.args = (LocalFilesImportStorage, storage.pk, str(record.pk), 'import_sync', 1)
        job.func_name, job.id = 'io_storages.base_models.storage_sync_v2', 'current-job'
        LocalFilesImportStorage.objects.filter(pk=storage.pk).update(
            meta={
                QUEUE_OWNER_META_KEY: str(uuid.uuid4()),
                'time_in_progress': timezone.now().isoformat(),
            }
        )
        storage_sync_v2_failure(job)
        storage.refresh_from_db()
        self.assertEqual(storage.status, 'in_progress')
        storage.meta[QUEUE_OWNER_META_KEY] = str(record.pk)
        storage.save(update_fields=['meta'])
        StorageOperation.objects.filter(pk=record.pk).update(state=StorageOperation.State.COMPLETED)
        storage_sync_v2_failure(job)
        storage.refresh_from_db()
        self.assertEqual(storage.status, 'in_progress')
        StorageOperation.objects.filter(pk=record.pk).update(state=StorageOperation.State.UNCERTAIN)
        LocalFilesImportStorage.objects.filter(pk=storage.pk).update(meta={QUEUE_OWNER_META_KEY: str(record.pk)})
        storage_sync_v2_failure(job)
        storage.refresh_from_db()
        self.assertEqual(storage.status, 'in_progress')
        storage.meta['time_in_progress'] = timezone.now().isoformat()
        storage.save(update_fields=['meta'])
        job.func_name = 'io_storages.base_models.storage_sync_v3'
        storage_sync_v2_failure(job)
        storage.refresh_from_db()
        record.refresh_from_db()
        self.assertEqual(storage.status, 'failed')
        self.assertEqual(record.state, StorageOperation.State.UNCERTAIN)
        with transaction.atomic(), self.assertRaisesRegex(ValidationError, 'STORAGE_NOT_DRAINED'):
            lock_drained_project(self.project.pk)

    def test_claim_waiting_on_new_reservation_rejects_older_attempt(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-attempt-race')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        pids = []

        def claim_old_attempt():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pids.append(cursor.fetchone()[0])
                worker_storage = LocalFilesImportStorage.objects.get(pk=storage.pk)
                return claim_storage_dispatch(worker_storage, record.pk, 'import_sync', dispatch_attempt=0)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                locked = StorageOperation.objects.select_for_update().get(pk=record.pk)
                future = pool.submit(claim_old_attempt)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if pids:
                        with connection.cursor() as cursor:
                            cursor.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s', [pids[0]])
                            result = cursor.fetchone()
                        if result and result[0] == 'Lock':
                            break
                    time.sleep(0.02)
                else:
                    self.fail('worker did not wait for the dispatch reservation')
                locked.dispatch_attempts = 1
                locked.save(update_fields=['dispatch_attempts'])
            with self.assertRaisesRegex(ValidationError, 'STALE_STORAGE_DISPATCH_ATTEMPT'):
                future.result(timeout=30)
        record.refresh_from_db()
        self.assertEqual((record.state, record.dispatch_attempts), (StorageOperation.State.QUEUED, 1))

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_health_check_does_not_infer_failure_before_job_id_exists(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-job-id-pending')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        for status in ('queued', 'in_progress'):
            with self.subTest(status=status), patch('io_storages.base_models.Job.fetch') as fetch:
                LocalFilesImportStorage.objects.filter(pk=storage.pk).update(status=status, last_sync_job=None)
                storage.refresh_from_db()
                storage.job_health_check()
                fetch.assert_not_called()
                storage.refresh_from_db()
                record.refresh_from_db()
                self.assertEqual(storage.status, status)
                self.assertEqual(record.state, StorageOperation.State.QUEUED)

    @unittest.skipUnless(os.environ.get('ISSUE86_OLD_LABEL_STUDIO_DIR'), 'requires an isolated old checkout')
    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_real_old_process_queue_update_is_not_cleared_by_new_cancellation(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-old-queue')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        code = """
import os, django
django.setup()
import io_storages.base_models as legacy_base
assert os.path.realpath(legacy_base.__file__).startswith(os.path.realpath(os.environ['ISSUE86_OLD_LABEL_STUDIO_DIR']) + os.sep)
from io_storages.localfiles.models import LocalFilesImportStorage
storage = LocalFilesImportStorage.objects.get(pk=int(os.environ['TEST_STORAGE_ID']))
storage._update_queued_status()
"""
        environment = os.environ.copy()
        environment.update(
            POSTGRE_NAME=connection.settings_dict['NAME'],
            TEST_STORAGE_ID=str(storage.pk),
            LATEST_VERSION_CHECK='false',
            DJANGO_SETTINGS_MODULE='core.settings.label_studio',
            PYTHONPATH=os.pathsep.join(
                (
                    str(Path(os.environ['ISSUE86_OLD_LABEL_STUDIO_DIR']).parent),
                    os.environ['ISSUE86_OLD_LABEL_STUDIO_DIR'],
                )
            ),
        )
        completed = subprocess.run(
            [sys.executable, '-c', code],
            cwd=os.environ['ISSUE86_OLD_LABEL_STUDIO_DIR'],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors='replace')[-3000:])
        storage.refresh_from_db()
        self.assertEqual(storage.status, 'queued')
        self.assertNotIn(QUEUE_OWNER_META_KEY, storage.meta)
        with patch.object(LocalFilesImportStorage, 'scan_and_create_links') as scan:
            with self.assertRaisesRegex(ValidationError, 'STORAGE_QUEUE_OWNERSHIP_UNVERIFIED'):
                storage_sync_v2(LocalFilesImportStorage, storage.pk, record.pk, 'import_sync')
            scan.assert_not_called()
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.QUEUED)
        cancel_queued_storage_dispatch(self.project.pk, record.pk, local_principal(self.manager), reason='cancel')
        self.assertEqual(record_storage_dispatch_job(storage, record.pk, 'late-job'), 0)
        storage.refresh_from_db()
        self.assertEqual(storage.status, 'queued')
        with transaction.atomic(), self.assertRaisesRegex(ValidationError, 'LEGACY_STORAGE_NOT_DRAINED'):
            lock_drained_project(self.project.pk)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_delayed_dispatch_result_cannot_replace_new_queue_job_id(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-delayed-dispatch')
        started, release = threading.Event(), threading.Event()

        def delayed_enqueue(*args, **kwargs):
            started.set()
            if not release.wait(15):
                raise TimeoutError('test did not release dispatch')
            return MagicMock(id='old-job')

        def worker():
            close_old_connections()
            try:
                LocalFilesImportStorage.objects.get(pk=storage.pk).sync()
            finally:
                connection.close()

        with patch('io_storages.base_models.redis_connected', return_value=True), patch(
            'io_storages.base_models.start_job_async_or_sync', side_effect=delayed_enqueue
        ), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            try:
                self.assertTrue(started.wait(15))
                old = StorageOperation.objects.get(project=self.project, state=StorageOperation.State.QUEUED)
                cancel_queued_storage_dispatch(self.project.pk, old.pk, local_principal(self.manager), reason='cancel')
                storage.refresh_from_db()
                new = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
                self.assertEqual(record_storage_dispatch_job(storage, new.pk, 'new-job'), 1)
            finally:
                release.set()
            future.result(30)
        storage.refresh_from_db()
        self.assertEqual(storage.last_sync_job, 'new-job')
        self.assertEqual(storage.meta[QUEUE_OWNER_META_KEY], str(new.pk))

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_retry_cannot_overwrite_unresolved_legacy_storage_status(self):
        self.require_pg()
        client = APIClient()
        client.force_authenticate(self.manager)
        for model, path, operation in (
            (S3ImportStorage, '/api/storages/s3/', 'import_sync'),
            (S3ExportStorage, '/api/storages/export/s3/', 'export_sync'),
        ):
            storage = model.objects.create(project=self.project, bucket='issue86-test')
            for state in ('failed', 'completed_with_errors', 'unknown'):
                with self.subTest(model=model.__name__, state=state):
                    model.objects.filter(pk=storage.pk).update(status=state)
                    with self.assertRaisesRegex(ValidationError, 'LEGACY_STORAGE_NOT_DRAINED'):
                        prepare_storage_dispatch(storage, operation, mark_queued=True)
                    with patch.object(model, 'validate_connection'), patch(
                        'io_storages.base_models.start_job_async_or_sync'
                    ) as enqueue:
                        response = client.post(f'{path}{storage.pk}/sync', {}, format='json')
                    self.assertEqual(response.status_code, 400, response.content)
                    self.assertIn('LEGACY_STORAGE_NOT_DRAINED', str(response.data))
                    enqueue.assert_not_called()
                    storage.refresh_from_db()
                    self.assertEqual(storage.status, state)
                    self.assertFalse(StorageOperation.objects.filter(project=self.project).exists())

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_retry_rejects_uncertain_ledger_even_when_storage_looks_completed(self):
        self.require_pg()
        storage = S3ImportStorage.objects.create(project=self.project, bucket='issue86-test', status='completed')
        record = StorageOperation.objects.create(
            project=self.project,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
            operation='import_sync',
            lifecycle_revision=0,
            state=StorageOperation.State.UNCERTAIN,
        )
        with self.assertRaisesRegex(ValidationError, 'STORAGE_OPERATION_PENDING'):
            prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        client = APIClient()
        client.force_authenticate(self.manager)
        with patch.object(S3ImportStorage, 'validate_connection'), patch(
            'io_storages.base_models.start_job_async_or_sync'
        ) as enqueue:
            response = client.post(f'/api/storages/s3/{storage.pk}/sync', {}, format='json')
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('STORAGE_OPERATION_PENDING', str(response.data))
        enqueue.assert_not_called()
        storage.refresh_from_db()
        record.refresh_from_db()
        self.assertEqual(storage.status, 'completed')
        self.assertEqual(record.state, StorageOperation.State.UNCERTAIN)
        self.assertEqual(StorageOperation.objects.filter(project=self.project).count(), 1)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_concurrent_sync_requests_create_one_queued_admission(self):
        self.require_pg()
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-input')
        start = threading.Barrier(2)

        def request_sync():
            close_old_connections()
            try:
                instance = LocalFilesImportStorage.objects.get(pk=storage.pk)
                start.wait(timeout=10)
                instance.sync()
            finally:
                connection.close()

        with patch('io_storages.base_models.redis_connected', return_value=True), patch(
            'io_storages.base_models.start_job_async_or_sync', return_value=MagicMock(id='synthetic-job')
        ) as enqueue:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(request_sync) for _ in range(2)]
                for future in futures:
                    future.result(timeout=30)
        self.assertEqual(StorageOperation.objects.filter(project=self.project, operation='import_sync').count(), 1)
        enqueue.assert_called_once()

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_storage_config_cannot_change_or_delete_during_pending_operation(self):
        self.require_pg()
        storage = S3ImportStorage.objects.create(project=self.project, bucket='issue86-test', title='before')
        record = prepare_storage_dispatch(storage, 'import_sync')
        client = APIClient()
        client.force_authenticate(self.manager)
        url = f'/api/storages/s3/{storage.pk}'
        response = client.patch(url, {'title': 'after'}, format='json')
        self.assertEqual(response.status_code, 400, response.content)
        response = client.delete(url)
        self.assertEqual(response.status_code, 400, response.content)
        storage.refresh_from_db()
        self.assertEqual(storage.title, 'before')
        cancel_queued_storage_dispatch(self.project.pk, record.pk, local_principal(self.manager), reason='cancel')
        response = client.patch(url, {'title': 'after'}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        storage.refresh_from_db()
        self.assertEqual(storage.title, 'after')

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_legacy_storage_status_cannot_be_erased_by_config_change_or_delete(self):
        self.require_pg()
        client = APIClient()
        client.force_authenticate(self.manager)
        for model, path in ((S3ImportStorage, '/api/storages/s3/'), (S3ExportStorage, '/api/storages/export/s3/')):
            storage = model.objects.create(project=self.project, bucket='issue86-test', title='before')
            url = f'{path}{storage.pk}'
            for state in ('queued', 'in_progress', 'failed', 'completed_with_errors', 'unknown'):
                with self.subTest(model=model.__name__, state=state):
                    model.objects.filter(pk=storage.pk).update(status=state)
                    self.assertFalse(StorageOperation.objects.filter(project=self.project).exists())
                    response = client.patch(url, {'title': 'after'}, format='json')
                    self.assertEqual(response.status_code, 400, response.content)
                    self.assertIn('LEGACY_STORAGE_NOT_DRAINED', str(response.data))
                    response = client.delete(url)
                    self.assertEqual(response.status_code, 400, response.content)
                    storage.refresh_from_db()
                    self.assertEqual((storage.status, storage.title), (state, 'before'))

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_client_cannot_set_server_storage_state_on_create_or_update(self):
        self.require_pg()
        client = APIClient()
        client.force_authenticate(self.manager)
        for model, path in ((S3ImportStorage, '/api/storages/s3/'), (S3ExportStorage, '/api/storages/export/s3/')):
            storage = model.objects.create(project=self.project, bucket='issue86-test', title='before')
            for field, value in (
                ('status', 'completed'),
                ('last_sync', '2026-01-01T00:00:00Z'),
                ('last_sync_count', 1),
                ('last_sync_job', 'forged'),
                ('traceback', 'forged'),
                ('meta', {'forged': True}),
            ):
                with self.subTest(model=model.__name__, field=field):
                    response = client.patch(f'{path}{storage.pk}', {field: value}, format='json')
                    self.assertEqual(response.status_code, 400, response.content)
                    storage.refresh_from_db()
                    self.assertEqual(storage.status, storage.Status.INITIALIZED)
            with patch.object(model, 'validate_connection') as validate:
                response = client.post(
                    path.rstrip('/'),
                    {
                        'project': self.project.pk,
                        'bucket': 'issue86-test',
                        'status': 'completed',
                    },
                    format='json',
                )
                self.assertEqual(response.status_code, 400, response.content)
                validate.assert_not_called()

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True, SYNC_ON_TARGET_STORAGE_CREATION=True)
    def test_new_export_storage_persists_initial_sync_before_dispatch_and_recovers(self):
        self.require_pg()
        client = APIClient()
        client.force_authenticate(self.manager)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            LocalFilesExportStorage, 'validate_connection'
        ), patch(
            'io_storages.dispatch_recovery.redispatch_queued_operation', side_effect=OSError('queue unavailable')
        ):
            response = client.post(
                '/api/storages/export/localfiles',
                {
                    'project': self.project.pk,
                    'path': directory,
                    'title': 'automatic',
                },
                format='json',
            )
        self.assertEqual(response.status_code, 201, response.content)
        record = StorageOperation.objects.get(project=self.project, operation='export_sync')
        self.assertEqual(record.state, StorageOperation.State.QUEUED)
        storage = LocalFilesExportStorage.objects.get(pk=record.storage_id)
        self.assertEqual(storage.meta[QUEUE_OWNER_META_KEY], str(record.pk))
        with transaction.atomic(), self.assertRaises(ValidationError):
            lock_drained_project(self.project.pk)
        with patch.object(LocalFilesExportStorage, 'save_all_annotations') as write:
            storage_sync_v2(LocalFilesExportStorage, storage.pk, record.pk, 'export_sync')
            with self.assertRaises(ValidationError):
                storage_sync_v2(LocalFilesExportStorage, storage.pk, record.pk, 'export_sync')
        write.assert_called_once()
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.COMPLETED)

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True, SYNC_ON_TARGET_STORAGE_CREATION=True)
    def test_new_export_storage_and_initial_sync_roll_back_together(self):
        self.require_pg()
        client = APIClient()
        client.force_authenticate(self.manager)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            LocalFilesExportStorage, 'validate_connection'
        ), patch('io_storages.dispatch_recovery.redispatch_queued_operation') as dispatch:
            with self.assertRaisesRegex(RuntimeError, 'rollback'):
                with transaction.atomic():
                    response = client.post(
                        '/api/storages/export/localfiles',
                        {
                            'project': self.project.pk,
                            'path': directory,
                            'title': 'automatic',
                        },
                        format='json',
                    )
                    self.assertEqual(response.status_code, 201, response.content)
                    self.assertTrue(StorageOperation.objects.filter(project=self.project).exists())
                    raise RuntimeError('rollback')
            dispatch.assert_not_called()
        self.assertFalse(StorageOperation.objects.filter(project=self.project).exists())
        self.assertFalse(LocalFilesExportStorage.objects.filter(project=self.project).exists())

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
    def test_storage_cannot_move_between_projects(self):
        self.require_pg()
        other = ProjectFactory(organization=self.org)
        storage = S3ImportStorage.objects.create(project=self.project, bucket='issue86-test', title='before')
        client = APIClient()
        client.force_authenticate(self.manager)
        response = client.patch(f'/api/storages/s3/{storage.pk}', {'project': other.pk}, format='json')
        self.assertIn(response.status_code, (400, 403, 404), response.content)
        storage.refresh_from_db()
        self.assertEqual(storage.project_id, self.project.pk)

    @override_settings(
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_old_generation_cannot_restart_after_pause_and_resume(self):
        self.require_pg()
        principal = local_principal(self.manager)
        transition_project(self.project.pk, principal, expected_revision=0, target='active', reason='start')
        record = begin_storage_operation(self.project.pk, 'io_storages.test', 1, 'export', expected_revision=1)
        with self.assertRaises(ValidationError):
            transition_project(self.project.pk, principal, expected_revision=1, target='paused', reason='pause')
        finish_storage_operation(record.pk, succeeded=True)
        transition_project(self.project.pk, principal, expected_revision=1, target='paused', reason='pause')
        transition_project(self.project.pk, principal, expected_revision=2, target='active', reason='resume')
        with self.assertRaises(ValidationError):
            begin_storage_operation(self.project.pk, 'io_storages.test', 1, 'export', expected_revision=1)
        with self.assertRaises(ValidationError):
            begin_storage_operation(self.project.pk, 'io_storages.test', 1, 'export')
        current = begin_storage_operation(self.project.pk, 'io_storages.test', 1, 'export', expected_revision=3)
        self.assertEqual(current.lifecycle_revision, 3)
        finish_storage_operation(current.pk, succeeded=True)

    @override_settings(
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_queued_dispatch_blocks_pause_until_cancelled(self):
        self.require_pg()
        principal = local_principal(self.manager)
        transition_project(self.project.pk, principal, expected_revision=0, target='active', reason='start')
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-queued')
        queued = prepare_storage_dispatch(storage, 'import_sync')
        self.assertEqual(queued.lifecycle_revision, 1)
        with self.assertRaises(ValidationError):
            transition_project(self.project.pk, principal, expected_revision=1, target='paused', reason='pause')
        cancel_queued_storage_dispatch(self.project.pk, queued.pk, principal, reason='cancel before pause')
        transition_project(self.project.pk, principal, expected_revision=1, target='paused', reason='pause')
        with self.assertRaises(ValidationError):
            claim_storage_dispatch(storage, queued.pk, 'import_sync')

    @override_settings(
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_automatic_export_queue_does_not_replay_after_pause_and_resume(self):
        self.require_pg()
        principal = local_principal(self.manager)
        transition_project(self.project.pk, principal, expected_revision=0, target='active', reason='start')
        task = TaskFactory(project=self.project)
        LocalFilesExportStorage.objects.create(project=self.project, path='/tmp/issue86-output')
        with patch('io_storages.annotation_outbox.dispatch_queued_annotation_export'):
            AnnotationFactory(task=task, project=self.project)
        queued = StorageOperation.objects.get(project=self.project, operation='annotation_export_v3_queued')
        self.assertEqual(queued.lifecycle_revision, 1)
        with self.assertRaises(ValidationError):
            transition_project(self.project.pk, principal, expected_revision=1, target='paused', reason='pause')
        cancel_queued_storage_dispatch(self.project.pk, queued.pk, principal, reason='cancel before pause')
        transition_project(self.project.pk, principal, expected_revision=1, target='paused', reason='pause')
        transition_project(self.project.pk, principal, expected_revision=2, target='active', reason='resume')
        with patch('io_storages.localfiles.models.open_local_file_write') as output:
            with self.assertRaises(ValidationError):
                annotation_export_v3(queued.pk)
            output.assert_not_called()

    @override_settings(
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_transition_rejects_nonmanager_and_invalid_transition(self):
        self.require_pg()
        actor = UserFactory(active_organization=self.org)
        self.org.add_user(actor)
        ProjectMember.objects.create(project=self.project, user=actor, role='annotator')
        with self.assertRaises(PermissionDenied):
            transition_project(
                self.project.pk, local_principal(actor), expected_revision=0, target='active', reason='start'
            )
        with self.assertRaises(ValidationError):
            transition_project(
                self.project.pk, local_principal(self.manager), expected_revision=0, target='archived', reason='skip'
            )
        self.project.refresh_from_db()
        self.assertEqual((self.project.lifecycle_state, self.project.lifecycle_revision), ('legacy', 0))
        self.assertFalse(ProjectLifecycleEvent.objects.exists())

    @override_settings(
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_competing_transitions_create_one_event(self):
        self.require_pg()
        barrier = threading.Barrier(2)

        def attempt():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    transition_project(
                        self.project.pk,
                        local_principal(self.manager),
                        expected_revision=0,
                        target='active',
                        reason='concurrent start',
                    )
                    return 'accepted'
                except ValidationError:
                    return 'rejected'
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt), pool.submit(attempt)]
            self.assertCountEqual([future.result(30) for future in futures], ['accepted', 'rejected'])
        self.project.refresh_from_db()
        self.assertEqual(self.project.lifecycle_revision, 1)
        self.assertEqual(ProjectLifecycleEvent.objects.filter(project=self.project).count(), 1)

    @override_settings(
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_transition_event_failure_rolls_back_state_and_revision(self):
        self.require_pg()
        with patch(
            'projects.lifecycle.ProjectLifecycleEvent.objects.create', side_effect=RuntimeError('event failed')
        ):
            with self.assertRaisesRegex(RuntimeError, 'event failed'):
                transition_project(
                    self.project.pk,
                    local_principal(self.manager),
                    expected_revision=0,
                    target='active',
                    reason='start',
                )
        self.project.refresh_from_db()
        self.assertEqual((self.project.lifecycle_state, self.project.lifecycle_revision), ('legacy', 0))
        self.assertFalse(ProjectLifecycleEvent.objects.filter(project=self.project).exists())
