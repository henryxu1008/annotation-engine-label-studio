"""Opt-in lost-response test against dedicated PostgreSQL and MinIO fixtures."""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import boto3
from botocore.exceptions import ConnectionClosedError
from django.core.management import call_command
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import get_resolver
from io_storages.admission import lock_drained_project
from io_storages.admission_models import StorageOperation
from io_storages.annotation_outbox import queue_annotation_deletion
from io_storages.s3.models import S3ExportStorage, S3ExportStorageLink
from io_storages.s3.utils import S3StorageError
from io_storages.s3.version_provenance import (
    S3ExportTargetBusy,
    inspect_aws_export_request,
    inspect_aws_export_request_page,
    storage_binding,
)
from organizations.tests.factories import OrganizationFactory
from projects.tests.factories import ProjectFactory
from rest_framework.exceptions import ValidationError
from tasks.tests.factories import AnnotationFactory, TaskFactory


@unittest.skipUnless(os.environ.get('ISSUE86_DISPOSABLE_S3_TEST') == '1', 'requires disposable S3 fixture')
@override_settings(
    SECRET_KEY='synthetic-issue86-secret',
    ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
    ANNOTATION_PROJECT_OPERATION_PROVIDER='projects.tests.test_lifecycle_task_fence.TitlePolicy',
)
class IsolatedS3UnknownExportTests(TransactionTestCase):
    def test_real_sdk_lost_response_does_not_repeat_committed_put(self):
        get_resolver().url_patterns
        client = boto3.client(
            's3',
            endpoint_url='http://127.0.0.1:9000',
            region_name='us-east-1',
            aws_access_key_id='issue86fixture',
            aws_secret_access_key='issue86fixturepassword',
        )
        bucket = 'issue86-' + uuid.uuid4().hex
        client.create_bucket(Bucket=bucket)
        client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={'Status': 'Enabled'})
        project = ProjectFactory(organization=OrganizationFactory(), title='test-active')
        annotation = AnnotationFactory(task=TaskFactory(project=project), project=project)
        received = []

        class LostResponseProxy(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_PUT(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                token = self.headers['x-amz-meta-ls-export-token']
                key = 'synthetic/' + S3ExportStorageLink.get_key(annotation)
                result = client.put_object(Bucket=bucket, Key=key, Body=body, Metadata={'ls-export-token': token})
                received.append(result['VersionId'])
                # The provider has committed, but the caller never gets a response.
                self.close_connection = True

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), LostResponseProxy)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            storage = S3ExportStorage.objects.create(
                project=project,
                bucket=bucket,
                prefix='synthetic',
                s3_endpoint=f'http://127.0.0.1:{server.server_port}',
                region_name='us-east-1',
                aws_access_key_id='issue86fixture',
                aws_secret_access_key='issue86fixturepassword',
            )
            with patch.dict(
                os.environ, {'AWS_MAX_ATTEMPTS': '7', 'AWS_RETRY_MODE': 'standard', 'NO_PROXY': '127.0.0.1,localhost'}
            ), patch.object(S3ExportStorage, '_get_serialized_data', return_value={'synthetic': True}):
                with self.assertRaises((ConnectionClosedError, S3StorageError)):
                    storage.save_annotation(annotation)
            record = StorageOperation.objects.get(project=project, operation='annotation_export')
            self.assertEqual(record.state, StorageOperation.State.UNCERTAIN)
            self.assertEqual(len(received), 1)
            versions = client.list_object_versions(Bucket=bucket, Prefix=record.target_key)['Versions']
            self.assertEqual([version['VersionId'] for version in versions], received)
            observed = client.head_object(Bucket=bucket, Key=record.target_key, VersionId=received[0])
            self.assertEqual(observed['Metadata']['ls-export-token'], record.target_object_token)
            with self.assertRaises(S3ExportTargetBusy):
                storage.save_annotation(annotation)
            self.assertEqual(len(received), 1)
            self.assertEqual(
                StorageOperation.objects.filter(
                    project=project, operation='annotation_export', state=StorageOperation.State.CANCELLED
                ).count(),
                1,
            )
            # A separate project/storage cannot infer that a lost response
            # means the provider did not commit, even for the same key.
            other = ProjectFactory(organization=project.organization, title='test-active')
            other_annotation = AnnotationFactory(task=TaskFactory(project=other), project=other)
            other_storage = S3ExportStorage.objects.create(project=other, bucket=bucket, prefix='synthetic')
            with self.assertRaises(S3ExportTargetBusy), transaction.atomic():
                queue_annotation_deletion(other_annotation, other_storage, record.target_key)
            self.assertFalse(
                StorageOperation.objects.filter(project=other, operation='annotation_delete_pending').exists()
            )
            other_key = record.target_key[len('synthetic/') :]
            with patch.object(S3ExportStorageLink, 'get_key', return_value=other_key), patch.object(
                S3ExportStorage, 'get_client_and_resource'
            ) as other_provider:
                with self.assertRaises(S3ExportTargetBusy):
                    other_storage.save_annotation(other_annotation)
                other_provider.assert_not_called()
            record.refresh_from_db()
            self.assertEqual(record.state, StorageOperation.State.UNCERTAIN)
            self.assertEqual(len(received), 1)
            with transaction.atomic(), self.assertRaisesRegex(ValidationError, 'STORAGE_NOT_DRAINED'):
                lock_drained_project(project.pk)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)

    def test_real_version_history_can_be_inspected_in_bounded_pages(self):
        get_resolver().url_patterns
        client = boto3.client(
            's3',
            endpoint_url='http://127.0.0.1:9000',
            region_name='us-east-1',
            aws_access_key_id='issue86fixture',
            aws_secret_access_key='issue86fixturepassword',
        )
        bucket = 'issue86-' + uuid.uuid4().hex
        client.create_bucket(Bucket=bucket)
        client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={'Status': 'Enabled'})
        org = OrganizationFactory()
        project = ProjectFactory(organization=org, title='test-active')
        storage = S3ExportStorage.objects.create(
            project=project,
            bucket=bucket,
            prefix='synthetic',
            expected_bucket_owner='123456789012',
            aws_access_key_id='issue86fixture',
            aws_secret_access_key='issue86fixturepassword',
        )
        token, key = uuid.uuid4().hex, 'synthetic/annotation'
        record = StorageOperation.objects.create(
            project=project,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
            operation='annotation_export',
            lifecycle_revision=0,
            subject_id=1,
            state=StorageOperation.State.UNCERTAIN,
            target_key=key,
            target_object_token=token,
            target_storage_binding=storage_binding(storage),
        )
        expected = set()
        for i in range(123):
            result = client.put_object(
                Bucket=bucket,
                Key=key,
                Body=b'synthetic',
                Metadata={'ls-export-token': token if i in (4, 118) else uuid.uuid4().hex},
            )
            if i in (4, 118):
                expected.add(result['VersionId'])
        facade = SimpleNamespace(
            meta=SimpleNamespace(endpoint_url='https://s3.us-east-1.amazonaws.com'),
            head_bucket=client.head_bucket,
            get_bucket_versioning=client.get_bucket_versioning,
            list_object_versions=client.list_object_versions,
            head_object=client.head_object,
        )
        seen, cursor, pages, inspected = set(), None, 0, 0
        while True:
            page = inspect_aws_export_request_page(
                storage, record, '123456789012', facade, cursor=cursor, page_size=10
            )
            pages += 1
            inspected += page['inspected_versions']
            self.assertEqual(page['outcome'], 'UNKNOWN')
            self.assertLessEqual(page['inspected_versions'], 10)
            seen.update(page['matching_version_ids'])
            cursor = page['next_cursor']
            if cursor is None:
                break
            self.assertLess(pages, 20)
        self.assertEqual((pages, inspected, seen), (13, 123, expected))
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.UNCERTAIN)

    def test_committed_put_with_lost_response_is_observable_but_not_resolved(self):
        get_resolver().url_patterns
        client = boto3.client(
            's3',
            endpoint_url='http://127.0.0.1:9000',
            region_name='us-east-1',
            aws_access_key_id='issue86fixture',
            aws_secret_access_key='issue86fixturepassword',
        )
        bucket = 'issue86-' + uuid.uuid4().hex
        client.create_bucket(Bucket=bucket)
        client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={'Status': 'Enabled'})
        org = OrganizationFactory()
        project = ProjectFactory(organization=org, title='test-active')
        annotation = AnnotationFactory(task=TaskFactory(project=project), project=project)
        storage = S3ExportStorage.objects.create(
            project=project,
            bucket=bucket,
            prefix='synthetic',
            expected_bucket_owner='123456789012',
            aws_access_key_id='issue86fixture',
            aws_secret_access_key='issue86fixturepassword',
        )

        def committed_then_lost(**kwargs):
            client.put_object(Bucket=bucket, Key=expected_key, **kwargs)
            raise TimeoutError('synthetic lost provider response')

        expected_key = 'synthetic/' + S3ExportStorageLink.get_key(annotation)
        resource = MagicMock()
        resource.Object.return_value.put.side_effect = committed_then_lost
        with patch.object(S3ExportStorage, 'get_client_and_resource', return_value=(client, resource)), patch.object(
            S3ExportStorage, '_get_serialized_data', return_value={'synthetic': True}
        ):
            with self.assertRaises(TimeoutError):
                storage.save_annotation(annotation)
        self.assertEqual(resource.Object.return_value.put.call_args.kwargs['ExpectedBucketOwner'], '123456789012')
        record = StorageOperation.objects.get(project=project, operation='annotation_export')
        self.assertEqual((record.state, record.target_key), (StorageOperation.State.UNCERTAIN, expected_key))
        self.assertTrue(record.target_object_token)
        self.assertFalse(S3ExportStorageLink.objects.filter(storage=storage, annotation=annotation).exists())

        # The facade adapts the real local client to the strict AWS endpoint
        # preflight solely for this fixture. MinIO is not AWS acceptance.
        facade = SimpleNamespace(
            meta=SimpleNamespace(endpoint_url='https://s3.us-east-1.amazonaws.com'),
            head_bucket=client.head_bucket,
            get_bucket_versioning=client.get_bucket_versioning,
            list_object_versions=client.list_object_versions,
            head_object=client.head_object,
        )
        result = inspect_aws_export_request(storage, record, '123456789012', facade)
        self.assertEqual(result, {'inspected_versions': 1, 'matching_versions': 1, 'outcome': 'UNKNOWN'})
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.UNCERTAIN)
        with transaction.atomic(), self.assertRaisesRegex(ValidationError, 'STORAGE_NOT_DRAINED'):
            lock_drained_project(project.pk)

    def test_process_death_after_put_retains_original_request_identity(self):
        get_resolver().url_patterns
        client = boto3.client(
            's3',
            endpoint_url='http://127.0.0.1:9000',
            region_name='us-east-1',
            aws_access_key_id='issue86fixture',
            aws_secret_access_key='issue86fixturepassword',
        )
        bucket = 'issue86-' + uuid.uuid4().hex
        client.create_bucket(Bucket=bucket)
        client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={'Status': 'Enabled'})
        org = OrganizationFactory()
        project = ProjectFactory(organization=org, title='test-active')
        annotation = AnnotationFactory(task=TaskFactory(project=project), project=project)
        storage = S3ExportStorage.objects.create(
            project=project,
            bucket=bucket,
            prefix='synthetic',
            expected_bucket_owner='123456789012',
            aws_access_key_id='issue86fixture',
            aws_secret_access_key='issue86fixturepassword',
        )
        code = """
import os, time, django
from unittest.mock import MagicMock, patch
import boto3
django.setup()
from io_storages.s3.models import S3ExportStorage, S3ExportStorageLink
from tasks.models import Annotation
storage = S3ExportStorage.objects.get(pk=int(os.environ['TEST_STORAGE_ID']))
annotation = Annotation.objects.get(pk=int(os.environ['TEST_ANNOTATION_ID']))
key = str(storage.prefix) + '/' + S3ExportStorageLink.get_key(annotation)
client = boto3.client('s3', endpoint_url='http://127.0.0.1:9000', region_name='us-east-1',
                      aws_access_key_id='issue86fixture', aws_secret_access_key='issue86fixturepassword')
def committed_then_hang(**kwargs):
    client.put_object(Bucket=storage.bucket, Key=key, **kwargs)
    open(os.environ['TEST_MARKER_PATH'], 'w').close()
    time.sleep(300)
resource = MagicMock()
resource.Object.return_value.put.side_effect = committed_then_hang
with patch.object(S3ExportStorage, 'get_client_and_resource', return_value=(client, resource)), \\
     patch.object(S3ExportStorage, '_get_serialized_data', return_value={'synthetic': True}):
    storage.save_annotation(annotation)
"""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'committed'
            environment = os.environ.copy()
            environment.update(
                POSTGRE_NAME=connection.settings_dict['NAME'],
                TEST_STORAGE_ID=str(storage.pk),
                TEST_ANNOTATION_ID=str(annotation.pk),
                TEST_MARKER_PATH=str(marker),
                DJANGO_SETTINGS_MODULE='core.settings.label_studio',
                LATEST_VERSION_CHECK='false',
                SECRET_KEY='synthetic-issue86-secret',
                ANNOTATION_STORAGE_ADMISSION_ENABLED='true',
                ANNOTATION_PROJECT_OPERATION_PROVIDER='projects.tests.test_lifecycle_task_fence.TitlePolicy',
            )
            process = subprocess.Popen(
                [sys.executable, '-c', code], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            try:
                deadline = time.monotonic() + 60
                while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(0.05)
                if not marker.exists():
                    if process.poll() is None:
                        process.kill()
                    _, error = process.communicate(timeout=10)
                    self.fail(f'export worker did not reach provider commit: {error.decode(errors="replace")[-3000:]}')
                record = StorageOperation.objects.get(project=project, operation='annotation_export')
                self.assertEqual(record.state, StorageOperation.State.RUNNING)
                self.assertTrue(record.target_object_token)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
            record.refresh_from_db()
            self.assertEqual(record.state, StorageOperation.State.RUNNING)
            self.assertFalse(S3ExportStorageLink.objects.filter(storage=storage, annotation=annotation).exists())
            facade = SimpleNamespace(
                meta=SimpleNamespace(endpoint_url='https://s3.us-east-1.amazonaws.com'),
                head_bucket=client.head_bucket,
                get_bucket_versioning=client.get_bucket_versioning,
                list_object_versions=client.list_object_versions,
                head_object=client.head_object,
            )
            result = inspect_aws_export_request(storage, record, '123456789012', facade)
            self.assertEqual(result, {'inspected_versions': 1, 'matching_versions': 1, 'outcome': 'UNKNOWN'})
            # A periodic dispatcher after process death may not replay a
            # running request whose external outcome has not been reconciled.
            with patch('io_storages.management.commands.replay_storage_outbox.redispatch_queued_operation') as replay:
                call_command('replay_storage_outbox', project_id=project.pk, limit=10, stdout=StringIO())
                replay.assert_not_called()
            record.refresh_from_db()
            self.assertEqual(record.state, StorageOperation.State.RUNNING)
            with transaction.atomic(), self.assertRaisesRegex(ValidationError, 'STORAGE_NOT_DRAINED'):
                lock_drained_project(project.pk)
