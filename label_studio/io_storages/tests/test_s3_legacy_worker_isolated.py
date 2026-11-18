"""Mixed-version process test against dedicated loopback-only fixtures.

The legacy source must be an independently extracted, trusted baseline tree.
This is not an AWS test and does not prove provider-side request drain.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import boto3
from botocore.exceptions import ClientError
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.urls import get_resolver
from io_storages.admission_models import StorageOperation
from io_storages.annotation_outbox import annotation_export_v3
from io_storages.s3.models import S3ExportStorage, S3ExportStorageLink
from io_storages.s3.version_provenance import storage_binding
from organizations.tests.factories import OrganizationFactory
from projects.tests.factories import ProjectFactory
from redis import Redis
from rest_framework.exceptions import PermissionDenied
from rq import Queue, SimpleWorker
from rq.registry import FailedJobRegistry
from tasks.tests.factories import AnnotationFactory, TaskFactory


@unittest.skipUnless(
    os.environ.get('ISSUE86_DISPOSABLE_S3_TEST') == '1'
    and os.environ.get('ISSUE86_DISPOSABLE_REDIS_TEST') == '1'
    and os.environ.get('ISSUE86_LEGACY_APP_ROOT') == '/legacy'
    and Path('/fixture/mc').is_file(),
    'requires disposable PostgreSQL, MinIO, Redis and trusted legacy source',
)
@override_settings(
    ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
    ANNOTATION_PROJECT_OPERATION_PROVIDER='projects.tests.test_lifecycle_task_fence.TitlePolicy',
)
class LegacyWorkerIsolationTests(TransactionTestCase):
    def test_legacy_rq_worker_cannot_execute_v3_then_new_worker_recovers(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires disposable PostgreSQL')
        get_resolver().url_patterns
        admin = boto3.client(
            's3',
            endpoint_url='http://127.0.0.1:9000',
            region_name='us-east-1',
            aws_access_key_id='issue86fixture',
            aws_secret_access_key='issue86fixturepassword',
        )
        bucket = 'issue86-' + uuid.uuid4().hex
        admin.create_bucket(Bucket=bucket)
        admin.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={'Status': 'Enabled'})
        project = ProjectFactory(organization=OrganizationFactory(), title='test-active')
        annotation = AnnotationFactory(task=TaskFactory(project=project), project=project)
        storage = S3ExportStorage.objects.create(
            project=project,
            bucket=bucket,
            prefix='synthetic',
            s3_endpoint='http://127.0.0.1:9000',
            region_name='us-east-1',
            aws_access_key_id='issue86fixture',
            aws_secret_access_key='issue86fixturepassword',
        )
        record = StorageOperation.objects.create(
            project=project,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
            operation='annotation_export_v3_queued',
            lifecycle_revision=0,
            subject_id=annotation.pk,
            state=StorageOperation.State.QUEUED,
        )
        redis = Redis(host='127.0.0.1', port=6379)
        try:
            queue = Queue('issue86-' + uuid.uuid4().hex, connection=redis, default_timeout=60)
            job = queue.enqueue('io_storages.annotation_outbox.annotation_export_v3', str(record.pk))
            code = """
import os, django
django.setup()
import redis
from rq import Queue, SimpleWorker
import io_storages.s3.models as old_s3
if not old_s3.__file__.startswith('/legacy/'):
    raise RuntimeError('worker did not load the legacy application')
connection = redis.Redis(host='127.0.0.1', port=6379)
SimpleWorker([Queue(os.environ['TEST_QUEUE'], connection=connection)], connection=connection).work(
    burst=True, logging_level='CRITICAL')
