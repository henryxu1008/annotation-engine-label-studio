"""Upgrade inventory is conservative and does not change broker state."""
from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings


@override_settings(RQ_QUEUES={'low': {'HOST': 'unused'}})
class UpgradeQueueInventoryTests(SimpleTestCase):
    def check_inventory(self, *, count=0, workers=()):
        queue = MagicMock()
        connection = queue.connection
        connection.scan.side_effect = lambda cursor, match, count: (
            0,
            [b'rq:failed:orphan'] if match == 'rq:failed:*' else [],
        )
        connection.zcard.return_value = count
        connection.scard.return_value = len(workers)
        with patch(
            'io_storages.management.commands.check_storage_upgrade_queues.django_rq.get_queue', return_value=queue
        ):
            out = StringIO()
            call_command('check_storage_upgrade_queues', stdout=out)
            return out.getvalue()

    def test_empty_inventory_is_only_point_in_time_observation(self):
        self.assertIn('point-in-time', self.check_inventory())

    def test_failed_jobs_and_any_registered_worker_block(self):
        for kwargs in ({'count': 1}, {'workers': [object()]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CommandError):
                self.check_inventory(**kwargs)

    def test_incomplete_scan_and_invalid_redis_type_fail_closed(self):
        queue = MagicMock()
        queue.connection.scard.return_value = 0
        queue.connection.scan.return_value = (1, [])
        with patch(
            'io_storages.management.commands.check_storage_upgrade_queues.django_rq.get_queue', return_value=queue
        ), patch('io_storages.management.commands.check_storage_upgrade_queues._MAX_SCAN_PAGES', 2):
            with self.assertRaisesRegex(CommandError, 'inventory failed'):
                call_command('check_storage_upgrade_queues', stdout=StringIO())
            queue.connection.scan.return_value = (0, [b'rq:queue:invalid'])
            queue.connection.llen.side_effect = RuntimeError('WRONGTYPE')
            with self.assertRaisesRegex(CommandError, 'inventory failed'):
                call_command('check_storage_upgrade_queues', stdout=StringIO())


@override_settings(RQ_QUEUES={'low': {'HOST': 'unused'}})
class UpgradeQueuePageTests(SimpleTestCase):
    def test_invalid_parameters_fail_before_connecting(self):
        common = {'connection': 'low', 'queue': 'legacy', 'kind': 'queued', 'stdout': StringIO()}
        invalid = (
            {'connection': 'unknown'},
            {'queue': '../bad'},
            {'kind': 'unknown'},
            {'offset': -1},
            {'offset': 1000001},
            {'limit': 0},
            {'limit': 101},
        )
        with patch('io_storages.management.commands.inspect_storage_upgrade_queue.django_rq.get_queue') as get_queue:
            for changes in invalid:
                with self.subTest(changes=changes), self.assertRaises((CommandError, TypeError)):
                    call_command('inspect_storage_upgrade_queue', **(common | changes))
            get_queue.assert_not_called()


@override_settings(RQ_QUEUES={'low': {'HOST': 'unused'}})
class UpgradeIdentityInventoryTests(SimpleTestCase):
    def check_inventory(self, *, results=None, redis_enabled=False, clients=None):
        database = MagicMock()
        database.vendor = 'postgresql'
        cursor = database.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = results or [(True,), (123, False), (0,), (0,), (0,), (0,)]
        queue = MagicMock()
        queue.connection.acl_getuser.return_value = {'enabled': redis_enabled}
        queue.connection.client_list.return_value = clients if clients is not None else [{'user': 'default'}]
        with patch('io_storages.management.commands.check_storage_upgrade_isolation.connection', database), patch(
            'io_storages.management.commands.check_storage_upgrade_isolation.django_rq.get_queue', return_value=queue
        ):
            out = StringIO()
            call_command('check_storage_upgrade_isolation', legacy_db_role='old', legacy_redis_user='old', stdout=out)
            return out.getvalue()

    def test_clear_identities_are_scoped_observation_only(self):
        output = self.check_inventory()
        self.assertIn('does not prevent reconnects', output)
        self.assertIn('establish cloud credential revocation', output)

    def test_active_login_member_or_database_session_blocks(self):
        cases = [
            [(False,)],
            [(True,), (123, True)],
            [(True,), (123, False), (1,)],
            [(True,), (123, False), (0,), (1,)],
            [(True,), (123, False), (0,), (0,), (1,)],
            [(True,), (123, False), (0,), (0,), (0,), (1,)],
        ]
        for results in cases:
            with self.subTest(results=results), self.assertRaises(CommandError):
                self.check_inventory(results=results)

    def test_enabled_redis_or_unaccounted_clients_block(self):
        for kwargs in ({'redis_enabled': True}, {'clients': [{'user': 'old'}]}, {'clients': [{}]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CommandError):
                self.check_inventory(**kwargs)
