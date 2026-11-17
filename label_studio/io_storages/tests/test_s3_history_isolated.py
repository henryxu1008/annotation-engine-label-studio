"""Opt-in tests against a disposable, loopback-only S3-compatible fixture.

This is not an AWS acceptance test and must never be pointed at a real bucket.
"""
import ast
import logging
import os
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import boto3
from botocore.exceptions import ClientError
from django.test import SimpleTestCase
from io_storages.s3.deletion_plan import describe_version_deletion
from io_storages.s3.version_provenance import inspect_aws_deletion_history


@unittest.skipUnless(os.environ.get('ISSUE86_DISPOSABLE_S3_TEST') == '1', 'requires disposable S3 fixture')
class IsolatedS3HistoryTests(SimpleTestCase):
    @unittest.skipUnless(os.environ.get('ISSUE86_LEGACY_S3_SOURCE'), 'requires old-version source fixture')
    def test_old_s3_delete_method_can_hide_a_new_replacement(self):
        """Execute the old method body against a real disposable provider.

        This demonstrates a mixed-version hazard; it is not a full old worker
        deployment or a test of AWS, database isolation, or request drain.
        """
        source = Path(os.environ['ISSUE86_LEGACY_S3_SOURCE']).read_text(encoding='utf-8-sig')
        module = ast.parse(source)
        old_class = next(
            node for node in module.body if isinstance(node, ast.ClassDef) and node.name == 'S3ExportStorage'
        )
        method = next(
            node for node in old_class.body if isinstance(node, ast.FunctionDef) and node.name == 'delete_annotation'
        )
        # Execute the exact old body with fixture dependencies, without loading
        # its legacy Django application or decorators into the current process.
        method.decorator_list = []
        executable = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        key = 'synthetic/annotation'
        links = MagicMock()
        links.get_key.return_value = key
        namespace = {'logger': logging.getLogger(__name__), 'S3ExportStorageLink': links}
        exec(compile(executable, '<legacy-s3-method>', 'exec'), namespace)  # nosec B102: controlled git fixture

        session = boto3.session.Session(
            aws_access_key_id='issue86fixture', aws_secret_access_key='issue86fixturepassword', region_name='us-east-1'
        )
        client = session.client('s3', endpoint_url='http://127.0.0.1:9000')
        resource = session.resource('s3', endpoint_url='http://127.0.0.1:9000')
        bucket = 'issue86-' + uuid.uuid4().hex
        client.create_bucket(Bucket=bucket)
        client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={'Status': 'Enabled'})
        client.put_object(Bucket=bucket, Key=key, Body=b'old')
        replacement = client.put_object(Bucket=bucket, Key=key, Body=b'new')['VersionId']
        storage = SimpleNamespace(bucket=bucket, prefix='', get_client_and_resource=lambda: (client, resource))
        namespace['delete_annotation'](storage, SimpleNamespace(pk=1))

        # The replacement version still exists, but the old unversioned DELETE
        # creates a current marker and makes normal reads return 404.
        with self.assertRaises(ClientError) as error:
            client.get_object(Bucket=bucket, Key=key)
        self.assertEqual(error.exception.response['ResponseMetadata']['HTTPStatusCode'], 404)
        self.assertEqual(client.get_object(Bucket=bucket, Key=key, VersionId=replacement)['Body'].read(), b'new')
        self.assertTrue(
            any(
                item['IsLatest']
                for item in client.list_object_versions(Bucket=bucket, Prefix=key).get('DeleteMarkers', [])
            )
        )

    def test_version_visibility_and_lost_response_reconciliation(self):
        # Bind this destructive fixture to the container's loopback; never
        # accept a configured endpoint or credentials from the environment.
        client = boto3.client(
            's3',
            endpoint_url='http://127.0.0.1:9000',
            region_name='us-east-1',
            aws_access_key_id='issue86fixture',
            aws_secret_access_key='issue86fixturepassword',
        )
        bucket = 'issue86-' + uuid.uuid4().hex
        key = 'synthetic/annotation'
        client.create_bucket(Bucket=bucket)
        client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={'Status': 'Enabled'})
        storage = SimpleNamespace(bucket=bucket)

        def put(value):
            return client.put_object(Bucket=bucket, Key=key, Body=value)['VersionId']

        def inspect(version):
            record = SimpleNamespace(target_key=key, target_version_id=version)
            return inspect_aws_deletion_history(storage, record, '123456789012', client)

        old = put(b'old')
        current = put(b'current')
        self.assertEqual(inspect(current), 'current_with_other_object_versions')
        plan = describe_version_deletion(inspect(current))
        self.assertEqual(plan['visibility'], 'OLDER_OBJECT_MAY_BECOME_VISIBLE')
        self.assertEqual(plan['automatic_execution'], 'BLOCKED')
        self.assertEqual(inspect(old), 'noncurrent')
        self.assertEqual(describe_version_deletion(inspect(old))['automatic_execution'], 'BLOCKED')
        # A target-version DELETE without a marker exposes the old value.
        client.delete_object(Bucket=bucket, Key=key, VersionId=current)
        self.assertEqual(client.get_object(Bucket=bucket, Key=key)['Body'].read(), b'old')
        # Simulate losing that DELETE response: an absent target is unknown,
        # not proof that a durable operation can safely be acknowledged.
        with self.assertRaises(ValueError):
            inspect(current)
        replacement = put(b'replacement')
        self.assertEqual(inspect(old), 'noncurrent')
        self.assertEqual(describe_version_deletion(inspect(old))['automatic_execution'], 'BLOCKED')
        client.delete_object(Bucket=bucket, Key=key, VersionId=old)
        self.assertEqual(client.get_object(Bucket=bucket, Key=key)['Body'].read(), b'replacement')
        self.assertEqual(inspect(replacement), 'current_without_other_object_versions')
        self.assertEqual(describe_version_deletion(inspect(replacement))['automatic_execution'], 'BLOCKED')
