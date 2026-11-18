"""Opt-in local MinIO behavior test, not evidence about AWS or owner enforcement."""
import os
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import boto3
from django.test import SimpleTestCase, override_settings
from io_storages.s3.multipart_inventory import inspect_aws_multipart_page


@unittest.skipUnless(os.environ.get('ISSUE86_DISPOSABLE_S3_TEST') == '1', 'requires disposable loopback MinIO')
@override_settings(SECRET_KEY='synthetic-issue86-secret')
class MultipartLocalFixtureTests(SimpleTestCase):
    def test_multiple_uploads_survive_observation_and_invalid_continuation(self):
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
        key = 'synthetic/key'
        ids = [client.create_multipart_upload(Bucket=bucket, Key=key)['UploadId'] for _ in range(3)]
        client.create_multipart_upload(Bucket=bucket, Key=key + '-other')
        storage = SimpleNamespace(bucket=bucket, pk=1)
        record = SimpleNamespace(
            pk=uuid.uuid4(),
            operation='annotation_delete_pending',
            state='queued',
            project_id=1,
            target_key=key,
            target_storage_binding='a' * 64,
        )
        # This fixture deliberately bypasses the AWS-only endpoint and binding
        # check; MinIO cannot prove AWS owner or identity enforcement.
        with patch('io_storages.s3.multipart_inventory._require_aws_storage_identity'):
            # This MinIO build returns a truncated page without NextKeyMarker
            # for the same key. Refuse it instead of inventing a marker.
            with self.assertRaisesRegex(ValueError, 'continuation'):
                inspect_aws_multipart_page(storage, record, '123456789012', client, page_size=1)
            page = inspect_aws_multipart_page(storage, record, '123456789012', client, page_size=10)
        self.assertEqual((page['matching_uploads'], page['outcome'], page['next_cursor']), (3, 'UNKNOWN', None))
        # A legacy row without a historical binding can observe this fixture's
        # current destination only. It must not acquire historical provenance.
        record.target_storage_binding = None
        with patch('io_storages.s3.multipart_inventory._require_aws_current_observer', return_value='b' * 64):
            legacy_page = inspect_aws_multipart_page(
                storage, record, '123456789012', client, page_size=10, legacy_current_configuration=True
            )
        self.assertEqual(
            (legacy_page['matching_uploads'], legacy_page['storage_identity'], legacy_page['outcome']),
            (3, 'CURRENT_CONFIGURATION_ONLY', 'UNKNOWN'),
        )
        self.assertIsNone(record.target_storage_binding)
        remaining = client.list_multipart_uploads(Bucket=bucket, Prefix=key)['Uploads']
        self.assertTrue(set(ids).issubset({upload['UploadId'] for upload in remaining}))