"""
            environment = os.environ.copy()
            environment.update(
                PYTHONPATH='/legacy:/legacy/label_studio',
                DJANGO_SETTINGS_MODULE='core.settings.label_studio',
                POSTGRE_NAME=connection.settings_dict['NAME'],
                TEST_QUEUE=queue.name,
                ANNOTATION_STORAGE_ADMISSION_ENABLED='false',
            )
            worker = subprocess.run(
                [sys.executable, '-c', code],
                cwd='/legacy/label_studio',
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=180,
            )
            if worker.returncode:
                self.fail('legacy worker fixture failed: ' + worker.stderr.decode(errors='replace')[-2500:])
            job.refresh()
            self.assertTrue(job.is_failed)
            self.assertIn('annotation_outbox', job.exc_info)
            record.refresh_from_db()
            self.assertEqual(record.state, StorageOperation.State.QUEUED)
            self.assertFalse(admin.list_objects_v2(Bucket=bucket).get('Contents'))

            # Inventory the actual old worker's failed queue entry without
            # deserializing the job or cleaning the failed registry.
            output = StringIO()
            with override_settings(RQ_QUEUES={'fixture': {'HOST': 'unused'}}), patch(
                'io_storages.management.commands.inspect_storage_upgrade_queue.django_rq.get_queue', return_value=queue
            ):
                call_command(
                    'inspect_storage_upgrade_queue',
                    connection='fixture',
                    queue=queue.name,
                    kind='failed',
                    stdout=output,
                )
            page = json.loads(output.getvalue())
            self.assertIn(job.id, page['entries'])
            self.assertEqual(page['disposition'], 'UNKNOWN')
            record.refresh_from_db()
            self.assertEqual(record.state, StorageOperation.State.QUEUED)

            # A current RQ worker claims the original durable record after the
            # incompatible job has failed; no new operation ID is invented.
            current_job = queue.enqueue(annotation_export_v3, str(record.pk))
            SimpleWorker([queue], connection=redis).work(burst=True, logging_level='CRITICAL')
            current_job.refresh()
            self.assertTrue(current_job.is_finished)
            record.refresh_from_db()
            self.assertEqual(record.state, StorageOperation.State.COMPLETED)
            key = 'synthetic/' + S3ExportStorageLink.get_key(annotation)
            response = admin.head_object(Bucket=bucket, Key=key)
            self.assertTrue(response.get('VersionId'))
            self.assertTrue(response.get('Metadata', {}).get('ls-export-token'))
            self.assertIn(job.id, FailedJobRegistry(queue=queue).get_job_ids(cleanup=False))
        finally:
            redis.close()

    def test_old_rq_worker_bypasses_new_fence_then_loses_disabled_identity(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires disposable PostgreSQL')
        get_resolver().url_patterns
        admin = boto3.client(
            's3',
            endpoint_url='http://127.0.0.1:9000',
            region_name='us-east-1',
            aws_access_key_id='issue86fixture',
            aws_secret_access_key='issue86fixturepassword',
        )
        bucket = 'issue86-' + uuid.uuid4().hex
        admin.create_bucket(Bucket=bucket)
        admin.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={'Status': 'Enabled'})
        user = 'old' + uuid.uuid4().hex[:16]
        password = 'fixture' + uuid.uuid4().hex
        org = OrganizationFactory()
        project = ProjectFactory(organization=org, title='test-active')
        annotation = AnnotationFactory(task=TaskFactory(project=project), project=project)
        storage = S3ExportStorage.objects.create(
            project=project,
            bucket=bucket,
            prefix='synthetic',
            s3_endpoint='http://127.0.0.1:9000',
            region_name='us-east-1',
            aws_access_key_id=user,
            aws_secret_access_key=password,
            expected_bucket_owner='123456789012',
        )
        key = 'synthetic/' + S3ExportStorageLink.get_key(annotation)
        token = uuid.uuid4().hex
        pinned = admin.put_object(
            Bucket=bucket, Key=key, Body=b'synthetic-new-version', Metadata={'ls-export-token': token}
        )['VersionId']
        link = S3ExportStorageLink.objects.create(
            storage=storage,
            annotation=annotation,
            object_exists=True,
            pinned_key=key,
            pinned_version_id=pinned,
            pinned_object_token=token,
            pinned_storage_binding=storage_binding(storage),
        )
        type(project).objects.filter(pk=project.pk).update(title='test-paused')
        project.refresh_from_db()
        self.assertEqual(project.title, 'test-paused')
        pending = StorageOperation.objects.create(
            project=project,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
            operation='annotation_export',
            lifecycle_revision=0,
            state=StorageOperation.State.UNCERTAIN,
            subject_id=annotation.pk,
            target_key=key,
            target_object_token='a' * 32,
        )
        with self.assertRaises(PermissionDenied):
            storage.save_annotation(annotation)

        code = """
