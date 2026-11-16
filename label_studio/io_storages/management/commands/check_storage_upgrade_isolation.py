"""Scoped, read-only observation of retired PostgreSQL and Redis identities."""
import django_rq
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = 'Check one retired PostgreSQL role and Redis user for login and active sessions.'

    def add_arguments(self, parser):
        parser.add_argument('--legacy-db-role', required=True)
        parser.add_argument('--legacy-redis-user', required=True)

    def handle(self, *args, **options):
        role = options['legacy_db_role']
        redis_user = options['legacy_redis_user']
        if not role or not redis_user or connection.vendor != 'postgresql':
            raise CommandError('A PostgreSQL connection and non-empty legacy identities are required.')
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT rolsuper OR pg_has_role(current_user, 'pg_read_all_stats', 'MEMBER') "
                    'FROM pg_roles WHERE rolname = current_user'
                )
                if not cursor.fetchone()[0]:
                    raise CommandError('Database session inventory requires pg_read_all_stats or superuser.')
                cursor.execute('SELECT oid, rolcanlogin FROM pg_roles WHERE rolname = %s', [role])
                target = cursor.fetchone()
                if target is None:
                    raise CommandError('The specified legacy database role does not exist.')
                if target[1]:
                    raise CommandError('The specified legacy database role can still log in.')
                cursor.execute(
                    'SELECT count(*) FROM pg_roles WHERE rolcanlogin AND NOT rolsuper '
                    "AND pg_has_role(oid, %s, 'MEMBER')",
                    [target[0]],
                )
                if cursor.fetchone()[0]:
                    raise CommandError('A login role still inherits the specified legacy database role.')
                cursor.execute('SELECT count(*) FROM pg_stat_activity WHERE usename = %s', [role])
                if cursor.fetchone()[0]:
                    raise CommandError('The specified legacy database role still has active sessions.')
                # pg_stat_activity.usename is the authenticated identity, not
                # a reliably observable current SET ROLE. An existing session
                # may retain a role after membership is revoked. Require a
                # maintenance window with no other application-db clients.
                cursor.execute(
                    'SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() '
                    "AND backend_type = 'client backend' AND pid <> pg_backend_pid()"
                )
                if cursor.fetchone()[0]:
                    raise CommandError(
                        'Other database client sessions remain; their effective roles cannot be verified.'
                    )
                # A disconnected session can leave a transaction that is
                # committed later, even after all client sessions have gone.
                cursor.execute('SELECT count(*) FROM pg_prepared_xacts WHERE database = current_database()')
                if cursor.fetchone()[0]:
                    raise CommandError('Prepared database transactions remain in the application database.')
            queues = getattr(settings, 'RQ_QUEUES', {})
            if not queues:
                raise CommandError('No Redis queues are configured.')
            for name in sorted(queues):
                redis = django_rq.get_queue(name).connection
                user = redis.acl_getuser(redis_user)
                if user is None:
                    raise CommandError(f'The specified legacy Redis user is absent on queue {name}.')
                if user.get('enabled') is not False:
                    raise CommandError(f'The specified legacy Redis user is enabled on queue {name}.')
                clients = redis.client_list()
                if any('user' not in item for item in clients):
                    raise CommandError(f'Redis client identity inventory is incomplete on queue {name}.')
                if any(item['user'] == redis_user for item in clients):
                    raise CommandError(f'The specified legacy Redis user still has clients on queue {name}.')
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(f'Isolation inventory failed: {type(exc).__name__}') from exc
        self.stdout.write(
            'Specified identities have no login or active sessions in the observed services; '
            'no other client sessions or prepared transactions were observed in the application database. '
            'This point-in-time check does not prevent reconnects, establish cloud credential revocation '
            'or cover other identities.'
        )
