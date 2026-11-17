"""An IAM observation must never become a cloud isolation attestation."""
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import MagicMock, patch

import boto3
from botocore.stub import Stubber
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase
from io_storages.s3.legacy_iam_inventory import inspect_legacy_iam_user


class LegacyIamInventoryTests(SimpleTestCase):
    def setUp(self):
        self.iam, self.sts = MagicMock(), MagicMock()
        self.iam.meta.endpoint_url = 'https://iam.amazonaws.com'
        self.sts.meta.endpoint_url = 'https://sts.us-east-1.amazonaws.com'
        self.sts.get_caller_identity.return_value = {'Account': '123456789012'}
        self.iam.get_user.return_value = {
            'User': {
                'UserName': 'old-worker',
                'UserId': 'AIDA1234567890123456',
                'Arn': 'arn:aws:iam::123456789012:user/old-worker',
            }
        }
        self.iam.list_access_keys.return_value = {'IsTruncated': False, 'AccessKeyMetadata': []}
        self.args = {'account_id': '123456789012', 'user_name': 'old-worker', 'user_id': 'AIDA1234567890123456'}

    def inspect(self, **kwargs):
        return inspect_legacy_iam_user(self.iam, self.sts, **(self.args | kwargs))

    def test_paginated_inventory_counts_keys_without_returning_identifiers(self):
        def key(suffix, status):
            return {'UserName': 'old-worker', 'AccessKeyId': 'AKIA' + suffix * 16, 'Status': status}

        self.iam.list_access_keys.side_effect = [
            {'IsTruncated': True, 'Marker': 'next', 'AccessKeyMetadata': [key('A', 'Inactive')]},
            {'IsTruncated': False, 'AccessKeyMetadata': [key('B', 'Active')]},
        ]
        result = self.inspect()
        self.assertEqual(
            result,
            {
                'active_long_term_keys': 1,
                'inactive_long_term_keys': 1,
                'isolation': 'UNKNOWN',
                'request_drain': 'UNKNOWN',
            },
        )
        self.iam.list_access_keys.assert_any_call(UserName='old-worker', MaxItems=100, Marker='next')
        self.assertNotIn('AKIA', str(result))

    def test_real_sdk_shapes_and_pagination_are_compatible(self):
        session = boto3.Session(
            aws_access_key_id='synthetic-key', aws_secret_access_key='synthetic-secret', region_name='us-east-1'
        )
        iam, sts = session.client('iam'), session.client('sts')
        with Stubber(iam) as iam_stub, Stubber(sts) as sts_stub:
            sts_stub.add_response('get_caller_identity', {'Account': '123456789012'}, {})
            user = dict(
                self.iam.get_user.return_value['User'], Path='/', CreateDate=datetime(2026, 1, 1, tzinfo=timezone.utc)
            )
            iam_stub.add_response('get_user', {'User': user}, {'UserName': 'old-worker'})
            iam_stub.add_response(
                'list_access_keys',
                {
                    'IsTruncated': True,
                    'Marker': 'next',
                    'AccessKeyMetadata': [],
                },
                {'UserName': 'old-worker', 'MaxItems': 100},
            )
            iam_stub.add_response(
                'list_access_keys',
                {
                    'IsTruncated': False,
                    'AccessKeyMetadata': [],
                },
                {'UserName': 'old-worker', 'MaxItems': 100, 'Marker': 'next'},
            )
            self.assertEqual(inspect_legacy_iam_user(iam, sts, **self.args)['request_drain'], 'UNKNOWN')
            iam_stub.assert_no_pending_responses()
            sts_stub.assert_no_pending_responses()

    def test_absent_keys_are_not_isolation_or_drain_proof(self):
        self.assertEqual(self.inspect()['isolation'], 'UNKNOWN')
        self.assertEqual(self.inspect()['request_drain'], 'UNKNOWN')
        self.iam.update_access_key.assert_not_called()
        self.iam.delete_access_key.assert_not_called()

    def test_wrong_or_recreated_principal_and_account_fail_closed(self):
        for change in ('account', 'user_id', 'arn'):
            with self.subTest(change=change):
                self.setUp()
                if change == 'account':
                    self.sts.get_caller_identity.return_value['Account'] = '999999999999'
                elif change == 'user_id':
                    self.iam.get_user.return_value['User']['UserId'] = 'AIDA9999999999999999'
                else:
                    self.iam.get_user.return_value['User']['Arn'] = 'arn:aws:iam::999999999999:user/old-worker'
                with self.assertRaises(ValueError):
                    self.inspect()
                self.iam.list_access_keys.assert_not_called()

    def test_endpoint_overrides_and_invalid_scope_fail_before_network(self):
        for endpoint in (
            'http://iam.amazonaws.com',
            'https://iam.amazonaws.com.evil.test',
            'https://user@iam.amazonaws.com',
            'https://iam.amazonaws.com/untrusted',
        ):
            with self.subTest(endpoint=endpoint):
                self.iam.meta.endpoint_url = endpoint
                with self.assertRaises(ValueError):
                    self.inspect()
                self.sts.get_caller_identity.assert_not_called()
        self.iam.meta.endpoint_url = 'https://iam.amazonaws.com'
        with self.assertRaises(ValueError):
            self.inspect(user_id='')
        self.sts.get_caller_identity.assert_not_called()

    def test_incomplete_unknown_duplicate_and_looping_pages_fail_closed(self):
        key = {'UserName': 'old-worker', 'AccessKeyId': 'AKIA' + 'A' * 16, 'Status': 'Inactive'}
        cases = [
            [{}],
            [{'IsTruncated': False, 'AccessKeyMetadata': [dict(key, Status='unknown')]}],
            [{'IsTruncated': False, 'AccessKeyMetadata': [key, key]}],
            [{'IsTruncated': True, 'AccessKeyMetadata': []}],
            [
                {'IsTruncated': True, 'AccessKeyMetadata': [], 'Marker': 'x'},
                {'IsTruncated': True, 'AccessKeyMetadata': [], 'Marker': 'x'},
            ],
        ]
        for pages in cases:
            with self.subTest(pages=pages):
                self.iam.list_access_keys.side_effect = pages
                with self.assertRaises(ValueError):
                    self.inspect()

    def test_command_outputs_only_counts_and_redacts_provider_errors(self):
        session = MagicMock()
        session.client.side_effect = lambda service: {'iam': self.iam, 'sts': self.sts}[service]
        output = StringIO()
        with patch('io_storages.management.commands.check_s3_legacy_iam_user.boto3.Session', return_value=session):
            call_command('check_s3_legacy_iam_user', stdout=output, **self.args)
            self.assertIn('"request_drain":"UNKNOWN"', output.getvalue())
            self.iam.get_user.side_effect = RuntimeError('sensitive provider error')
            with self.assertRaises(CommandError) as error:
                call_command('check_s3_legacy_iam_user', **self.args)
        self.assertNotIn('sensitive provider error', str(error.exception))
