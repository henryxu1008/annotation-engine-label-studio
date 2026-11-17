"""Bounded, read-only identifiers for manual legacy queue reconciliation."""
import json
import re

import django_rq
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

_KINDS = {
    'queued': ('rq:queue:', 'lrange'),
    'started': ('rq:wip:', 'zrange'),
    'scheduled': ('rq:scheduled:', 'zrange'),
    'deferred': ('rq:deferred:', 'zrange'),
    'failed': ('rq:failed:', 'zrange'),
    'canceled': ('rq:canceled:', 'zrange'),
}
_SAFE_ID = re.compile(r'[A-Za-z0-9_.:@-]{1,256}\Z', re.ASCII)
_SAFE_QUEUE = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z', re.ASCII)


class Command(BaseCommand):
    help = 'Read one page of legacy RQ entry IDs without loading or changing jobs.'

    def add_arguments(self, parser):
        parser.add_argument('--connection', required=True, help='Configured RQ connection name.')
        parser.add_argument('--queue', required=True, help='Exact legacy queue name, including orphan queues.')
        parser.add_argument('--kind', required=True, choices=tuple(_KINDS))
        parser.add_argument('--offset', type=int, default=0)
        parser.add_argument('--limit', type=int, default=25)

    def handle(self, *args, **options):
        configured = getattr(settings, 'RQ_QUEUES', {})
        name, queue, kind = options['connection'], options['queue'], options['kind']
        offset, limit = options['offset'], options['limit']
        if name not in configured or not isinstance(queue, str) or not _SAFE_QUEUE.fullmatch(queue):
            raise CommandError('A configured connection and valid exact queue name are required.')
        if (
            kind not in _KINDS
            or type(offset) is not int
            or not 0 <= offset <= 1000000
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise CommandError('Invalid queue inventory page parameters.')
        prefix, method = _KINDS[kind]
        try:
            redis = django_rq.get_queue(name).connection
            redis.ping()
            # Request one extra entry to find whether another page exists.
            # Read Redis directly: RQ registry access can clean old records,
            # and fetching a Job can deserialize attacker-controlled pickle.
            raw = getattr(redis, method)(prefix + queue, offset, offset + limit)
            if not isinstance(raw, (list, tuple)) or len(raw) > limit + 1:
                raise ValueError('Invalid page')
            entries = []
            for value in raw:
                value = value.decode('ascii') if isinstance(value, bytes) else value
                if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
                    raise ValueError('Unsafe job identifier')
                entries.append(value)
        except Exception as exc:
            raise CommandError(f'RQ queue inspection failed: {type(exc).__name__}') from exc
        has_more = len(entries) > limit
        if has_more and offset + limit > 1000000:
            raise CommandError('RQ queue inspection exceeds the supported offset; inventory remains incomplete.')
        self.stdout.write(
            json.dumps(
                {
                    'connection': name,
                    'queue': queue,
                    'kind': kind,
                    'entries': entries[:limit],
                    'next_offset': offset + limit if has_more else None,
                    'observation': 'NON_SNAPSHOT',
                    'disposition': 'UNKNOWN',
                }
            )
        )
