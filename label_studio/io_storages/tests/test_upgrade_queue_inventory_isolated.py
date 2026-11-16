"""Exercise inventory against a dedicated Redis, with no job deserialization."""
import json
import os
import unittest
import uuid
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from redis import Redis
from rq import Queue


@unittest.skipUnless(os.environ.get('ISSUE86_DISPOSABLE_REDIS_TEST') == '1', 'requires disposable Redis')
@override_settings(RQ_QUEUES={'low': {'HOST': 'unused'}})
class IsolatedQueueInventoryTests(SimpleTestCase):
    def setUp(self):
        self.redis = Redis(host='127.0.0.1', port=6379)
        self.name = 'issue86-' + uuid.uuid4().hex
        self.keys = []
        self.queue = Queue('low', connection=self.redis)

    def tearDown(self):
        if self.keys:
            self.redis.delete(*self.keys)
        self.redis.close()

    def inventory(self):
        with patch(
            'io_storages.management.commands.check_storage_upgrade_queues.django_rq.get_queue', return_value=self.queue
        ):
            return call_command('check_storage_upgrade_queues', stdout=StringIO())

    def inspect(self, kind, *, offset=0, limit=2):
        out = StringIO()
        with patch(
            'io_storages.management.commands.inspect_storage_upgrade_queue.django_rq.get_queue',
            return_value=self.queue,
        ):
            call_command(
                'inspect_storage_upgrade_queue',
                connection='low',
                queue=self.name,
                kind=kind,
                offset=offset,
                limit=limit,
                stdout=out,
            )
        return json.loads(out.getvalue())

    def test_empty_dedicated_broker_passes(self):
        self.inventory()

    def test_unexpected_structure_blocks_without_modification(self):
        key = 'rq:failed:' + self.name
        self.keys.append(key)
        self.redis.set(key, 'unexpected-type')
        before = self.redis.dump(key)
        with self.assertRaisesRegex(CommandError, 'inventory failed'):
            self.inventory()
        self.assertEqual(self.redis.dump(key), before)

    def test_orphan_queue_and_expired_registries_block_without_cleanup(self):
        # None of these names is registered in rq:queues; the IDs need not
        # deserialize or still have corresponding job hashes to block a drain.
        entries = (
            ('rq:queue:', 'list'),
            ('rq:wip:', 'zset'),
            ('rq:deferred:', 'zset'),
            ('rq:failed:', 'zset'),
            ('rq:scheduled:', 'zset'),
            ('rq:canceled:', 'zset'),
            ('rq:workers:', 'set'),
        )
        for prefix, kind in entries:
            key = prefix + self.name
            self.keys.append(key)
            if kind == 'list':
                self.redis.lpush(key, 'legacy-unknown')
            elif kind == 'zset':
                self.redis.zadd(key, {'legacy-unknown': 1})
            else:
                self.redis.sadd(key, 'missing-worker')
            before = self.redis.dump(key)
            with self.subTest(key=key), self.assertRaises(CommandError):
                self.inventory()
            self.assertEqual(self.redis.dump(key), before)
            self.redis.delete(key)

    def test_stale_global_worker_registration_is_not_removed(self):
        key = 'rq:workers'
        self.assertEqual(self.redis.scard(key), 0, 'fixture Redis must be dedicated and empty')
        self.keys.append(key)
        self.redis.sadd(key, 'rq:worker:' + self.name)
        before = self.redis.dump(key)
        with self.assertRaises(CommandError):
            self.inventory()
        self.assertEqual(self.redis.dump(key), before)

    def test_orphan_and_expired_entries_are_paged_without_changes(self):
        for kind, prefix, is_list in (
            ('queued', 'rq:queue:', True),
            ('started', 'rq:wip:', False),
            ('failed', 'rq:failed:', False),
            ('scheduled', 'rq:scheduled:', False),
            ('deferred', 'rq:deferred:', False),
            ('canceled', 'rq:canceled:', False),
        ):
            key = prefix + self.name
            self.keys.append(key)
            ids = ['legacy-1', 'legacy-2', 'legacy-3']
            if is_list:
                self.redis.rpush(key, *ids)
            else:
                self.redis.zadd(key, {value: index + 1 for index, value in enumerate(ids)})
            before = self.redis.dump(key)
            with self.subTest(kind=kind):
                first, second = self.inspect(kind), self.inspect(kind, offset=2)
                self.assertEqual(first['entries'], ids[:2])
                self.assertEqual(first['next_offset'], 2)
                self.assertEqual(second['entries'], ids[2:])
                self.assertIsNone(second['next_offset'])
                self.assertEqual(first['observation'], 'NON_SNAPSHOT')
                self.assertEqual(first['disposition'], 'UNKNOWN')
                self.assertEqual(self.redis.dump(key), before)
            self.redis.delete(key)

    def test_unsafe_ids_and_wrong_types_fail_without_changes(self):
        key = 'rq:queue:' + self.name
        self.keys.append(key)
        self.redis.rpush(key, b'legacy\nunsafe')
        before = self.redis.dump(key)
        with self.assertRaises(CommandError):
            self.inspect('queued')
        self.assertEqual(self.redis.dump(key), before)
        self.redis.delete(key)
        self.redis.set(key, 'wrong type')
        before = self.redis.dump(key)
        with self.assertRaises(CommandError):
            self.inspect('queued')
        self.assertEqual(self.redis.dump(key), before)

    def test_concurrent_dequeue_makes_pagination_non_snapshot(self):
        key = 'rq:queue:' + self.name
        self.keys.append(key)
        self.redis.rpush(key, 'a', 'b', 'c')
        first = self.inspect('queued')
        self.redis.lpop(key)  # represents a still-running old consumer
        second = self.inspect('queued', offset=first['next_offset'])
        self.assertEqual(second['entries'], [])
        self.assertEqual(self.redis.lrange(key, 0, -1), [b'b', b'c'])
        self.assertEqual(second['disposition'], 'UNKNOWN')
