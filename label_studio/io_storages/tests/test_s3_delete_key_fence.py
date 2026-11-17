"""Cross-project S3 deletion admission against real PostgreSQL row locks."""
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from unittest.mock import MagicMock, patch

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import get_resolver
from io_storages.admission_models import StorageOperation
from io_storages.annotation_outbox import queue_annotation_deletion
from io_storages.s3.models import S3ExportStorage, S3ExportStorageLink
from io_storages.s3.version_provenance import S3ExportTargetBusy
from organizations.tests.factories import OrganizationFactory
from projects.tests.factories import ProjectFactory
from rest_framework.exceptions import ValidationError
from tasks.models import Annotation
from tasks.tests.factories import AnnotationFactory, TaskFactory
from users.product_tours.models import UserProductTour  # noqa: F401


@override_settings(
    ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
    ANNOTATION_PROJECT_OPERATION_PROVIDER='projects.tests.test_lifecycle_task_fence.TitlePolicy',
)
class S3DeletionKeyFenceTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires PostgreSQL advisory locks')
        get_resolver().url_patterns
        org = OrganizationFactory()
        projects = [ProjectFactory(organization=org, title='test-active') for _ in range(2)]
        self.annotations = [
            AnnotationFactory(task=TaskFactory(project=project), project=project) for project in projects
        ]
        self.storages = [
            S3ExportStorage.objects.create(project=project, bucket='fixture-' + str(index))
            for index, project in enumerate(projects)
        ]
        self.resource = MagicMock()
        self.resource.Object.return_value.put.return_value = {'VersionId': 'synthetic-version'}

    def send(self):
        close_old_connections()
        try:
            self.storages[1].save_annotation(self.annotations[1])
        finally:
            connection.close()

    def test_deletion_intent_commit_blocks_other_project_export(self):
        prepared, release, started = threading.Event(), threading.Event(), threading.Event()

        def prepare_delete():
            close_old_connections()
            try:
                with transaction.atomic():
                    queue_annotation_deletion(self.annotations[0], self.storages[0], 'shared-key')
                    prepared.set()
                    if not release.wait(20):
                        raise TimeoutError('fixture release timed out')
            finally:
                connection.close()

        def send():
            started.set()
            return self.send()

        with patch.object(S3ExportStorageLink, 'get_key', return_value='shared-key'), patch.object(
            S3ExportStorage, 'get_client_and_resource', return_value=(MagicMock(), self.resource)
        ), patch.object(S3ExportStorage, '_get_serialized_data', return_value={'synthetic': True}), ThreadPoolExecutor(
            max_workers=2
        ) as pool:
            deletion = pool.submit(prepare_delete)
            try:
                self.assertTrue(prepared.wait(20))
                export = pool.submit(send)
                self.assertTrue(started.wait(20))
                with self.assertRaises(FutureTimeout):
                    export.result(timeout=0.5)
                self.resource.Object.return_value.put.assert_not_called()
            finally:
                release.set()
            deletion.result(timeout=20)
            with self.assertRaises(S3ExportTargetBusy):
                export.result(timeout=20)
        self.resource.Object.return_value.put.assert_not_called()
        self.assertEqual(StorageOperation.objects.get(operation='annotation_delete_pending').target_key, 'shared-key')
        attempt = StorageOperation.objects.get(operation='annotation_export')
        self.assertEqual(attempt.state, StorageOperation.State.CANCELLED)
        self.assertIsNone(attempt.target_object_token)

    def test_inflight_export_blocks_other_project_deletion_intent(self):
        entered, release = threading.Event(), threading.Event()

        def put(**kwargs):
            entered.set()
            if not release.wait(20):
                raise TimeoutError('fixture release timed out')
            return {'VersionId': 'synthetic-version'}

        self.resource.Object.return_value.put.side_effect = put
        with patch.object(S3ExportStorageLink, 'get_key', return_value='shared-key'), patch.object(
            S3ExportStorage, 'get_client_and_resource', return_value=(MagicMock(), self.resource)
        ), patch.object(S3ExportStorage, '_get_serialized_data', return_value={'synthetic': True}), ThreadPoolExecutor(
            max_workers=1
        ) as pool:
            export = pool.submit(self.send)
            try:
                self.assertTrue(entered.wait(20))
                with self.assertRaises(S3ExportTargetBusy), transaction.atomic():
                    queue_annotation_deletion(self.annotations[0], self.storages[0], 'shared-key')
                self.assertFalse(StorageOperation.objects.filter(operation='annotation_delete_pending').exists())
            finally:
                release.set()
            export.result(timeout=20)
        self.resource.Object.return_value.put.assert_called_once()

    def test_unknown_legacy_delete_key_blocks_and_known_different_key_does_not(self):
        deletion = StorageOperation.objects.create(
            project=self.annotations[0].project,
            storage_model='io_storages.s3exportstorage',
            storage_id=self.storages[0].pk,
            operation='annotation_delete_pending',
            lifecycle_revision=0,
            state=StorageOperation.State.QUEUED,
        )
        with patch.object(S3ExportStorageLink, 'get_key', return_value='shared-key'), patch.object(
            S3ExportStorage, 'get_client_and_resource', return_value=(MagicMock(), self.resource)
        ), patch.object(S3ExportStorage, '_get_serialized_data', return_value={'synthetic': True}):
            with self.assertRaises(S3ExportTargetBusy):
                self.storages[1].save_annotation(self.annotations[1])
            self.resource.Object.return_value.put.assert_not_called()
            deletion.target_key = 'different-key'
            deletion.save(update_fields=['target_key'])
            self.storages[1].save_annotation(self.annotations[1])
        self.resource.Object.return_value.put.assert_called_once()

    def test_same_transaction_can_record_two_deletion_intents_for_shared_key(self):
        second = S3ExportStorage.objects.create(project=self.annotations[0].project, bucket='another-fixture')
        with transaction.atomic():
            queue_annotation_deletion(self.annotations[0], self.storages[0], 'shared-key')
            queue_annotation_deletion(self.annotations[0], second, 'shared-key')
        self.assertEqual(
            StorageOperation.objects.filter(operation='annotation_delete_pending', target_key='shared-key').count(), 2
        )

    def test_uncertain_export_with_unknown_key_blocks_deletion(self):
        record = StorageOperation.objects.create(
            project=self.annotations[1].project,
            storage_model='io_storages.s3exportstorage',
            storage_id=self.storages[1].pk,
            operation='annotation_export',
            lifecycle_revision=0,
            state=StorageOperation.State.UNCERTAIN,
            target_key=None,
        )
        for state in (StorageOperation.State.UNCERTAIN, StorageOperation.State.QUEUED, 'unrecognized'):
            StorageOperation.objects.filter(pk=record.pk).update(state=state)
            with self.subTest(state=state), self.assertRaises(S3ExportTargetBusy), transaction.atomic():
                queue_annotation_deletion(self.annotations[0], self.storages[0], 'shared-key')
            record.refresh_from_db()
            self.assertEqual(record.state, state)
        self.assertFalse(StorageOperation.objects.filter(operation='annotation_delete_pending').exists())

    def test_delete_reads_pin_under_project_lock_before_concurrent_export(self):
        storage = self.storages[0]
        storage.can_delete_objects = True
        storage.aws_access_key_id = 'synthetic-access'
        storage.aws_secret_access_key = 'synthetic-secret'
        storage.save(update_fields=['can_delete_objects', 'aws_access_key_id', 'aws_secret_access_key'])
        annotation = self.annotations[0]
        self.resource.Object.return_value.put.side_effect = [
            {'VersionId': 'old-version'},
            {'VersionId': 'new-version'},
        ]
        paused, release, started = threading.Event(), threading.Event(), threading.Event()

        def pause_after_pin_read(*args, **kwargs):
            paused.set()
            if not release.wait(20):
                raise TimeoutError('fixture release timed out')
            return queue_annotation_deletion(*args, **kwargs)

        def delete():
            close_old_connections()
            try:
                Annotation.objects.get(pk=annotation.pk).delete()
            finally:
                connection.close()

        def export():
            close_old_connections()
            started.set()
            try:
                storage.save_annotation(Annotation.objects.get(pk=annotation.pk))
            finally:
                connection.close()

        with patch.object(
            S3ExportStorage, 'get_client_and_resource', return_value=(MagicMock(), self.resource)
        ), patch.object(S3ExportStorage, '_get_serialized_data', return_value={'synthetic': True}):
            storage.save_annotation(annotation)
            link = S3ExportStorageLink.objects.get(storage=storage, annotation=annotation)
            token = link.pinned_object_token
            with patch(
                'io_storages.annotation_outbox.queue_annotation_deletion', side_effect=pause_after_pin_read
            ), ThreadPoolExecutor(max_workers=2) as pool:
                deleting = pool.submit(delete)
                try:
                    self.assertTrue(paused.wait(20))
                    exporting = pool.submit(export)
                    self.assertTrue(started.wait(20))
                    with self.assertRaises(FutureTimeout):
                        exporting.result(timeout=0.5)
                    self.assertEqual(self.resource.Object.return_value.put.call_count, 1)
                finally:
                    release.set()
                deleting.result(timeout=20)
                with self.assertRaises(ValidationError):
                    exporting.result(timeout=20)
        record = StorageOperation.objects.get(operation='annotation_delete_pending')
        self.assertEqual((record.target_version_id, record.target_object_token), ('old-version', token))
        self.assertEqual(self.resource.Object.return_value.put.call_count, 1)

    def test_stale_annotation_task_identity_cannot_create_deletion_intent(self):
        annotation = self.annotations[0]
        new_task = TaskFactory(project=annotation.project)
        # Simulate a stale in-memory deletion object after a committed change.
        Annotation.objects.filter(pk=annotation.pk).update(task=new_task)
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            annotation.delete()
        self.assertTrue(Annotation.objects.filter(pk=annotation.pk, task=new_task).exists())
        self.assertFalse(StorageOperation.objects.filter(operation='annotation_delete_pending').exists())
