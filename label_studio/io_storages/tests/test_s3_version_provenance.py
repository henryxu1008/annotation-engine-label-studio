"""S3 identity preflight must fail closed before any version deletion exists."""
import json
import time
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

from botocore.exceptions import ClientError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from io_storages.s3.deletion_plan import describe_version_deletion
from io_storages.s3.version_provenance import (
    _export_cursor_cipher,
    inspect_aws_deletion_history,
    inspect_aws_export_request,
    inspect_aws_export_request_page,
    storage_binding,
    verify_aws_deletion_target,
)


@override_settings(SECRET_KEY='synthetic-issue86-secret')
class S3VersionIdentityTests(SimpleTestCase):
    def setUp(self):
        self.storage = SimpleNamespace(
            project_id=4,
            pk=9,
            bucket='synthetic-bucket',
            prefix='a',
            s3_endpoint=None,
            region_name='us-east-1',
            aws_access_key_id='synthetic',
            aws_secret_access_key='synthetic-secret',
            expected_bucket_owner='123456789012',
            aws_session_token=None,
            _meta=SimpleNamespace(label_lower='io_storages.s3exportstorage'),
        )
        self.record = SimpleNamespace(
            pk=UUID('00000000-0000-0000-0000-000000000001'),
            operation='annotation_delete_pending',
            project_id=4,
            storage_id=9,
            storage_model='io_storages.s3exportstorage',
            lifecycle_revision=0,
            subject_id=12,
            target_key='a/object',
            target_version_id='v1',
            target_object_token='a' * 32,
            target_storage_binding=storage_binding(self.storage),
        )
        self.client = MagicMock()
        self.client.meta.endpoint_url = 'https://s3.us-east-1.amazonaws.com'
        self.client.get_bucket_versioning.return_value = {'Status': 'Enabled'}
        self.client.head_object.return_value = {'VersionId': 'v1', 'Metadata': {'ls-export-token': 'a' * 32}}

    def test_verified_target_passes_expected_owner_to_both_requests(self):
        verify_aws_deletion_target(self.storage, self.record, '123456789012', self.client)
        self.client.head_bucket.assert_called_once_with(Bucket='synthetic-bucket', ExpectedBucketOwner='123456789012')
        self.client.get_bucket_versioning.assert_called_once_with(
            Bucket='synthetic-bucket',
            ExpectedBucketOwner='123456789012',
        )
        self.client.head_object.assert_called_once_with(
            Bucket='synthetic-bucket',
            Key='a/object',
            VersionId='v1',
            ExpectedBucketOwner='123456789012',
        )
        self.client.delete_object.assert_not_called()

    def test_custom_endpoint_is_refused_before_any_provider_request(self):
        self.storage.s3_endpoint = 'https://storage.example.test'
        with self.assertRaisesRegex(ValueError, 'standard AWS'):
            verify_aws_deletion_target(self.storage, self.record, '123456789012', self.client)
        self.client.head_bucket.assert_not_called()

    def test_owner_scope_binding_and_nonce_fail_closed(self):
        cases = [
            ('owner', lambda: verify_aws_deletion_target(self.storage, self.record, 'bad-owner', self.client)),
            (
                'other owner',
                lambda: verify_aws_deletion_target(self.storage, self.record, '999999999999', self.client),
            ),
            (
                'scope',
                lambda: (
                    setattr(self.record, 'project_id', 5),
                    verify_aws_deletion_target(self.storage, self.record, '123456789012', self.client),
                ),
            ),
            (
                'binding',
                lambda: (
                    setattr(self.storage, 'bucket', 'replacement'),
                    verify_aws_deletion_target(self.storage, self.record, '123456789012', self.client),
                ),
            ),
            (
                'token',
                lambda: (
                    self.client.head_object.return_value['Metadata'].update({'ls-export-token': 'b' * 32}),
                    verify_aws_deletion_target(self.storage, self.record, '123456789012', self.client),
                ),
            ),
        ]
        for name, operation in cases:
            with self.subTest(name=name):
                self.setUp()
                with self.assertRaises(ValueError):
                    operation()
                self.client.delete_object.assert_not_called()

    def test_legacy_binding_cannot_be_upgraded_by_setting_owner_after_write(self):
        self.storage.expected_bucket_owner = None
        self.record.target_storage_binding = storage_binding(self.storage)
        self.storage.expected_bucket_owner = '123456789012'
        with self.assertRaisesRegex(ValueError, 'binding changed'):
            verify_aws_deletion_target(self.storage, self.record, '123456789012', self.client)
        self.client.head_bucket.assert_not_called()

    def test_missing_or_null_version_is_refused(self):
        for version in (None, '', 'null'):
            self.record.target_version_id = version
            with self.subTest(version=version), self.assertRaises(ValueError):
                verify_aws_deletion_target(self.storage, self.record, '123456789012', self.client)
        self.client.head_bucket.assert_not_called()

    def test_provider_owner_mismatch_prevents_version_lookup(self):
        self.client.head_bucket.side_effect = ClientError(
            {'Error': {'Code': '403', 'Message': 'owner mismatch'}}, 'HeadBucket'
        )
        with self.assertRaises(ClientError):
            verify_aws_deletion_target(self.storage, self.record, '123456789012', self.client)
        self.client.head_object.assert_not_called()
        self.client.delete_object.assert_not_called()

    def test_versioning_suspended_or_unknown_fails_before_object_lookup(self):
        for status in ('Suspended', None, 'unexpected'):
            self.setUp()
            self.client.get_bucket_versioning.return_value = {'Status': status}
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'versioning must be enabled'):
                verify_aws_deletion_target(self.storage, self.record, '123456789012', self.client)
            self.client.head_object.assert_not_called()
            self.client.delete_object.assert_not_called()

    def test_missing_permission_to_read_versioning_fails_closed(self):
        self.client.get_bucket_versioning.side_effect = ClientError(
            {'Error': {'Code': '403', 'Message': 'access denied'}}, 'GetBucketVersioning'
        )
        with self.assertRaises(ClientError):
            verify_aws_deletion_target(self.storage, self.record, '123456789012', self.client)
        self.client.head_object.assert_not_called()
        self.client.delete_object.assert_not_called()

    def test_wrong_version_from_provider_is_rejected(self):
        self.client.head_object.return_value['VersionId'] = 'other-version'
        with self.assertRaisesRegex(ValueError, 'does not match'):
            verify_aws_deletion_target(self.storage, self.record, '123456789012', self.client)
        self.client.delete_object.assert_not_called()

    def test_object_lock_metadata_never_makes_a_read_only_observation_executable(self):
        self.client.head_object.return_value['ObjectLockMode'] = 'COMPLIANCE'
        self.client.head_object.return_value['ObjectLockLegalHoldStatus'] = 'ON'
        self.client.list_object_versions.return_value = {
            'IsTruncated': False,
            'Versions': [{'Key': 'a/object', 'VersionId': 'v1', 'IsLatest': True}],
        }
        verify_aws_deletion_target(self.storage, self.record, '123456789012', self.client)
        history = inspect_aws_deletion_history(self.storage, self.record, '123456789012', self.client)
        plan = describe_version_deletion(history)
        self.assertIn('OBJECT_LOCK_NOT_VERIFIED', plan['blockers'])
        self.assertEqual(plan['automatic_execution'], 'BLOCKED')
        self.client.delete_object.assert_not_called()

    def test_read_only_inspection_command_binds_record_and_never_deletes(self):
        storage = MagicMock()
        storage.s3_endpoint = None
        storage.get_client.return_value = self.client
        output = StringIO()
        with patch(
            'io_storages.management.commands.check_s3_deletion_target.StorageOperation.objects.filter'
        ) as records, patch(
            'io_storages.management.commands.check_s3_deletion_target.S3ExportStorage.objects.filter'
        ) as storages, patch(
            'io_storages.management.commands.check_s3_deletion_target.verify_aws_deletion_target'
        ) as verify, patch(
            'io_storages.management.commands.check_s3_deletion_target.inspect_aws_deletion_history',
            return_value='noncurrent',
        ) as history:
            records.return_value.first.return_value = self.record
            storages.return_value.first.return_value = storage
            call_command(
                'check_s3_deletion_target',
                operation_id='00000000-0000-0000-0000-000000000001',
                expected_owner='123456789012',
                stdout=output,
            )
        storages.assert_called_once_with(pk=self.record.storage_id, project_id=self.record.project_id)
        verify.assert_called_once_with(storage, self.record, '123456789012', self.client)
        history.assert_called_once_with(storage, self.record, '123456789012', self.client)
        self.client.delete_object.assert_not_called()
        self.assertIn('does not authorize deletion', output.getvalue())

    def test_history_paginates_exact_key_and_detects_older_versions(self):
        self.client.list_object_versions.side_effect = [
            {
                'IsTruncated': True,
                'NextKeyMarker': 'a/object',
                'NextVersionIdMarker': 'v1',
                'Versions': [{'Key': 'a/object', 'VersionId': 'v1', 'IsLatest': True}],
            },
            {
                'IsTruncated': False,
                'Versions': [
                    {'Key': 'a/object', 'VersionId': 'older', 'IsLatest': False},
                    {'Key': 'a/object-other', 'VersionId': 'unrelated', 'IsLatest': True},
                ],
            },
        ]
        self.assertEqual(
            inspect_aws_deletion_history(self.storage, self.record, '123456789012', self.client),
            'current_with_other_object_versions',
        )
        self.client.list_object_versions.assert_any_call(
            Bucket='synthetic-bucket',
            Prefix='a/object',
            ExpectedBucketOwner='123456789012',
            KeyMarker='a/object',
            VersionIdMarker='v1',
        )
        self.client.delete_object.assert_not_called()

    def test_history_distinguishes_noncurrent_and_current_without_older_objects(self):
        entry = {'Key': 'a/object', 'VersionId': 'v1', 'IsLatest': True}
        self.client.list_object_versions.return_value = {'IsTruncated': False, 'Versions': [entry]}
        self.assertEqual(
            inspect_aws_deletion_history(self.storage, self.record, '123456789012', self.client),
            'current_without_other_object_versions',
        )
        entry['IsLatest'] = False
        self.client.list_object_versions.return_value['DeleteMarkers'] = [
            {'Key': 'a/object', 'VersionId': 'marker', 'IsLatest': True},
        ]
        self.assertEqual(
            inspect_aws_deletion_history(self.storage, self.record, '123456789012', self.client), 'noncurrent'
        )

    def test_history_rejects_incomplete_ambiguous_and_looping_inventories(self):
        target = {'Key': 'a/object', 'VersionId': 'v1', 'IsLatest': True}
        cases = [
            [{}],
            [{'IsTruncated': False, 'Versions': []}],
            [{'IsTruncated': False, 'Versions': [target, target]}],
            [
                {
                    'IsTruncated': False,
                    'Versions': [target],
                    'DeleteMarkers': [{'Key': 'a/object', 'VersionId': 'marker', 'IsLatest': True}],
                }
            ],
            [{'IsTruncated': True, 'Versions': [target]}],
            [
                {'IsTruncated': True, 'NextKeyMarker': 'a/object', 'NextVersionIdMarker': 'x'},
                {'IsTruncated': True, 'NextKeyMarker': 'a/object', 'NextVersionIdMarker': 'x'},
            ],
        ]
        for pages in cases:
            with self.subTest(pages=pages):
                self.client.list_object_versions.side_effect = pages
                with self.assertRaises(ValueError):
                    inspect_aws_deletion_history(self.storage, self.record, '123456789012', self.client)
        self.client.delete_object.assert_not_called()

    def test_history_rejects_page_limit_and_provider_failure(self):
        self.client.list_object_versions.return_value = {
            'IsTruncated': True,
            'NextKeyMarker': 'a/object',
            'NextVersionIdMarker': 'v1',
        }
        with self.assertRaisesRegex(ValueError, 'page limit'):
            inspect_aws_deletion_history(self.storage, self.record, '123456789012', self.client, max_pages=1)
        self.client.list_object_versions.side_effect = ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'denied'}}, 'ListObjectVersions'
        )
        with self.assertRaises(ClientError):
            inspect_aws_deletion_history(self.storage, self.record, '123456789012', self.client)

    def test_export_request_inspection_is_paged_scoped_and_always_unknown(self):
        self.record.operation = 'annotation_export'
        self.record.state = 'uncertain'
        self.record.subject_id = 12
        self.client.list_object_versions.side_effect = [
            {
                'IsTruncated': True,
                'NextKeyMarker': 'a/object',
                'NextVersionIdMarker': 'v1',
                'Versions': [
                    {'Key': 'a/object', 'VersionId': 'v1'},
                    {'Key': 'a/object-other', 'VersionId': 'unrelated'},
                ],
            },
            {'IsTruncated': False, 'Versions': [{'Key': 'a/object', 'VersionId': 'v2'}]},
        ]
        self.client.head_object.side_effect = [
            {'VersionId': 'v1', 'Metadata': {'ls-export-token': 'a' * 32}},
            {'VersionId': 'v2', 'Metadata': {'ls-export-token': 'b' * 32}},
        ]
        result = inspect_aws_export_request(self.storage, self.record, '123456789012', self.client)
        self.assertEqual(result, {'inspected_versions': 2, 'matching_versions': 1, 'outcome': 'UNKNOWN'})
        self.assertEqual(self.client.head_object.call_count, 2)
        self.client.list_object_versions.assert_any_call(
            Bucket='synthetic-bucket',
            Prefix='a/object',
            ExpectedBucketOwner='123456789012',
            KeyMarker='a/object',
            VersionIdMarker='v1',
        )
        self.client.delete_object.assert_not_called()

    def test_export_request_inspection_rejects_unbound_terminal_or_incomplete_evidence(self):
        self.record.operation = 'annotation_export'
        self.record.state = 'uncertain'
        self.record.subject_id = 12
        for state, token in (('completed', 'a' * 32), ('uncertain', ''), ('uncertain', None)):
            with self.subTest(state=state, token=token), self.assertRaises(ValueError):
                self.record.state, self.record.target_object_token = state, token
                inspect_aws_export_request(self.storage, self.record, '123456789012', self.client)
        self.record.state, self.record.target_object_token, self.record.subject_id = 'uncertain', 'a' * 32, None
        with self.assertRaises(ValueError):
            inspect_aws_export_request(self.storage, self.record, '123456789012', self.client)
        self.client.head_bucket.assert_not_called()
        self.record.subject_id = 12
        self.client.list_object_versions.return_value = {
            'IsTruncated': False,
            'Versions': [
                {'Key': 'a/object', 'VersionId': 'v1'},
                {'Key': 'a/object', 'VersionId': 'v2'},
            ],
        }
        with self.assertRaisesRegex(ValueError, 'inspection limit'):
            inspect_aws_export_request(self.storage, self.record, '123456789012', self.client, max_versions=1)
        self.client.delete_object.assert_not_called()

    def test_export_inspection_command_keeps_outcome_unknown_and_hides_secrets(self):
        storage = MagicMock()
        storage.bucket = 'synthetic-bucket'
        storage.aws_access_key_id = 'synthetic'
        storage.aws_secret_access_key = 'synthetic-secret'
        storage.s3_endpoint = None
        storage.get_client.return_value = self.client
        output = StringIO()
        with patch(
            'io_storages.management.commands.check_s3_export_request.StorageOperation.objects.filter'
        ) as records, patch(
            'io_storages.management.commands.check_s3_export_request.S3ExportStorage.objects.filter'
        ) as storages, patch(
            'io_storages.management.commands.check_s3_export_request.inspect_aws_export_request_page'
        ) as inspect:
            records.return_value.first.return_value = self.record
            storages.return_value.first.return_value = storage
            inspect.return_value = {
                'inspected_versions': 2,
                'matching_version_ids': ['v1\nsynthetic'],
                'page': 1,
                'next_cursor': None,
                'outcome': 'UNKNOWN',
            }
            call_command(
                'check_s3_export_request',
                operation_id='00000000-0000-0000-0000-000000000001',
                expected_owner='123456789012',
                stdout=output,
            )
            inspect.assert_called_once_with(
                storage, self.record, '123456789012', self.client, cursor=None, page_size=25
            )
            self.assertIn('UNKNOWN', output.getvalue())
            self.assertNotIn(self.record.target_object_token, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())['matching_version_ids'], ['v1\nsynthetic'])
            self.assertNotIn('\n', output.getvalue().strip())
            inspect.side_effect = ValueError('sensitive provider detail')
            with self.assertRaises(CommandError) as error:
                call_command(
                    'check_s3_export_request',
                    operation_id='00000000-0000-0000-0000-000000000001',
                    expected_owner='123456789012',
                )
            self.assertNotIn('sensitive provider detail', str(error.exception))
        self.client.delete_object.assert_not_called()

    def test_export_page_cursor_is_encrypted_bound_and_reports_only_observed_version_ids(self):
        self.record.operation, self.record.state, self.record.subject_id = 'annotation_export', 'uncertain', 12
        self.client.list_object_versions.side_effect = [
            {
                'IsTruncated': True,
                'NextKeyMarker': 'a/object',
                'NextVersionIdMarker': 'v1',
                'Versions': [
                    {'Key': 'a/object', 'VersionId': 'v1'},
                    {'Key': 'a/object-other', 'VersionId': 'other'},
                ],
            },
            {'IsTruncated': False, 'Versions': [{'Key': 'a/object', 'VersionId': 'v2'}]},
        ]
        self.client.head_object.side_effect = [
            {'VersionId': 'v1', 'Metadata': {'ls-export-token': 'a' * 32}},
            {'VersionId': 'v2', 'Metadata': {'ls-export-token': 'b' * 32}},
        ]
        first = inspect_aws_export_request_page(self.storage, self.record, '123456789012', self.client, page_size=2)
        self.assertEqual((first['page'], first['matching_version_ids'], first['outcome']), (1, ['v1'], 'UNKNOWN'))
        cursor = first['next_cursor']
        self.assertTrue(cursor)
        self.assertNotIn(self.record.target_key, cursor)
        self.assertNotIn(self.record.target_object_token, cursor)
        second = inspect_aws_export_request_page(
            self.storage, self.record, '123456789012', self.client, cursor=cursor, page_size=2
        )
        self.assertEqual((second['page'], second['matching_version_ids'], second['next_cursor']), (2, [], None))
        self.client.list_object_versions.assert_any_call(
            Bucket='synthetic-bucket',
            Prefix='a/object',
            ExpectedBucketOwner='123456789012',
            KeyMarker='a/object',
            VersionIdMarker='v1',
            MaxKeys=2,
        )
        self.client.delete_object.assert_not_called()

    def test_export_page_rejects_tampered_expired_or_cross_scope_cursor_before_network(self):
        self.record.operation, self.record.state, self.record.subject_id = 'annotation_export', 'uncertain', 12
        self.client.list_object_versions.return_value = {
            'IsTruncated': True,
            'NextKeyMarker': 'a/object',
            'NextVersionIdMarker': 'v1',
            'Versions': [{'Key': 'a/object', 'VersionId': 'v1'}],
        }
        cursor = inspect_aws_export_request_page(self.storage, self.record, '123456789012', self.client)['next_cursor']
        self.client.reset_mock()
        middle = len(cursor) // 2
        tampered = cursor[:middle] + ('A' if cursor[middle] != 'A' else 'B') + cursor[middle + 1 :]
        expired = (
            _export_cursor_cipher()
            .encrypt_at_time(_export_cursor_cipher().decrypt(cursor.encode()), int(time.time()) - 7200)
            .decode()
        )
        for token, owner in ((tampered, '123456789012'), (expired, '123456789012'), (cursor, '999999999999')):
            with self.subTest(owner=owner), self.assertRaisesRegex(
                ValueError, 'Invalid or expired|owner was not bound'
            ):
                inspect_aws_export_request_page(self.storage, self.record, owner, self.client, cursor=token)
        self.record.subject_id = 13
        with self.assertRaisesRegex(ValueError, 'Invalid or expired'):
            inspect_aws_export_request_page(self.storage, self.record, '123456789012', self.client, cursor=cursor)
        self.record.subject_id = 12
        self.record.target_object_token = 'b' * 32
        with self.assertRaisesRegex(ValueError, 'Invalid or expired'):
            inspect_aws_export_request_page(self.storage, self.record, '123456789012', self.client, cursor=cursor)
        self.record.target_object_token = 'a' * 32
        self.record.pk = UUID('00000000-0000-0000-0000-000000000002')
        with self.assertRaisesRegex(ValueError, 'Invalid or expired'):
            inspect_aws_export_request_page(self.storage, self.record, '123456789012', self.client, cursor=cursor)
        self.record.pk = UUID('00000000-0000-0000-0000-000000000001')
        with self.assertRaisesRegex(ValueError, 'Invalid or expired'):
            inspect_aws_export_request_page(
                self.storage, self.record, '123456789012', self.client, cursor=cursor, page_size=10
            )
        self.client.head_bucket.assert_not_called()
        self.client.list_object_versions.assert_not_called()

    def test_export_page_rejects_incomplete_repeated_and_excessive_pagination(self):
        self.record.operation, self.record.state, self.record.subject_id = 'annotation_export', 'uncertain', 12
        for page in (
            {},
            {'IsTruncated': True},
            {'IsTruncated': False, 'Versions': 'invalid'},
            {'IsTruncated': False, 'Versions': [{'Key': 'a/object', 'VersionId': 'null'}]},
        ):
            self.client.list_object_versions.return_value = page
            with self.subTest(page=page), self.assertRaises(ValueError):
                inspect_aws_export_request_page(self.storage, self.record, '123456789012', self.client)
        self.client.list_object_versions.return_value = {
            'IsTruncated': True,
            'NextKeyMarker': 'a/object',
            'NextVersionIdMarker': 'v1',
            'Versions': [{'Key': 'a/object', 'VersionId': 'v1'}],
        }
        cursor = inspect_aws_export_request_page(self.storage, self.record, '123456789012', self.client)['next_cursor']
        with self.assertRaisesRegex(ValueError, 'continuation'):
            inspect_aws_export_request_page(self.storage, self.record, '123456789012', self.client, cursor=cursor)
        payload = json.loads(_export_cursor_cipher().decrypt(cursor.encode()))
        payload['page'] = 100
        late = _export_cursor_cipher().encrypt(json.dumps(payload).encode()).decode()
        with self.assertRaisesRegex(ValueError, 'continuation'):
            inspect_aws_export_request_page(self.storage, self.record, '123456789012', self.client, cursor=late)

    def test_export_page_can_retry_same_cursor_after_provider_error_without_resolving(self):
        self.record.operation, self.record.state, self.record.subject_id = 'annotation_export', 'uncertain', 12
        first_page = {
            'IsTruncated': True,
            'NextKeyMarker': 'a/object',
            'NextVersionIdMarker': 'v1',
            'Versions': [{'Key': 'a/object', 'VersionId': 'v1'}],
        }
        second_page = {'IsTruncated': False, 'Versions': [{'Key': 'a/object', 'VersionId': 'v2'}]}
        self.client.list_object_versions.side_effect = [first_page, second_page, second_page]
        self.client.head_object.side_effect = [
            {'VersionId': 'v1', 'Metadata': {'ls-export-token': 'a' * 32}},
            ClientError({'Error': {'Code': '503', 'Message': 'synthetic'}}, 'HeadObject'),
            {'VersionId': 'v2', 'Metadata': {'ls-export-token': 'a' * 32}},
        ]
        cursor = inspect_aws_export_request_page(self.storage, self.record, '123456789012', self.client)['next_cursor']
        with self.assertRaises(ClientError):
            inspect_aws_export_request_page(self.storage, self.record, '123456789012', self.client, cursor=cursor)
        retried = inspect_aws_export_request_page(
            self.storage, self.record, '123456789012', self.client, cursor=cursor
        )
        self.assertEqual(
            (retried['matching_version_ids'], retried['outcome'], retried['next_cursor']), (['v2'], 'UNKNOWN', None)
        )
        self.assertEqual(self.record.state, 'uncertain')
        self.client.delete_object.assert_not_called()

    def test_export_inspection_rejects_ambient_credentials_before_client_creation(self):
        storage = MagicMock()
        storage.bucket = 'synthetic-bucket'
        storage.aws_access_key_id = 'synthetic'
        storage.s3_endpoint = None
        storage.aws_secret_access_key = None
        with patch(
            'io_storages.management.commands.check_s3_export_request.StorageOperation.objects.filter'
        ) as records, patch(
            'io_storages.management.commands.check_s3_export_request.S3ExportStorage.objects.filter'
        ) as storages:
            records.return_value.first.return_value = self.record
            storages.return_value.first.return_value = storage
            with self.assertRaisesRegex(CommandError, 'Explicit S3 credentials'):
                call_command(
                    'check_s3_export_request',
                    operation_id='00000000-0000-0000-0000-000000000001',
                    expected_owner='123456789012',
                )
            storage.get_client.assert_not_called()

    def test_inspection_command_hides_provider_exception_details(self):
        storage = MagicMock()
        storage.s3_endpoint = None
        storage.get_client.side_effect = ValueError('sensitive provider detail')
        with patch(
            'io_storages.management.commands.check_s3_deletion_target.StorageOperation.objects.filter'
        ) as records, patch(
            'io_storages.management.commands.check_s3_deletion_target.S3ExportStorage.objects.filter'
        ) as storages:
            records.return_value.first.return_value = self.record
            storages.return_value.first.return_value = storage
            with self.assertRaises(CommandError) as error:
                call_command(
                    'check_s3_deletion_target',
                    operation_id='00000000-0000-0000-0000-000000000001',
                    expected_owner='123456789012',
                )
        self.assertNotIn('sensitive provider detail', str(error.exception))
        self.client.delete_object.assert_not_called()

    def test_inspection_command_rejects_unknown_or_missing_storage_before_network(self):
        with patch(
            'io_storages.management.commands.check_s3_deletion_target.StorageOperation.objects.filter'
        ) as records, patch(
            'io_storages.management.commands.check_s3_deletion_target.S3ExportStorage.objects.filter'
        ) as storages, patch(
            'io_storages.management.commands.check_s3_deletion_target.verify_aws_deletion_target'
        ) as verify:
            records.return_value.first.return_value = None
            with self.assertRaisesRegex(CommandError, 'queued S3 annotation deletion'):
                call_command(
                    'check_s3_deletion_target',
                    operation_id='00000000-0000-0000-0000-000000000001',
                    expected_owner='123456789012',
                )
            storages.assert_not_called()
            records.return_value.first.return_value = self.record
            storages.return_value.first.return_value = None
            with self.assertRaisesRegex(CommandError, 'no longer exists'):
                call_command(
                    'check_s3_deletion_target',
                    operation_id='00000000-0000-0000-0000-000000000001',
                    expected_owner='123456789012',
                )
            verify.assert_not_called()

    def test_inspection_command_rejects_ambient_credentials_and_custom_endpoint(self):
        storage = MagicMock()
        with patch(
            'io_storages.management.commands.check_s3_deletion_target.StorageOperation.objects.filter'
        ) as records, patch(
            'io_storages.management.commands.check_s3_deletion_target.S3ExportStorage.objects.filter'
        ) as storages:
            records.return_value.first.return_value = self.record
            storages.return_value.first.return_value = storage
            for secret, endpoint in ((None, None), ('synthetic', 'https://example.invalid')):
                storage.aws_secret_access_key = secret
                storage.s3_endpoint = endpoint
                with self.subTest(secret_present=bool(secret), custom_endpoint=bool(endpoint)), self.assertRaisesRegex(
                    CommandError, 'Explicit S3 credentials'
                ):
                    call_command(
                        'check_s3_deletion_target',
                        operation_id='00000000-0000-0000-0000-000000000001',
                        expected_owner='123456789012',
                    )
            storage.get_client.assert_not_called()
