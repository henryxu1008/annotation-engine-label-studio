"""Destructive synthetic-only checks against disposable loopback MinIO.

The AWS gate and endpoint validation are replaced only in this test process.
These tests are not AWS acceptance or proof of cloud isolation.
"""
import os
import subprocess
import sys
import unittest
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.urls import get_resolver
from django.utils import timezone
from io_storages.admission_models import StorageOperation
from io_storages.s3 import version_delete
from io_storages.s3.models import S3ExportStorage
from io_storages.s3.version_provenance import storage_binding
from organizations.tests.factories import OrganizationFactory
from projects.tests.factories import ProjectFactory
from redis import Redis
from rest_framework.exceptions import ValidationError
from rq import Queue, SimpleWorker
from users.product_tours.models import UserProductTour  # noqa: F401


@unittest.skipUnless(os.environ.get('ISSUE86_DISPOSABLE_S3_TEST') == '1', 'requires disposable loopback S3')
@override_settings(
    ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
    ANNOTATION_PROJECT_OPERATION_PROVIDER='projects.tests.test_lifecycle_task_fence.TitlePolicy',
)
class IsolatedVersionDeleteTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires PostgreSQL')
        get_resolver().url_patterns
        self.client = boto3.client(
            's3', endpoint_url='http://127.0.0.1:9000', region_name='us-east-1',
            aws_access_key_id='issue86fixture', aws_secret_access_key='issue86fixturepassword',
            config=Config(retries={'total_max_attempts': 1}),
        )
        self.bucket = 'issue145-' + uuid.uuid4().hex
        self.key = 'synthetic/key'
        self.client.create_bucket(Bucket=self.bucket)
        self.client.put_bucket_versioning(Bucket=self.bucket, VersioningConfiguration={'Status': 'Enabled'})
        project = ProjectFactory(organization=OrganizationFactory(), title='test-active')
        self.storage = S3ExportStorage.objects.create(
            project=project, bucket=self.bucket, region_name='us-east-1', s3_endpoint='http://127.0.0.1:9000',
            aws_access_key_id='issue86fixture', aws_secret_access_key='issue86fixturepassword',
            expected_bucket_owner='123456789012',
        )

    def put(self, body):
        return self.client.put_object(
            Bucket=self.bucket, Key=self.key, Body=body, Metadata={'ls-export-token': 'a' * 32}
        )['VersionId']

    def pending(self, version):
        return StorageOperation.objects.create(
            project=self.storage.project, storage_model=self.storage._meta.label_lower, storage_id=self.storage.pk,
            operation='annotation_delete_pending', lifecycle_revision=self.storage.project.lifecycle_revision,
            subject_id=42, target_key=self.key, target_version_id=version, target_object_token='a' * 32,
            target_storage_binding=storage_binding(self.storage), state='queued',
            dispatch_attempts=1, next_dispatch_at=timezone.now() + timedelta(minutes=5),
        )

    def patches(self):
        return (
            patch.object(version_delete, '_require_verified_cloud_isolation'),
            patch('io_storages.s3.version_provenance._require_aws_current_observer',
                  side_effect=lambda storage, record, owner, client: storage_binding(storage)),
            patch.object(S3ExportStorage, 'get_client_and_resource', return_value=(self.client, MagicMock())),
        )

    def test_old_target_is_deleted_without_hiding_replacement(self):
        old = self.put(b'old')
        replacement = self.put(b'replacement')
        record = self.pending(old)
        gate, endpoint, provider = self.patches()
        with gate, endpoint, provider:
            version_delete.versioned_delete_worker(record.pk, dispatch_attempt=1)
        record.refresh_from_db()
        self.assertEqual(record.state, 'completed')
        self.assertEqual(self.client.get_object(Bucket=self.bucket, Key=self.key)['Body'].read(), b'replacement')
        self.assertEqual(self.client.head_object(Bucket=self.bucket, Key=self.key)['VersionId'], replacement)
        with self.assertRaises(ClientError):
            self.client.head_object(Bucket=self.bucket, Key=self.key, VersionId=old)

    def test_duplicate_intent_does_not_turn_missing_version_into_success(self):
        old = self.put(b'old')
        replacement = self.put(b'replacement')
        first, second = self.pending(old), self.pending(old)
        gate, endpoint, provider = self.patches()
        with gate, endpoint, provider:
            version_delete.versioned_delete_worker(first.pk, dispatch_attempt=1)
            with patch.object(self.client, 'delete_object', wraps=self.client.delete_object) as delete:
                with self.assertRaises(ClientError):
                    version_delete.versioned_delete_worker(second.pk, dispatch_attempt=1)
                delete.assert_not_called()
        second.refresh_from_db()
        self.assertEqual((second.state, second.effect_phase), ('running', version_delete.PRE_EFFECT))
        self.assertEqual(self.client.head_object(Bucket=self.bucket, Key=self.key)['VersionId'], replacement)

    def test_current_target_with_older_object_never_submits(self):
        self.put(b'older')
        target = self.put(b'target')
        record = self.pending(target)
        gate, endpoint, provider = self.patches()
        with gate, endpoint, provider, patch.object(self.client, 'delete_object') as delete:
            with self.assertRaises(ValidationError):
                version_delete.versioned_delete_worker(record.pk, dispatch_attempt=1)
        delete.assert_not_called()
        record.refresh_from_db()
        self.assertEqual((record.state, record.effect_phase), ('running', version_delete.PRE_EFFECT))
        self.assertEqual(self.client.get_object(Bucket=self.bucket, Key=self.key)['Body'].read(), b'target')

    def test_noncurrent_target_does_not_remove_current_delete_marker(self):
        old = self.put(b'old')
        marker = self.client.delete_object(Bucket=self.bucket, Key=self.key)['VersionId']
        record = self.pending(old)
        gate, endpoint, provider = self.patches()
        with gate, endpoint, provider:
            version_delete.versioned_delete_worker(record.pk, dispatch_attempt=1)
        record.refresh_from_db()
        self.assertEqual(record.state, 'completed')
        with self.assertRaises(ClientError):
            self.client.get_object(Bucket=self.bucket, Key=self.key)
        versions = self.client.list_object_versions(Bucket=self.bucket, Prefix=self.key)
        self.assertTrue(any(item['VersionId'] == marker and item['IsLatest'] for item in versions['DeleteMarkers']))

    def test_durable_dispatch_runs_on_disposable_redis_without_hiding_replacement(self):
        old = self.put(b'old')
        replacement = self.put(b'replacement')
        record = self.pending(old)
        StorageOperation.objects.filter(pk=record.pk).update(dispatch_attempts=0, next_dispatch_at=None)
        broker = Redis(host='127.0.0.1', port=6379)
        queue = Queue('issue145-' + uuid.uuid4().hex, connection=broker)

        def enqueue(func, *args, queue_name, **kwargs):
            self.assertEqual(queue_name, 'low')
            return queue.enqueue_call(func, args=args, kwargs=kwargs)

        gate, endpoint, provider = self.patches()
        with gate, endpoint, provider, patch.object(version_delete, 'start_job_async_or_sync', side_effect=enqueue):
            job = version_delete.dispatch_versioned_delete(record.pk)
            record.refresh_from_db()
            self.assertEqual((record.state, record.dispatch_attempts), ('queued', 1))
            SimpleWorker([queue], connection=broker).work(burst=True)
        job.refresh()
        self.assertTrue(job.is_finished, job.exc_info)
        record.refresh_from_db()
        self.assertEqual(record.state, 'completed')
        self.assertEqual(self.client.head_object(Bucket=self.bucket, Key=self.key)['VersionId'], replacement)

    def test_lost_response_blocks_retry_and_preserves_replacement(self):
        old = self.put(b'old')
        replacement = self.put(b'replacement')
        record = self.pending(old)
        actual_delete = self.client.delete_object

        def lose_response(**kwargs):
            actual_delete(**kwargs)
            raise TimeoutError('synthetic lost response')

        gate, endpoint, provider = self.patches()
        with gate, endpoint, provider, patch.object(self.client, 'delete_object', side_effect=lose_response) as delete:
            with self.assertRaises(TimeoutError):
                version_delete.versioned_delete_worker(record.pk, dispatch_attempt=1)
            with self.assertRaises(ValidationError):
                version_delete.versioned_delete_worker(record.pk, dispatch_attempt=1)
        delete.assert_called_once()
        record.refresh_from_db()
        self.assertEqual((record.state, record.effect_phase), ('uncertain', version_delete.SUBMITTING))
        self.assertEqual(self.client.head_object(Bucket=self.bucket, Key=self.key)['VersionId'], replacement)
        self.assertEqual(self.client.get_object(Bucket=self.bucket, Key=self.key)['Body'].read(), b'replacement')

    def test_real_process_death_after_provider_delete_remains_blocking(self):
        old = self.put(b'old')
        replacement = self.put(b'replacement')
        record = self.pending(old)
        code = """
import os, django, boto3
from unittest.mock import MagicMock, patch
from botocore.config import Config
django.setup()
from io_storages.s3 import version_delete
from io_storages.s3.models import S3ExportStorage
from io_storages.s3.version_provenance import storage_binding
client = boto3.client('s3', endpoint_url='http://127.0.0.1:9000', region_name='us-east-1',
    aws_access_key_id='issue86fixture', aws_secret_access_key='issue86fixturepassword',
    config=Config(retries={'total_max_attempts': 1}))
actual = client.delete_object
def die_after_delete(**kwargs):
    actual(**kwargs)
    os._exit(73)
with patch.object(version_delete, '_require_verified_cloud_isolation'), \\
     patch('io_storages.s3.version_provenance._require_aws_current_observer',
           side_effect=lambda storage, record, owner, client: storage_binding(storage)), \\
     patch.object(S3ExportStorage, 'get_client_and_resource', return_value=(client, MagicMock())), \\
     patch.object(client, 'delete_object', side_effect=die_after_delete):
    version_delete.versioned_delete_worker(os.environ['ISSUE145_OPERATION'], dispatch_attempt=1)
"""
        environment = os.environ.copy()
        environment.update(
            POSTGRE_NAME=connection.settings_dict['NAME'], SECRET_KEY=settings.SECRET_KEY,
            ANNOTATION_STORAGE_ADMISSION_ENABLED='true',
            ANNOTATION_PROJECT_OPERATION_PROVIDER='projects.tests.test_lifecycle_task_fence.TitlePolicy',
            ISSUE145_OPERATION=str(record.pk), DJANGO_SETTINGS_MODULE='core.settings.label_studio',
            LATEST_VERSION_CHECK='false',
        )
        process = subprocess.run([sys.executable, '-c', code], env=environment, capture_output=True, timeout=60)
        self.assertEqual(process.returncode, 73, process.stderr.decode(errors='replace')[-2000:])
        record.refresh_from_db()
        self.assertEqual((record.state, record.effect_phase), ('running', version_delete.SUBMITTING))
        gate, endpoint, provider = self.patches()
        with gate, endpoint, provider, self.assertRaises(ValidationError):
            version_delete.versioned_delete_worker(record.pk, dispatch_attempt=1)
        self.assertEqual(self.client.head_object(Bucket=self.bucket, Key=self.key)['VersionId'], replacement)
        self.assertEqual(self.client.get_object(Bucket=self.bucket, Key=self.key)['Body'].read(), b'replacement')

    def test_old_worker_cannot_claim_new_delete_job_and_new_worker_can_recover(self):
        old_root = os.environ.get('ISSUE86_LEGACY_APP_ROOT')
        if not old_root:
            self.skipTest('requires pinned old application source and disposable Redis')
        old = self.put(b'old')
        replacement = self.put(b'replacement')
        record = self.pending(old)
        broker = Redis(host='127.0.0.1', port=6379)
        queue = Queue('issue145-' + uuid.uuid4().hex, connection=broker)
        job = queue.enqueue_call('io_storages.s3.version_delete.versioned_delete_worker', args=(str(record.pk),), kwargs={'dispatch_attempt': 1})
        script = """
import os, django
django.setup()
from redis import Redis
from rq import Queue, SimpleWorker
broker = Redis(host='127.0.0.1', port=6379)
queue = Queue(os.environ['ISSUE145_QUEUE'], connection=broker)
SimpleWorker([queue], connection=broker).work(burst=True)
"""
        environment = os.environ.copy()
        environment.update(
            POSTGRE_NAME=connection.settings_dict['NAME'], SECRET_KEY=settings.SECRET_KEY,
            DJANGO_SETTINGS_MODULE='core.settings.label_studio', LATEST_VERSION_CHECK='false',
            PYTHONPATH=os.pathsep.join((old_root, os.path.join(old_root, 'label_studio'))),
            ISSUE145_QUEUE=queue.name,
        )
        worker = subprocess.run(
            [sys.executable, '-c', script], cwd=os.path.join(old_root, 'label_studio'),
            env=environment, capture_output=True, text=True, timeout=90,
        )
        self.assertEqual(worker.returncode, 0, worker.stderr[-2000:])
        job.refresh()
        self.assertTrue(job.is_failed)
        record.refresh_from_db()
        self.assertEqual(record.state, 'queued')
        gate, endpoint, provider = self.patches()
        with gate, endpoint, provider:
            version_delete.versioned_delete_worker(record.pk, dispatch_attempt=1)
        record.refresh_from_db()
        self.assertEqual(record.state, 'completed')
        self.assertTrue(job.is_failed, 'old failed job remains for explicit queue disposition')
        self.assertEqual(self.client.head_object(Bucket=self.bucket, Key=self.key)['VersionId'], replacement)