import os, django
django.setup()
import redis
from rq import Queue, SimpleWorker
import io_storages.s3.models as old_s3
if not old_s3.__file__.startswith('/legacy/'):
    raise RuntimeError('worker did not load the legacy application')
connection = redis.Redis(host='127.0.0.1', port=6379)
queue = Queue(os.environ['TEST_QUEUE'], connection=connection)
job = queue.enqueue(old_s3.async_export_annotation_to_s3_storages, int(os.environ['TEST_ANNOTATION_ID']))
SimpleWorker([queue], connection=connection).work(burst=True, logging_level='CRITICAL')
job.refresh()
if not (job.is_finished if os.environ['TEST_EXPECT_SUCCESS'] == '1' else job.is_failed):
    raise RuntimeError('legacy job did not have the expected terminal state')
"""
        with tempfile.TemporaryDirectory() as config:

            def mc(*args):
                return subprocess.run(
                    ['/fixture/mc', '--config-dir', config, *args],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=30,
                )

            mc('alias', 'set', 'fixture', 'http://127.0.0.1:9000', 'issue86fixture', 'issue86fixturepassword')
            mc('admin', 'user', 'add', 'fixture', user, password)
            mc('admin', 'policy', 'attach', 'fixture', 'readwrite', '--user', user)
            old_client = boto3.client(
                's3',
                endpoint_url='http://127.0.0.1:9000',
                region_name='us-east-1',
                aws_access_key_id=user,
                aws_secret_access_key=password,
            )
            old_client.head_bucket(Bucket=bucket)

            def run_worker(expect_success):
                environment = os.environ.copy()
                environment.update(
                    PYTHONPATH='/legacy:/legacy/label_studio',
                    DJANGO_SETTINGS_MODULE='core.settings.label_studio',
                    POSTGRE_NAME=connection.settings_dict['NAME'],
                    TEST_ANNOTATION_ID=str(annotation.pk),
                    TEST_QUEUE='issue86-' + uuid.uuid4().hex,
                    TEST_EXPECT_SUCCESS='1' if expect_success else '0',
                    ANNOTATION_STORAGE_ADMISSION_ENABLED='false',
                )
                result = subprocess.run(
                    [sys.executable, '-c', code],
                    cwd='/legacy/label_studio',
                    env=environment,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=180,
                )
                if result.returncode:
                    self.fail('legacy worker fixture failed: ' + result.stderr.decode(errors='replace')[-2500:])

            run_worker(True)
            versions = admin.list_object_versions(Bucket=bucket, Prefix=key).get('Versions', [])
            self.assertEqual(len(versions), 2)
            self.assertNotEqual(next(version['VersionId'] for version in versions if version['IsLatest']), pinned)
            link.refresh_from_db()
            self.assertEqual((link.pinned_version_id, link.pinned_object_token), (pinned, token))
            self.assertNotIn('ls-export-token', admin.head_object(Bucket=bucket, Key=key).get('Metadata', {}))
            self.assertEqual(StorageOperation.objects.filter(project=project).count(), 1)
            pending.refresh_from_db()
            self.assertEqual(
                (pending.state, pending.target_object_token), (StorageOperation.State.UNCERTAIN, 'a' * 32)
            )
            # Only this disposable user's identity is disabled. This prevents
            # later requests but is not proof about an already accepted PUT.
            mc('admin', 'user', 'disable', 'fixture', user)
            with self.assertRaises(ClientError) as error:
                old_client.head_bucket(Bucket=bucket)
            self.assertEqual(error.exception.response['ResponseMetadata']['HTTPStatusCode'], 403)
            run_worker(False)
            self.assertEqual(len(admin.list_object_versions(Bucket=bucket, Prefix=key).get('Versions', [])), 2)
