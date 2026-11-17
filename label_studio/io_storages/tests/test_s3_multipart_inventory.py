"""Multipart observations must not change an operation or authorize deletion."""
import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

import boto3
from botocore.stub import Stubber
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from io_storages.s3.multipart_inventory import inspect_aws_multipart_page
from io_storages.s3.version_provenance import storage_binding


@override_settings(SECRET_KEY='synthetic-issue86-secret')
class MultipartInventoryTests(SimpleTestCase):
    def setUp(self):
        self.storage = SimpleNamespace(
            project_id=4,
            pk=9,
            bucket='fixture',
            prefix='a',
            s3_endpoint=None,
            region_name='us-east-1',
            aws_access_key_id='synthetic',
            aws_secret_access_key='synthetic-secret',
            aws_session_token=None,
            expected_bucket_owner='123456789012',
            _meta=SimpleNamespace(label_lower='io_storages.s3exportstorage'),
        )
        self.record = SimpleNamespace(
            pk=UUID('00000000-0000-0000-0000-000000000001'),
            operation='annotation_delete_pending',
            state='queued',
            project_id=4,
            storage_id=9,
            storage_model='io_storages.s3exportstorage',
            target_key='a/key',
            target_storage_binding=storage_binding(self.storage),
        )
        self.client = MagicMock()
        self.client.meta.endpoint_url = 'https://s3.us-east-1.amazonaws.com'
        self.client.get_bucket_versioning.return_value = {'Status': 'Enabled'}

    def page(self, **kwargs):
        return inspect_aws_multipart_page(self.storage, self.record, '123456789012', self.client, **kwargs)

    def test_paginated_exact_key_count_and_encrypted_cursor(self):
        self.client.list_multipart_uploads.side_effect = [
            {
                'IsTruncated': True,
                'NextKeyMarker': 'a/key',
                'NextUploadIdMarker': 'secret-upload-1',
                'Uploads': [
                    {'Key': 'a/key', 'UploadId': 'secret-upload-1'},
                    {'Key': 'a/key-extra', 'UploadId': 'secret-upload-2'},
                ],
            },
            {'IsTruncated': False, 'Uploads': [{'Key': 'a/key', 'UploadId': 'secret-upload-3'}]},
        ]
        first = self.page(page_size=2)
        self.assertEqual((first['matching_uploads'], first['inspected_uploads'], first['outcome']), (1, 2, 'UNKNOWN'))
        self.assertNotIn('secret-upload', json.dumps(first))
        second = self.page(page_size=2, cursor=first['next_cursor'])
        self.assertEqual((second['page'], second['matching_uploads'], second['next_cursor']), (2, 1, None))
        self.client.list_multipart_uploads.assert_called_with(
            Bucket='fixture',
            Prefix='a/key',
            MaxUploads=2,
            ExpectedBucketOwner='123456789012',
            KeyMarker='a/key',
            UploadIdMarker='secret-upload-1',
        )
        self.client.abort_multipart_upload.assert_not_called()
        self.client.delete_object.assert_not_called()

    def test_boto3_models_accept_owner_and_continuation_parameters(self):
        client = boto3.client(
            's3', region_name='us-east-1', aws_access_key_id='synthetic', aws_secret_access_key='synthetic-secret'
        )
        head = {'Bucket': 'fixture', 'ExpectedBucketOwner': '123456789012'}
        with Stubber(client) as stub:
            for continuation in (False, True):
                stub.add_response('head_bucket', {}, head)
                stub.add_response('get_bucket_versioning', {'Status': 'Enabled'}, head)
                params = {
                    'Bucket': 'fixture',
                    'Prefix': 'a/key',
                    'MaxUploads': 1,
                    'ExpectedBucketOwner': '123456789012',
                }
                response = {
                    'IsTruncated': not continuation,
                    'Uploads': [{'Key': 'a/key', 'UploadId': 'u2' if continuation else 'u1'}],
                }
                if continuation:
                    params.update(KeyMarker='a/key', UploadIdMarker='u1')
                else:
                    response.update(NextKeyMarker='a/key', NextUploadIdMarker='u1')
                stub.add_response('list_multipart_uploads', response, params)
            first = inspect_aws_multipart_page(self.storage, self.record, '123456789012', client, page_size=1)
            second = inspect_aws_multipart_page(
                self.storage, self.record, '123456789012', client, page_size=1, cursor=first['next_cursor']
            )
            self.assertEqual(
                (first['matching_uploads'], second['matching_uploads'], second['outcome']), (1, 1, 'UNKNOWN')
            )
            stub.assert_no_pending_responses()

    def test_cursor_is_bound_to_owner_key_record_and_page_size(self):
        self.client.list_multipart_uploads.return_value = {
            'IsTruncated': True,
            'NextKeyMarker': 'a/key',
            'NextUploadIdMarker': 'upload',
            'Uploads': [],
        }
        cursor = self.page(page_size=2)['next_cursor']
        for change in ('key', 'record', 'size'):
            with self.subTest(change=change):
                original_key, original_pk = self.record.target_key, self.record.pk
                if change == 'key':
                    self.record.target_key = 'a/other'
                if change == 'record':
                    self.record.pk = UUID('00000000-0000-0000-0000-000000000002')
                with self.assertRaises(ValueError):
                    self.page(page_size=3 if change == 'size' else 2, cursor=cursor)
                self.record.target_key, self.record.pk = original_key, original_pk

    def test_invalid_provider_pages_and_unbound_records_fail_closed(self):
        bad_pages = [
            {},
            {'IsTruncated': 'false'},
            {'IsTruncated': False, 'Uploads': [{}]},
            {'IsTruncated': False, 'Uploads': [{'Key': 'other', 'UploadId': 'id'}]},
            {'IsTruncated': True, 'Uploads': [], 'NextKeyMarker': 'a/key'},
            {'IsTruncated': False, 'Uploads': [{'Key': 'a/key', 'UploadId': 'id'}] * 2},
        ]
        for page in bad_pages:
            with self.subTest(page=page), self.assertRaises(ValueError):
                self.client.list_multipart_uploads.return_value = page
                self.page()
        self.record.state = 'completed'
        with self.assertRaises(ValueError):
            self.page()
        self.record.state = 'queued'
        self.record.target_storage_binding = None
        with self.assertRaises(ValueError):
            self.page()

    def test_nonstandard_aws_endpoint_components_fail_before_provider_calls(self):
        for endpoint in (
            'https://s3.us-east-1.amazonaws.com/custom',
            'https://s3.us-east-1.amazonaws.com/?other=1',
            'https://s3.us-east-1.amazonaws.com/#fragment',
        ):
            with self.subTest(endpoint=endpoint):
                self.client.meta.endpoint_url = endpoint
                with self.assertRaisesRegex(ValueError, 'standard AWS'):
                    self.page()
        self.client.head_bucket.assert_not_called()
        self.client.list_multipart_uploads.assert_not_called()

    def test_only_blocking_export_or_queued_deletion_can_be_inspected(self):
        self.client.list_multipart_uploads.return_value = {'IsTruncated': False}
        for operation, state in (
            ('annotation_export', 'running'),
            ('annotation_export', 'uncertain'),
            ('annotation_delete_pending', 'queued'),
        ):
            self.record.operation, self.record.state = operation, state
            with self.subTest(operation=operation, state=state):
                self.assertEqual(self.page()['outcome'], 'UNKNOWN')
        for operation, state in (
            ('annotation_export', 'completed'),
            ('annotation_export', 'queued'),
            ('annotation_delete_pending', 'completed'),
            ('other', 'running'),
        ):
            self.record.operation, self.record.state = operation, state
            with self.subTest(operation=operation, state=state), self.assertRaises(ValueError):
                self.page()

    def test_owner_and_permission_failures_do_not_list_uploads(self):
        with self.assertRaises(ValueError):
            inspect_aws_multipart_page(self.storage, self.record, '999999999999', self.client)
        self.client.head_bucket.side_effect = PermissionError('synthetic denied')
        with self.assertRaises(PermissionError):
            self.page()
        self.client.list_multipart_uploads.assert_not_called()

    def test_unbound_legacy_mode_reports_current_configuration_only(self):
        self.record.target_storage_binding = None
        self.storage.expected_bucket_owner = None
        self.client.list_multipart_uploads.return_value = {
            'IsTruncated': False,
            'Uploads': [
                {'Key': 'a/key', 'UploadId': 'synthetic'},
            ],
        }
        with self.assertRaises(ValueError):
            self.page()
        result = self.page(legacy_current_configuration=True)
        self.assertEqual(
            (result['matching_uploads'], result['outcome'], result['storage_identity']),
            (1, 'UNKNOWN', 'CURRENT_CONFIGURATION_ONLY'),
        )
        self.assertEqual(result['bucket_versioning'], 'Enabled')
        self.client.list_multipart_uploads.assert_called_once_with(
            Bucket='fixture',
            Prefix='a/key',
            MaxUploads=25,
            ExpectedBucketOwner='123456789012',
        )
        self.client.abort_multipart_upload.assert_not_called()
        self.client.delete_object.assert_not_called()
        self.assertIsNone(self.record.target_storage_binding)

    def test_legacy_observation_handles_unversioned_and_suspended_without_authorizing_delete(self):
        self.record.target_storage_binding = None
        self.storage.expected_bucket_owner = None
        self.client.list_multipart_uploads.return_value = {'IsTruncated': False, 'Uploads': []}
        for response, expected in (({}, 'UNVERSIONED'), ({'Status': 'Suspended'}, 'Suspended')):
            self.client.get_bucket_versioning.return_value = response
            with self.subTest(response=response):
                result = self.page(legacy_current_configuration=True)
                self.assertEqual((result['bucket_versioning'], result['outcome']), (expected, 'UNKNOWN'))
        for response in ({'Status': None}, {'Status': 'UNVERSIONED'}, {'Status': 'unexpected'}, None):
            self.client.get_bucket_versioning.return_value = response
            with self.subTest(response=response), self.assertRaises(ValueError):
                self.page(legacy_current_configuration=True)
        self.client.delete_object.assert_not_called()
        self.client.abort_multipart_upload.assert_not_called()

    def test_legacy_cursor_is_bound_to_current_configuration_and_mode(self):
        self.record.target_storage_binding = None
        self.storage.expected_bucket_owner = None
        self.client.list_multipart_uploads.return_value = {
            'IsTruncated': True,
            'NextKeyMarker': 'a/key',
            'NextUploadIdMarker': 'synthetic',
            'Uploads': [],
        }
        cursor = self.page(page_size=1, legacy_current_configuration=True)['next_cursor']
        self.storage.aws_access_key_id = 'different-current-identity'
        with self.assertRaises(ValueError):
            self.page(page_size=1, cursor=cursor, legacy_current_configuration=True)
        self.storage.aws_access_key_id = 'synthetic'
        self.record.target_storage_binding = storage_binding(self.storage)
        self.storage.expected_bucket_owner = '123456789012'
        self.record.target_storage_binding = storage_binding(self.storage)
        with self.assertRaises(ValueError):
            self.page(page_size=1, cursor=cursor)
        with self.assertRaises(ValueError):
            self.page(page_size=1, cursor=cursor, legacy_current_configuration=True)

    def test_legacy_mode_rejects_wrong_current_owner_or_project(self):
        self.record.target_storage_binding = None
        self.storage.expected_bucket_owner = '999999999999'
        with self.assertRaises(ValueError):
            self.page(legacy_current_configuration=True)
        self.storage.expected_bucket_owner = None
        self.record.project_id = 5
        with self.assertRaises(ValueError):
            self.page(legacy_current_configuration=True)
        self.client.head_bucket.assert_not_called()

    def test_command_redacts_provider_error_and_never_resolves_ledger(self):
        storage = MagicMock()
        storage.s3_endpoint = None
        storage.get_client.return_value = self.client
        output = StringIO()
        with patch(
            'io_storages.management.commands.check_s3_multipart_uploads.StorageOperation.objects.filter'
        ) as records, patch(
            'io_storages.management.commands.check_s3_multipart_uploads.S3ExportStorage.objects.filter'
        ) as stores, patch(
            'io_storages.management.commands.check_s3_multipart_uploads.inspect_aws_multipart_page'
        ) as inspect:
            records.return_value.first.return_value = self.record
            stores.return_value.first.return_value = storage
            inspect.side_effect = RuntimeError('sensitive provider text')
            with self.assertRaises(CommandError) as error:
                call_command(
                    'check_s3_multipart_uploads',
                    operation_id=str(self.record.pk),
                    expected_owner='123456789012',
                    stdout=output,
                )
            self.assertNotIn('sensitive provider text', str(error.exception))
            self.assertEqual(self.record.state, 'queued')

    def test_command_rejects_ambient_credentials_before_client_construction(self):
        storage = MagicMock()
        storage.aws_access_key_id = None
        with patch(
            'io_storages.management.commands.check_s3_multipart_uploads.StorageOperation.objects.filter'
        ) as records, patch(
            'io_storages.management.commands.check_s3_multipart_uploads.S3ExportStorage.objects.filter'
        ) as stores:
            records.return_value.first.return_value = self.record
            stores.return_value.first.return_value = storage
            with self.assertRaises(CommandError):
                call_command(
                    'check_s3_multipart_uploads',
                    operation_id=str(self.record.pk),
                    expected_owner='123456789012',
                    legacy_current_configuration=True,
                    stdout=StringIO(),
                )
            storage.get_client.assert_not_called()

    def test_legacy_command_exposes_non_authoritative_identity_notice(self):
        storage = MagicMock()
        storage.s3_endpoint = None
        storage.get_client.return_value = self.client
        output = StringIO()
        with patch(
            'io_storages.management.commands.check_s3_multipart_uploads.StorageOperation.objects.filter'
        ) as records, patch(
            'io_storages.management.commands.check_s3_multipart_uploads.S3ExportStorage.objects.filter'
        ) as stores, patch(
            'io_storages.management.commands.check_s3_multipart_uploads.inspect_aws_multipart_page'
        ) as inspect:
            records.return_value.first.return_value = self.record
            stores.return_value.first.return_value = storage
            inspect.return_value = {'storage_identity': 'CURRENT_CONFIGURATION_ONLY', 'outcome': 'UNKNOWN'}
            call_command(
                'check_s3_multipart_uploads',
                operation_id=str(self.record.pk),
                expected_owner='123456789012',
                legacy_current_configuration=True,
                stdout=output,
            )
            self.assertTrue(inspect.call_args.kwargs['legacy_current_configuration'])
            result = json.loads(output.getvalue())
            self.assertEqual(result['outcome'], 'UNKNOWN')
            self.assertIn('not proof of the historical request destination', result['notice'])
