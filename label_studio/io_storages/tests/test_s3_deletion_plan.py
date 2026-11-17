"""A point-in-time S3 observation never grants automatic delete permission."""
import threading
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection
from django.test import SimpleTestCase, TransactionTestCase
from io_storages.admission_models import StorageOperation
from io_storages.s3.deletion_plan import describe_version_deletion
from io_storages.s3.models import S3ExportStorage
from organizations.tests.factories import OrganizationFactory
from projects.tests.factories import ProjectFactory
from users.product_tours.models import UserProductTour  # noqa: F401


class S3DeletionPlanTests(SimpleTestCase):
    def test_each_valid_history_keeps_execution_blocked(self):
        for history, expected_visibility in (
            ('noncurrent', 'CURRENT_VERSION_UNCHANGED_IF_HISTORY_STABLE'),
            ('current_with_other_object_versions', 'OLDER_OBJECT_MAY_BECOME_VISIBLE'),
            ('current_without_other_object_versions', 'NO_OLDER_OBJECT_OBSERVED'),
        ):
            with self.subTest(history=history):
                result = describe_version_deletion(history)
                self.assertEqual(result['visibility'], expected_visibility)
                self.assertEqual(result['automatic_execution'], 'BLOCKED')
                self.assertEqual(result['observation'], 'NON_SNAPSHOT')
                self.assertIn('CLOUD_ISOLATION_AND_DRAIN_UNVERIFIED', result['blockers'])
                self.assertIn('VERSION_HISTORY_NOT_A_SNAPSHOT', result['blockers'])
                self.assertIn('OBJECT_LOCK_NOT_VERIFIED', result['blockers'])
                self.assertEqual(
                    'OLDER_OBJECT_MAY_BECOME_VISIBLE' in result['blockers'],
                    history == 'current_with_other_object_versions',
                )

    def test_unknown_or_malformed_history_is_refused(self):
        for history in (None, '', 'missing', 'UNKNOWN', [], {}, True):
            with self.subTest(history=history), self.assertRaises(ValueError):
                describe_version_deletion(history)

    def test_check_command_does_not_delete_when_inspection_fails_or_returns_a_plan(self):
        record = MagicMock(storage_id=12, project_id=34)
        storage, client = MagicMock(), MagicMock()
        storage.s3_endpoint = None
        storage.get_client.return_value = client
        path = 'io_storages.management.commands.check_s3_deletion_target.'
        with patch(path + 'StorageOperation.objects.filter') as records, patch(
            path + 'S3ExportStorage.objects.filter'
        ) as storages, patch(path + 'verify_aws_deletion_target') as verify, patch(
            path + 'inspect_aws_deletion_history'
        ) as history:
            records.return_value.first.return_value = record
            storages.return_value.first.return_value = storage
            output = StringIO()
            history.return_value = 'noncurrent'
            call_command('check_s3_deletion_target', operation_id='example', expected_owner='123456789012', stdout=output)
            self.assertIn('"automatic_execution": "BLOCKED"', output.getvalue())
            self.assertIn('does not authorize deletion', output.getvalue())
            # A lost provider response cannot be converted into success or a retry.
            history.side_effect = TimeoutError('sensitive provider detail')
            with self.assertRaises(CommandError) as error:
                call_command('check_s3_deletion_target', operation_id='example', expected_owner='123456789012')
            self.assertNotIn('sensitive provider detail', str(error.exception))
            verify.assert_called()
        client.delete_object.assert_not_called()
        record.save.assert_not_called()


class S3DeletionPlanConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires PostgreSQL transactions')

    def create_pending(self):
        project = ProjectFactory(organization=OrganizationFactory())
        storage = S3ExportStorage.objects.create(
            project=project, bucket='synthetic', aws_access_key_id='synthetic', aws_secret_access_key='synthetic'
        )
        record = StorageOperation.objects.create(
            project=project,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
            lifecycle_revision=0,
            operation='annotation_delete_pending',
            state=StorageOperation.State.QUEUED,
        )
        return storage, record

    def test_ledger_change_during_remote_observation_cannot_authorize_or_clear_it(self):
        storage, record = self.create_pending()
        entered, release = threading.Event(), threading.Event()
        client = MagicMock()

        def observe(*args):
            entered.set()
            if not release.wait(20):
                raise TimeoutError('fixture timed out')
            return 'noncurrent'

        def inspect():
            close_old_connections()
            try:
                output = StringIO()
                call_command(
                    'check_s3_deletion_target',
                    operation_id=str(record.pk),
                    expected_owner='123456789012',
                    stdout=output,
                )
                return output.getvalue()
            finally:
                connection.close()

        path = 'io_storages.management.commands.check_s3_deletion_target.'
        with patch.object(S3ExportStorage, 'get_client', return_value=client), patch(
            path + 'verify_aws_deletion_target'
        ), patch(path + 'inspect_aws_deletion_history', side_effect=observe), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(inspect)
            try:
                self.assertTrue(entered.wait(20))
                StorageOperation.objects.filter(pk=record.pk).update(state=StorageOperation.State.UNCERTAIN)
            finally:
                release.set()
            with self.assertRaisesRegex(CommandError, 'changed during inspection'):
                future.result(timeout=20)
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.UNCERTAIN)
        client.delete_object.assert_not_called()

    def test_lost_read_response_keeps_original_pending_through_later_inspection(self):
        storage, record = self.create_pending()
        client = MagicMock()
        path = 'io_storages.management.commands.check_s3_deletion_target.'
        with patch.object(S3ExportStorage, 'get_client', return_value=client), patch(
            path + 'verify_aws_deletion_target'
        ), patch(path + 'inspect_aws_deletion_history', side_effect=[TimeoutError('lost response'), 'noncurrent']):
            with self.assertRaises(CommandError):
                call_command('check_s3_deletion_target', operation_id=str(record.pk), expected_owner='123456789012')
            record.refresh_from_db()
            self.assertEqual(record.state, StorageOperation.State.QUEUED)
            output = StringIO()
            call_command(
                'check_s3_deletion_target', operation_id=str(record.pk), expected_owner='123456789012', stdout=output
            )
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.QUEUED)
        self.assertIn('"automatic_execution": "BLOCKED"', output.getvalue())
        client.delete_object.assert_not_called()
