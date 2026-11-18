"""Paused policy must be rechecked at storage worker and export execution."""
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from io_storages.base_models import import_sync_background
from io_storages.localfiles.models import LocalFilesExportStorage, LocalFilesImportStorage
from organizations.tests.factories import OrganizationFactory
from projects.tests.factories import ProjectFactory
from rest_framework.exceptions import PermissionDenied
from tasks.tests.factories import AnnotationFactory, TaskFactory


@override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
class StorageStateSerializerFenceTests(SimpleTestCase):
    def test_all_builtin_storage_serializers_reject_status_override_before_network(self):
        from io_storages.azure_blob.serializers import (
            AzureBlobExportStorageSerializer,
            AzureBlobImportStorageSerializer,
        )
        from io_storages.gcs.serializers import GCSExportStorageSerializer, GCSImportStorageSerializer
        from io_storages.localfiles.serializers import (
            LocalFilesExportStorageSerializer,
            LocalFilesImportStorageSerializer,
        )
        from io_storages.redis.serializers import RedisExportStorageSerializer, RedisImportStorageSerializer
        from io_storages.s3.serializers import S3ExportStorageSerializer, S3ImportStorageSerializer

        for serializer_class in (
            AzureBlobExportStorageSerializer,
            AzureBlobImportStorageSerializer,
            GCSExportStorageSerializer,
            GCSImportStorageSerializer,
            LocalFilesExportStorageSerializer,
            LocalFilesImportStorageSerializer,
            RedisExportStorageSerializer,
            RedisImportStorageSerializer,
            S3ExportStorageSerializer,
            S3ImportStorageSerializer,
        ):
            with self.subTest(serializer=serializer_class.__name__):
                serializer = serializer_class(data={'status': 'completed'}, partial=True)
                self.assertFalse(serializer.is_valid())
                self.assertIn('status', serializer.errors)


@override_settings(ANNOTATION_PROJECT_OPERATION_PROVIDER='tasks.tests.test_project_operation_policy.PausedProvider')
class StoragePolicyFenceTests(TestCase):
    def setUp(self):
        self.org = OrganizationFactory()
        self.project = ProjectFactory(organization=self.org)
        self.annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)

    def test_queued_import_checks_policy_before_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalFilesImportStorage.objects.create(project=self.project, path=directory)
            with patch.object(LocalFilesImportStorage, 'scan_and_create_links') as scan:
                with self.assertRaises(PermissionDenied):
                    import_sync_background(LocalFilesImportStorage, storage.pk)
                scan.assert_not_called()

    def test_export_checks_policy_before_external_write(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalFilesExportStorage.objects.create(project=self.project, path=directory)
            with patch('io_storages.localfiles.models.open_local_file_write') as output:
                with self.assertRaises(PermissionDenied):
                    storage.save_annotation(self.annotation)
                output.assert_not_called()
