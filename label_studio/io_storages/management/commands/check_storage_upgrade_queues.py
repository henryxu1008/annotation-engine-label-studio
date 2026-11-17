"""Read-only RQ inventory for an operator-controlled maintenance window."""
import django_rq
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

# RQ's registry.count and Worker.all perform cleanup as a side effect. Inspect
# the underlying structures instead, including queues absent from rq:queues.
_STRUCTURES = (
    ('rq:queue:*', 'llen'),
    ('rq:wip:*', 'zcard'),
    ('rq:scheduled:*', 'zcard'),
    ('rq:deferred:*', 'zcard'),
    ('rq:failed:*', 'zcard'),
    ('rq:canceled:*', 'zcard'),
    ('rq:workers:*', 'scard'),
)
_MAX_SCAN_PAGES = 10000
_MAX_REPORTED = 50


def _busy_structures(connection):
    """Yield nonempty standard RQ structures without invoking RQ cleanup."""
    workers = connection.scard('rq:workers')
    if workers:
        yield 'rq:workers', workers
    for pattern, count_method in _STRUCTURES:
        cursor = 0
        seen = set()
        for _ in range(_MAX_SCAN_PAGES):
            cursor, keys = connection.scan(cursor=cursor, match=pattern, count=500)
            for key in keys:
                if key in seen:
                    continue
                seen.add(key)
                count = getattr(connection, count_method)(key)
                if count:
                    name = key.decode('utf-8', errors='backslashreplace') if isinstance(key, bytes) else str(key)
                    yield name, count
            if cursor == 0:
                break
        else:
            raise RuntimeError(f'RQ scan exceeded page limit for {pattern}')


class Command(BaseCommand):
    help = 'Check configured RQ brokers for jobs and workers before a maintenance upgrade.'

    def handle(self, *args, **options):
        configured = getattr(settings, 'RQ_QUEUES', {})
        if not configured:
            raise CommandError('No RQ queues are configured; queue inventory is unavailable.')
        busy = []
        try:
            for name in sorted(configured):
                connection = django_rq.get_queue(name).connection
                connection.ping()
                for key, count in _busy_structures(connection):
                    busy.append(f'{name}/{key!r}: {count}')
                    # A partial inventory must never be reported as clear.
                    if len(busy) >= _MAX_REPORTED:
                        raise CommandError('RQ is not quiescent (report limit reached): ' + '; '.join(busy))
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(f'RQ inventory failed: {type(exc).__name__}') from exc
        if busy:
            raise CommandError('RQ is not quiescent: ' + '; '.join(busy))
        self.stdout.write(
            'Configured RQ brokers show no registered workers or outstanding jobs. '
            'This is a point-in-time observation, not producer or process isolation.'
        )
