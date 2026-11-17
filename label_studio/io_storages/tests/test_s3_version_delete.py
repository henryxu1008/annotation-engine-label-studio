"""Version deletion stays dormant; isolated tests replace only the cloud gate."""
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

from access_control.project_operations import local_principal
from botocore.exceptions import ClientError
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import get_resolver
from django.utils import timezone
from io_storages.admission_models import StorageOperation, StorageOperationRecovery
from io_storages.s3 import version_delete
from io_storages.s3.models import S3ExportStorage, S3ExportStorageLink
from io_storages.s3.version_provenance import storage_binding
from organizations.tests.factories import OrganizationFactory
from projects.models import ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIClient
from tasks.models import Annotation
from tasks.tests.factories import AnnotationFactory, TaskFactory
from users.product_tours.models import UserProductTour  # noqa: F401
from users.tests.factories import UserFactory


@override_settings(
    ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
    ANNOTATION_PROJECT_OPERATION_PROVIDER='projects.tests.test_lifecycle_task_fence.TitlePolicy',
)
class VersionDeleteTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires PostgreSQL advisory and row locks')
        get_resolver().url_patterns
        org = OrganizationFactory()
        self.manager = org.created_by
        self.project = ProjectFactory(organization=org, title='test-active')
        self.storage = S3ExportStorage.objects.create(
            project=self.project, bucket='synthetic-bucket', region_name='us-east-1',
            aws_access_key_id='synthetic-key', aws_secret_access_key='synthetic-secret',
            expected_bucket_owner='123456789012',
        )
        self.record = StorageOperation.objects.create(
            project=self.project, storage_model=self.storage._meta.label_lower, storage_id=self.storage.pk,
            operation='annotation_delete_pending', lifecycle_revision=self.project.lifecycle_revision,
            subject_id=42, target_key='synthetic/key', target_version_id='version-1',
            target_object_token='a' * 32, target_storage_binding=storage_binding(self.storage), state='queued',
            dispatch_attempts=1, next_dispatch_at=timezone.now() + timedelta(minutes=5),
        )
        self.client = MagicMock()
        self.client.meta.endpoint_url = 'https://s3.us-east-1.amazonaws.com'
        self.client.get_bucket_versioning.return_value = {'Status': 'Enabled'}
        self.client.head_object.return_value = {'VersionId': 'version-1', 'Metadata': {'ls-export-token': 'a' * 32}}
        self.client.list_object_versions.return_value = {
            'IsTruncated': False,
            'Versions': [{'Key': 'synthetic/key', 'VersionId': 'version-1', 'IsLatest': True}],
        }
        self.client.delete_object.return_value = {
            'VersionId': 'version-1', 'ResponseMetadata': {'HTTPStatusCode': 204, 'RequestId': 'synthetic-request'}
        }

    def worker(self):
        close_old_connections()
        try:
            version_delete.versioned_delete_worker(self.record.pk, dispatch_attempt=1)
        finally:
            connection.close()

    def mocks(self):
        return patch.object(S3ExportStorage, 'get_client_and_resource', return_value=(self.client, MagicMock()))

    def test_cloud_gate_rejects_dispatch_and_worker_before_claim(self):
        with patch.object(version_delete, 'start_job_async_or_sync') as enqueue, self.mocks():
            with self.assertRaisesRegex(RuntimeError, 'isolation'):
                version_delete.dispatch_versioned_delete(self.record.pk)
            with self.assertRaisesRegex(RuntimeError, 'isolation'):
                version_delete.versioned_delete_worker(self.record.pk, dispatch_attempt=1)
        self.record.refresh_from_db()
        self.assertEqual(self.record.state, 'queued')
        self.assertIsNone(self.record.effect_claim_token)
        enqueue.assert_not_called()
        self.client.delete_object.assert_not_called()

    def test_replay_command_cannot_bypass_unimplemented_cloud_gate(self):
        with patch.object(version_delete, 'start_job_async_or_sync') as enqueue:
            with self.assertRaisesRegex(CommandError, 'isolation'):
                call_command('replay_s3_version_deletions', stdout=StringIO())
        self.record.refresh_from_db()
        self.assertEqual((self.record.state, self.record.dispatch_attempts), ('queued', 1))
        enqueue.assert_not_called()

    def test_replay_refuses_an_unusable_database_before_reservation(self):
        with patch.object(version_delete, '_require_verified_cloud_isolation'), patch.object(
            version_delete, '_require_autocommit', side_effect=RuntimeError('unsupported database')
        ), patch.object(version_delete, 'start_job_async_or_sync') as enqueue:
            with self.assertRaisesRegex(CommandError, 'PostgreSQL'):
                call_command('replay_s3_version_deletions', stdout=StringIO())
        self.record.refresh_from_db()
        self.assertEqual((self.record.state, self.record.dispatch_attempts), ('queued', 1))
        enqueue.assert_not_called()

    def test_replay_reserves_before_enqueue_when_test_gate_is_replaced(self):
        StorageOperation.objects.filter(pk=self.record.pk).update(dispatch_attempts=0, next_dispatch_at=None)
        with patch.object(version_delete, '_require_verified_cloud_isolation'), patch.object(
            version_delete, 'start_job_async_or_sync', return_value='synthetic-job'
        ) as enqueue:
            output = StringIO()
            call_command('replay_s3_version_deletions', project_id=self.project.pk, stdout=output)
        self.record.refresh_from_db()
        self.assertEqual((self.record.state, self.record.dispatch_attempts), ('queued', 1))
        self.assertIsNotNone(self.record.next_dispatch_at)
        self.assertIn('Attempted 1; failed 0.', output.getvalue())
        self.assertEqual(enqueue.call_args.kwargs['dispatch_attempt'], 1)

    def test_invalid_intent_is_deferred_so_bounded_replay_reaches_other_work(self):
        StorageOperation.objects.filter(pk=self.record.pk).update(dispatch_attempts=0, next_dispatch_at=None)
        invalid = StorageOperation.objects.create(
            project=self.project, storage_model=self.storage._meta.label_lower, storage_id=self.storage.pk,
            operation='annotation_delete_pending', lifecycle_revision=self.project.lifecycle_revision,
            subject_id=42, target_key='different-key', state='queued',
        )
        StorageOperation.objects.filter(pk=invalid.pk).update(started_at=self.record.started_at - timedelta(seconds=1))
        with patch.object(version_delete, '_require_verified_cloud_isolation'), patch.object(
            version_delete, 'start_job_async_or_sync', return_value='synthetic-job'
        ) as enqueue:
            with self.assertRaises(CommandError):
                call_command('replay_s3_version_deletions', project_id=self.project.pk, limit=1, stdout=StringIO(), stderr=StringIO())
            invalid.refresh_from_db()
            self.assertEqual((invalid.state, invalid.dispatch_attempts), ('queued', 0))
            self.assertGreater(invalid.next_dispatch_at, timezone.now())
            call_command('replay_s3_version_deletions', project_id=self.project.pk, limit=1, stdout=StringIO())
        self.assertEqual(enqueue.call_count, 1)
        self.record.refresh_from_db()
        self.assertEqual((self.record.state, self.record.dispatch_attempts), ('queued', 1))

    def test_unknown_other_writer_blocks_before_provider(self):
        StorageOperation.objects.create(
            project=self.project, storage_model=self.storage._meta.label_lower, storage_id=self.storage.pk,
            operation='annotation_export', lifecycle_revision=self.project.lifecycle_revision,
            state=StorageOperation.State.UNCERTAIN, target_key=None,
        )
        with patch.object(version_delete, '_require_verified_cloud_isolation'), self.mocks() as provider:
            with self.assertRaises(ValidationError):
                version_delete.versioned_delete_worker(self.record.pk, dispatch_attempt=1)
        provider.assert_not_called()
        self.record.refresh_from_db()
        self.assertEqual(self.record.state, 'queued')

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=False)
    def test_worker_refuses_disabled_admission_even_if_test_gate_is_replaced(self):
        with patch.object(version_delete, '_require_verified_cloud_isolation'), self.mocks() as provider:
            with self.assertRaises(ValidationError):
                version_delete.versioned_delete_worker(self.record.pk, dispatch_attempt=1)
        provider.assert_not_called()
        self.record.refresh_from_db()
        self.assertEqual(self.record.state, 'queued')

    def test_worker_refuses_unreserved_or_missing_dispatch_epoch(self):
        StorageOperation.objects.filter(pk=self.record.pk).update(next_dispatch_at=None)
        with patch.object(version_delete, '_require_verified_cloud_isolation'), self.mocks() as provider:
            with self.assertRaises(ValidationError):
                version_delete.versioned_delete_worker(self.record.pk, dispatch_attempt=1)
            with self.assertRaises(ValidationError):
                version_delete.versioned_delete_worker(self.record.pk)
        provider.assert_not_called()
        self.record.refresh_from_db()
        self.assertEqual(self.record.state, 'queued')

    def test_failed_enqueue_keeps_reservation_and_fences_delayed_job(self):
        StorageOperation.objects.filter(pk=self.record.pk).update(dispatch_attempts=0, next_dispatch_at=None)
        with patch.object(version_delete, '_require_verified_cloud_isolation'), self.mocks(), patch.object(
            version_delete, 'start_job_async_or_sync', side_effect=[OSError('lost enqueue response'), 'enqueued']
        ) as enqueue:
            with self.assertRaises(OSError):
                version_delete.dispatch_versioned_delete(self.record.pk)
            self.record.refresh_from_db()
            self.assertEqual((self.record.state, self.record.dispatch_attempts), ('queued', 1))
            with self.assertRaises(ValidationError):
                version_delete.dispatch_versioned_delete(self.record.pk)
            StorageOperation.objects.filter(pk=self.record.pk).update(next_dispatch_at=timezone.now() - timedelta(seconds=1))
            self.assertEqual(version_delete.dispatch_versioned_delete(self.record.pk), 'enqueued')
            with self.assertRaises(ValidationError):
                version_delete.versioned_delete_worker(self.record.pk, dispatch_attempt=1)
            version_delete.versioned_delete_worker(self.record.pk, dispatch_attempt=2)
        self.assertEqual(enqueue.call_count, 2)
        self.client.delete_object.assert_called_once()
        self.record.refresh_from_db()
        self.assertEqual(self.record.state, 'completed')

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=False)
    def test_legacy_direct_delete_is_blocked_before_provider_access(self):
        with self.mocks() as provider, self.assertRaises(PermissionDenied):
            self.storage.delete_annotation(MagicMock())
        provider.assert_not_called()
        self.client.delete_object.assert_not_called()

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=False)
    def test_legacy_signal_preserves_annotation_and_link_when_delete_is_enabled(self):
        self.storage.can_delete_objects = True
        self.storage.save(update_fields=['can_delete_objects'])
        with patch('io_storages.s3.models._dispatch_s3_annotation_export'):
            annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        link = S3ExportStorageLink.objects.create(storage=self.storage, annotation=annotation)
        with self.mocks() as provider, self.assertRaises(PermissionDenied), transaction.atomic():
            annotation.delete()
        self.assertTrue(Annotation.objects.filter(pk=annotation.pk).exists())
        self.assertTrue(S3ExportStorageLink.objects.filter(pk=link.pk).exists())
        provider.assert_not_called()

    @override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=False)
    def test_legacy_signal_allows_annotation_delete_when_external_delete_disabled(self):
        self.storage.can_delete_objects = False
        self.storage.save(update_fields=['can_delete_objects'])
        with patch('io_storages.s3.models._dispatch_s3_annotation_export'):
            annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        annotation_id = annotation.pk
        S3ExportStorageLink.objects.create(storage=self.storage, annotation=annotation)
        with self.mocks() as provider:
            annotation.delete()
        self.assertFalse(Annotation.objects.filter(pk=annotation_id).exists())
        provider.assert_not_called()

    def test_exact_version_single_attempt_and_recorded_receipt(self):
        with patch.object(version_delete, '_require_verified_cloud_isolation'), self.mocks() as provider:
            version_delete.versioned_delete_worker(self.record.pk, dispatch_attempt=1)
        provider.assert_called_once_with(single_attempt=True)
        self.client.delete_object.assert_called_once_with(
            Bucket='synthetic-bucket', Key='synthetic/key', VersionId='version-1', ExpectedBucketOwner='123456789012'
        )
        self.record.refresh_from_db()
        self.assertEqual((self.record.state, self.record.effect_phase), ('completed', version_delete.SUBMITTING))
        self.assertEqual(self.record.effect_payload['provider_request_id'], 'synthetic-request')

    def test_current_version_with_older_object_stays_blocking_before_submission(self):
        self.client.list_object_versions.return_value['Versions'].append(
            {'Key': 'synthetic/key', 'VersionId': 'older', 'IsLatest': False}
        )
        with patch.object(version_delete, '_require_verified_cloud_isolation'), self.mocks():
            with self.assertRaisesRegex(ValidationError, 'older object'):
                version_delete.versioned_delete_worker(self.record.pk, dispatch_attempt=1)
        self.record.refresh_from_db()
        self.assertEqual((self.record.state, self.record.effect_phase), ('running', version_delete.PRE_EFFECT))
        self.client.delete_object.assert_not_called()

    def test_lost_response_and_invalid_receipt_do_not_replay(self):
        for response in (
            TimeoutError('lost response'),
            {'VersionId': 'other', 'ResponseMetadata': {'HTTPStatusCode': 204, 'RequestId': 'request'}},
        ):
            with self.subTest(response=type(response).__name__):
                self.client.delete_object.reset_mock()
                self.client.delete_object.side_effect = response if isinstance(response, Exception) else None
                if not isinstance(response, Exception):
                    self.client.delete_object.return_value = response
                with patch.object(version_delete, '_require_verified_cloud_isolation'), self.mocks():
                    with self.assertRaises((TimeoutError, ValueError)):
                        version_delete.versioned_delete_worker(self.record.pk, dispatch_attempt=1)
                    with self.assertRaises(ValidationError):
                        version_delete.versioned_delete_worker(self.record.pk, dispatch_attempt=1)
                self.record.refresh_from_db()
                self.assertEqual((self.record.state, self.record.effect_phase), ('uncertain', version_delete.SUBMITTING))
                self.client.delete_object.assert_called_once()
                StorageOperation.objects.filter(pk=self.record.pk).update(
                    state='queued', effect_phase=None, effect_claim_token=None, effect_payload=None,
                    dispatch_attempts=1, next_dispatch_at=timezone.now() + timedelta(minutes=5),
                )

    def test_changed_binding_and_missing_pin_refuse_before_provider_access(self):
        for field, value in (('target_version_id', None), ('target_object_token', None), ('target_storage_binding', 'b' * 64)):
            with self.subTest(field=field):
                old = getattr(self.record, field)
                StorageOperation.objects.filter(pk=self.record.pk).update(**{field: value})
                with patch.object(version_delete, '_require_verified_cloud_isolation'), self.mocks() as provider:
                    with self.assertRaises(ValidationError):
                        version_delete.versioned_delete_worker(self.record.pk, dispatch_attempt=1)
                provider.assert_not_called()
                StorageOperation.objects.filter(pk=self.record.pk).update(**{field: old})
        self.client.delete_object.assert_not_called()

    def test_object_lock_denial_stays_uncertain_without_bypass_or_retry(self):
        self.client.delete_object.side_effect = ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'synthetic object lock'}}, 'DeleteObject'
        )
        with patch.object(version_delete, '_require_verified_cloud_isolation'), self.mocks():
            with self.assertRaises(ClientError):
                version_delete.versioned_delete_worker(self.record.pk, dispatch_attempt=1)
            with self.assertRaises(ValidationError):
                version_delete.versioned_delete_worker(self.record.pk, dispatch_attempt=1)
        self.record.refresh_from_db()
        self.assertEqual((self.record.state, self.record.effect_phase), ('uncertain', version_delete.SUBMITTING))
        self.client.delete_object.assert_called_once_with(
            Bucket='synthetic-bucket', Key='synthetic/key', VersionId='version-1', ExpectedBucketOwner='123456789012'
        )

    def test_lost_receipt_database_write_cannot_trigger_a_second_delete(self):
        with patch.object(version_delete, '_require_verified_cloud_isolation'), self.mocks(), patch.object(
            version_delete, '_finish', side_effect=OSError('synthetic receipt write failure')
        ):
            with self.assertRaises(OSError):
                version_delete.versioned_delete_worker(self.record.pk, dispatch_attempt=1)
            with self.assertRaises(ValidationError):
                version_delete.versioned_delete_worker(self.record.pk, dispatch_attempt=1)
        self.record.refresh_from_db()
        self.assertEqual((self.record.state, self.record.effect_phase), ('uncertain', version_delete.SUBMITTING))
        self.client.delete_object.assert_called_once()

    def test_worker_process_dies_after_submission_fence_and_stays_blocking(self):
        code = """
import os, django
from unittest.mock import patch
django.setup()
from io_storages.s3.version_delete import _claim, _begin_submission
with patch('io_storages.s3.version_delete._require_verified_cloud_isolation'):
    _, target, token = _claim(os.environ['ISSUE145_OPERATION'], dispatch_attempt=1)
    _begin_submission(os.environ['ISSUE145_OPERATION'], token, target)
os._exit(71)
"""
        environment = os.environ.copy()
        environment.update(
            POSTGRE_NAME=connection.settings_dict['NAME'], SECRET_KEY=settings.SECRET_KEY,
            ANNOTATION_STORAGE_ADMISSION_ENABLED='true',
            ANNOTATION_PROJECT_OPERATION_PROVIDER='projects.tests.test_lifecycle_task_fence.TitlePolicy',
            ISSUE145_OPERATION=str(self.record.pk), DJANGO_SETTINGS_MODULE='core.settings.label_studio',
            LATEST_VERSION_CHECK='false',
        )
        result = subprocess.run([sys.executable, '-c', code], env=environment, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 71, result.stderr.decode(errors='replace')[-2000:])
        self.record.refresh_from_db()
        self.assertEqual((self.record.state, self.record.effect_phase), ('running', version_delete.SUBMITTING))
        with self.assertRaises(ValidationError):
            version_delete.recover_unsubmitted_delete(
                self.project.pk, self.record.pk, local_principal(self.manager), reason='worker died'
            )
        with patch.object(version_delete, '_require_verified_cloud_isolation'), self.mocks():
            with self.assertRaises(ValidationError):
                version_delete.versioned_delete_worker(self.record.pk, dispatch_attempt=1)
        self.client.delete_object.assert_not_called()

    def test_simultaneous_workers_cannot_submit_twice(self):
        entered, release = threading.Event(), threading.Event()

        def send(**kwargs):
            entered.set()
            if not release.wait(20):
                raise TimeoutError('fixture timed out')
            return {'VersionId': 'version-1', 'ResponseMetadata': {'HTTPStatusCode': 204, 'RequestId': 'request'}}

        self.client.delete_object.side_effect = send
        with patch.object(version_delete, '_require_verified_cloud_isolation'), self.mocks(), ThreadPoolExecutor(
            max_workers=2
        ) as pool:
            first = pool.submit(self.worker)
            try:
                self.assertTrue(entered.wait(20))
                second = pool.submit(self.worker)
                with self.assertRaises(ValidationError):
                    second.result(timeout=20)
            finally:
                release.set()
            first.result(timeout=20)
        self.client.delete_object.assert_called_once()

    def test_recovery_fences_a_pre_effect_worker_and_audits(self):
        entered, release = threading.Event(), threading.Event()
        original = version_delete._begin_submission

        def pause(*args):
            entered.set()
            if not release.wait(20):
                raise TimeoutError('fixture timed out')
            return original(*args)

        with patch.object(version_delete, '_require_verified_cloud_isolation'), self.mocks(), patch.object(
            version_delete, '_begin_submission', side_effect=pause
        ), ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.worker)
            try:
                self.assertTrue(entered.wait(20))
                version_delete.recover_unsubmitted_delete(
                    self.project.pk, self.record.pk, local_principal(self.manager), reason='worker stopped'
                )
            finally:
                release.set()
            with self.assertRaises(ValidationError):
                first.result(timeout=20)
        self.record.refresh_from_db()
        self.assertEqual((self.record.state, self.record.effect_phase), ('queued', None))
        self.assertEqual(self.record.dispatch_attempts, 2)
        self.assertEqual(StorageOperationRecovery.objects.filter(operation=self.record).count(), 1)
        self.client.delete_object.assert_not_called()
        with patch.object(version_delete, '_require_verified_cloud_isolation'), self.mocks(), patch.object(
            version_delete, 'start_job_async_or_sync', return_value='enqueued'
        ):
            version_delete.dispatch_versioned_delete(self.record.pk)
            version_delete.versioned_delete_worker(self.record.pk, dispatch_attempt=3)
        self.client.delete_object.assert_called_once()

    def test_submitting_claim_cannot_be_recovered_even_after_worker_disappears(self):
        with patch.object(version_delete, '_require_verified_cloud_isolation'):
            _, target, claim = version_delete._claim(self.record.pk, dispatch_attempt=1)
            version_delete._begin_submission(self.record.pk, claim, target)
        with self.assertRaises(ValidationError):
            version_delete.recover_unsubmitted_delete(
                self.project.pk, self.record.pk, local_principal(self.manager), reason='worker died'
            )
        self.record.refresh_from_db()
        self.assertEqual((self.record.state, self.record.effect_phase), ('running', version_delete.SUBMITTING))
        self.client.delete_object.assert_not_called()

    def test_nonmanager_cannot_recover_pre_effect_claim(self):
        with patch.object(version_delete, '_require_verified_cloud_isolation'):
            version_delete._claim(self.record.pk, dispatch_attempt=1)
        with self.assertRaises(PermissionDenied):
            version_delete.recover_unsubmitted_delete(
                self.project.pk, self.record.pk, local_principal(UserFactory()), reason='worker stopped'
            )
        self.record.refresh_from_db()
        self.assertEqual((self.record.state, self.record.effect_phase), ('running', version_delete.PRE_EFFECT))
        self.assertFalse(StorageOperationRecovery.objects.filter(operation=self.record).exists())

    @override_settings(
        ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED=True,
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_public_recovery_checks_manager_project_attestation_and_claim(self):
        version_delete._claim(self.record.pk, dispatch_attempt=1)
        client = APIClient()
        client.force_authenticate(self.manager)
        url = f'/api/projects/{self.project.pk}/storage-operations/{self.record.pk}/resolve/'
        body = {'action': 'recover_s3_delete_pre_effect', 'reason': 'worker stopped', 'attested': False}
        inventory = client.get(f'/api/projects/{self.project.pk}/storage-operations/')
        self.assertEqual(inventory.status_code, 200, inventory.content)
        entry = next(item for item in inventory.data['operations'] if str(item['id']) == str(self.record.pk))
        self.assertTrue(entry['can_recover_s3_delete_pre_effect'])
        self.assertNotIn('effect_claim_token', entry)
        self.assertNotIn('effect_payload', entry)
        self.assertEqual(client.post(url, {**body, 'attested': True}, format='json').status_code, 400)
        other = ProjectFactory(organization=self.project.organization)
        other_url = f'/api/projects/{other.pk}/storage-operations/{self.record.pk}/resolve/'
        self.assertEqual(client.post(other_url, body, format='json').status_code, 400)
        annotator = UserFactory(active_organization=self.project.organization)
        self.project.organization.add_user(annotator)
        ProjectMember.objects.create(project=self.project, user=annotator, role='annotator')
        client.force_authenticate(annotator)
        self.assertIn(client.post(url, body, format='json').status_code, (403, 404))
        client.force_authenticate(self.manager)
        self.assertEqual(client.post(url, body, format='json').status_code, 204)
        self.assertEqual(client.post(url, body, format='json').status_code, 400)
        self.record.refresh_from_db()
        self.assertEqual((self.record.state, self.record.dispatch_attempts), ('queued', 2))
        self.assertEqual(StorageOperationRecovery.objects.filter(operation=self.record).count(), 1)
