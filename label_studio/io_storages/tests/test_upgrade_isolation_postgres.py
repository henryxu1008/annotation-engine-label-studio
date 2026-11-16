"""Opt-in PostgreSQL maintenance-window regression with disposable roles."""
import os
import unittest
import uuid
from io import StringIO
from unittest.mock import MagicMock, patch

import redis
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.backends.postgresql.base import Database
from django.test import SimpleTestCase, override_settings


@unittest.skipUnless(os.environ.get('ISSUE86_POSTGRES_ISOLATION_TEST') == '1', 'requires disposable PostgreSQL')
@override_settings(RQ_QUEUES={'low': {'HOST': 'unused'}})
class PostgresUpgradeIsolationTests(SimpleTestCase):
    # No app rows are changed; avoid TransactionTestCase's global table flush,
    # which is incompatible with unmanaged upstream foreign-key tables.
    databases = {'default'}

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires PostgreSQL')
        self.suffix = uuid.uuid4().hex[:16]
        self.legacy = 'i86_old_' + self.suffix
        self.alias = 'i86_alias_' + self.suffix
        self.transaction_id = 'i86_' + self.suffix
        self.legacy_sql = connection.ops.quote_name(self.legacy)
        self.alias_sql = connection.ops.quote_name(self.alias)
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE ROLE {self.legacy_sql} NOLOGIN')
            cursor.execute(f"CREATE ROLE {self.alias_sql} LOGIN PASSWORD 'fixture-only-password'")
            cursor.execute(f'GRANT {self.legacy_sql} TO {self.alias_sql}')
        self.addCleanup(self.drop_roles)

    def drop_roles(self):
        with connection.cursor() as cursor:
            cursor.execute(f'DROP ROLE IF EXISTS {self.alias_sql}')
            cursor.execute(f'DROP ROLE IF EXISTS {self.legacy_sql}')

    def check_isolation(self):
        queue = MagicMock()
        queue.connection.acl_getuser.return_value = {'enabled': False}
        queue.connection.client_list.return_value = [{'user': 'default'}]
        with patch(
            'io_storages.management.commands.check_storage_upgrade_isolation.django_rq.get_queue', return_value=queue
        ):
            call_command(
                'check_storage_upgrade_isolation',
                legacy_db_role=self.legacy,
                legacy_redis_user='old',
                stdout=StringIO(),
            )

    def new_connection(self, *, alias=False):
        params = connection.get_connection_params()
        if alias:
            params.update(user=self.alias, password='fixture-only-password')
        else:
            params['password'] = connection.settings_dict['PASSWORD']
        conn = Database.connect(**params)
        conn.autocommit = True
        return conn

    def test_revoked_membership_does_not_drain_existing_set_role_session(self):
        old_session = self.new_connection(alias=True)
        try:
            with old_session.cursor() as cursor:
                cursor.execute(f'SET ROLE {self.legacy_sql}')
            with connection.cursor() as cursor:
                cursor.execute(f'REVOKE {self.legacy_sql} FROM {self.alias_sql}')
            with old_session.cursor() as cursor:
                cursor.execute('SELECT current_user')
                self.assertEqual(cursor.fetchone()[0], self.legacy)
            with self.assertRaisesRegex(CommandError, 'Other database client sessions'):
                self.check_isolation()
        finally:
            old_session.close()
        self.check_isolation()

    def test_prepared_transaction_blocks_after_source_session_disconnects(self):
        with connection.cursor() as cursor:
            cursor.execute(f'REVOKE {self.legacy_sql} FROM {self.alias_sql}')
        source = self.new_connection()
        try:
            with source.cursor() as cursor:
                cursor.execute('BEGIN')
                cursor.execute('SELECT 1')
                cursor.execute(f"PREPARE TRANSACTION '{self.transaction_id}'")
        finally:
            source.close()
        try:
            with self.assertRaisesRegex(CommandError, 'Prepared database transactions'):
                self.check_isolation()
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f"ROLLBACK PREPARED '{self.transaction_id}'")
        self.check_isolation()

    @unittest.skipUnless(os.environ.get('ISSUE86_REDIS_ISOLATION_TEST') == '1', 'requires disposable Redis')
    def test_disabled_redis_user_still_has_an_existing_connection(self):
        with connection.cursor() as cursor:
            cursor.execute(f'REVOKE {self.legacy_sql} FROM {self.alias_sql}')
        admin = redis.Redis(host='127.0.0.1', port=6379, decode_responses=True)
        redis_user = 'i86_' + self.suffix
        old_client = None
        try:
            admin.execute_command('ACL', 'SETUSER', redis_user, 'on', '>fixture-only-password', '~*', '+@all')
            old_client = redis.Redis(
                host='127.0.0.1',
                port=6379,
                username=redis_user,
                password='fixture-only-password',
                decode_responses=True,
            )
            self.assertTrue(old_client.ping())
            admin.execute_command('ACL', 'SETUSER', redis_user, 'off')
            queue = MagicMock()
            queue.connection = admin
            with patch(
                'io_storages.management.commands.check_storage_upgrade_isolation.django_rq.get_queue',
                return_value=queue,
            ):
                with self.assertRaisesRegex(CommandError, 'still has clients'):
                    call_command(
                        'check_storage_upgrade_isolation',
                        legacy_db_role=self.legacy,
                        legacy_redis_user=redis_user,
                        stdout=StringIO(),
                    )
                old_client.close()
                old_client = None
                call_command(
                    'check_storage_upgrade_isolation',
                    legacy_db_role=self.legacy,
                    legacy_redis_user=redis_user,
                    stdout=StringIO(),
                )
        finally:
            if old_client is not None:
                old_client.close()
            admin.execute_command('ACL', 'DELUSER', redis_user)
            admin.close()
