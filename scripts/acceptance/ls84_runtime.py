"""Lazy native execution adapter. No import-time Django, connections or services."""
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import ls84_contract as c

ROOT = Path(__file__).resolve().parents[2]


def load(args):
    if args.command == 'worker':
        c.require(os.name == 'posix', 'NATIVE_POSIX_WORKER_REQUIRED')
    raw = c.read_file(args.manifest)
    c.require(c.sha(raw) == args.manifest_sha256, 'MANIFEST_DIGEST_MISMATCH')
    m = c.validate_manifest(c.decode(raw), workspace=ROOT, protected=c.protected_worktrees(ROOT))
    root = Path(m['run_root'])
    authorization_raw = c.read_file(root / 'authorization.json')
    c.require(c.sha(authorization_raw) == args.authorization_sha256, 'AUTHORIZATION_DIGEST_MISMATCH')
    c.validate_approval(c.decode(authorization_raw), m, args.command)
    c.require(c.source_digest(ROOT) == m['source_digest'], 'SOURCE_CHANGED')
    # The operator must pre-create private credentials in this exclusively owned run.
    # No discovery, generated fallback, ambient credentials or outside secret references.
    for name in ('secret.txt', 'postgres-password.txt', 'postgres-ownership.json'):
        c.safe_path(root / name)
    if os.name != 'nt':
        c.require(root.stat().st_mode & 0o077 == 0, 'PRIVATE_ROOT_PERMISSIONS_REQUIRED')
        for name in ('secret.txt', 'postgres-password.txt'):
            c.require((root / name).stat().st_mode & 0o077 == 0, 'PRIVATE_SECRET_PERMISSIONS_REQUIRED')
    if args.command == 'seed':
        c.require(not args.fixture_sha256, 'SEED_HAS_NO_FIXTURE_INPUT')
        c.require(not any((root / 'reports').iterdir()) and not any((root / 'exports').iterdir())
                  and not any((root / 'data').iterdir()), 'FRESH_OUTPUT_REQUIRED')
        value = None
    else:
        c.require(isinstance(args.fixture_sha256, str) and len(args.fixture_sha256) == 64,
                  'FIXTURE_DIGEST_REQUIRED')
        fixture_raw = c.read_file(root / 'reports/fixture-private.json')
        c.require(c.sha(fixture_raw) == args.fixture_sha256, 'FIXTURE_CHANGED')
        value = c.decode(fixture_raw)
        c.fixture_settings(value, m)
    return m, value


def credentials(m):
    root = Path(m['run_root'])
    try:
        secret = c.read_file(root / 'secret.txt', 256).decode('utf-8').strip()
        password = c.read_file(root / 'postgres-password.txt', 256).decode('utf-8').strip()
        c.require(len(secret) >= 32 and password and '\n' not in secret + password and '\r' not in secret + password,
                  'INVALID_PRIVATE_CREDENTIALS')
        return secret, password
    except UnicodeError:
        raise c.SupportError('INVALID_PRIVATE_CREDENTIALS') from None


def credential_binding(m):
    root = Path(m['run_root'])
    return {name: c.sha(c.read_file(root / name, 256)) for name in ('secret.txt', 'postgres-password.txt')}


def check_binding(value, m, manifest_hash, credentials_hash, *, runtime_hash=None):
    c.exact(value, {'schema', 'run_id', 'source_digest', 'manifest_sha256', 'credentials', 'runtime_sha256'})
    c.require(value['schema'] == 'ls84-private-configuration/v1' and value['run_id'] == m['run_id']
              and value['source_digest'] == m['source_digest'] and value['manifest_sha256'] == manifest_hash
              and value['credentials'] == credentials_hash, 'PRIVATE_CONFIGURATION_CHANGED')
    if runtime_hash is not None:
        c.require(value['runtime_sha256'] == runtime_hash, 'FORMATTER_RUNTIME_CHANGED')


def probe(m, password, *, fresh):
    """Read actual native identities before any product import/migration/write."""
    import psycopg
    from redis import Redis

    p, r, root = m['postgres'], m['redis'], Path(m['run_root'])
    # postmaster.pid is part of this exact private cluster, not a declared filename elsewhere.
    pid = c.read_file(root / 'pgdata/postmaster.pid', 4096).decode('ascii').splitlines()[0]
    c.require(pid == str(p['pid']), 'POSTGRES_PROCESS_MISMATCH')
    with psycopg.connect(host='127.0.0.1', port=p['port'], dbname=p['database'], user=p['user'], password=password,
                        connect_timeout=3, options='-c default_transaction_read_only=on -c statement_timeout=3000') as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user, current_setting('data_directory'), "
                           "current_setting('port'), system_identifier FROM pg_control_system()")
            database, user, directory, port, identifier = cursor.fetchone()
            cursor.execute("SELECT count(*) FROM information_schema.tables "
                           "WHERE table_schema NOT IN ('pg_catalog','information_schema')")
            count = cursor.fetchone()[0]
    pg = {'database': database, 'user': user, 'port': int(port), 'cluster': str(Path(directory).resolve()),
          'system_identifier': str(identifier), 'pid': int(pid), 'public_tables': count}
    c.safe_path(directory, directory=True)
    with Redis(host='127.0.0.1', port=r['port'], db=0, socket_connect_timeout=3, socket_timeout=3,
               decode_responses=True) as redis:
        info = redis.info()
        config = redis.config_get('dir')
        ports = redis.config_get('port')
        observation = {'run_id': info['run_id'], 'pid': info['process_id'], 'port': int(ports['port']),
                       'directory': str(Path(config['dir']).resolve()),
                       'keyspace': redis.info('keyspace'), 'role': info['role'],
                       'cluster_enabled': info.get('cluster_enabled')}
        c.safe_path(config['dir'], directory=True)
    c.validate_observations(m, pg, observation, fresh=fresh)
    proof = c.decode(c.read_file(root / 'postgres-ownership.json', 4096))
    c.exact(proof, {'schema', 'database', 'port', 'cluster', 'system_identifier', 'fresh_public_tables'})
    c.require(c.integer(proof['fresh_public_tables'], 0, 0)
              and proof == {'schema': 'second-review-owned-native/v1', 'database': p['database'], 'port': p['port'],
                       'cluster': str(root / 'pgdata'), 'system_identifier': p['system_identifier'],
                       'fresh_public_tables': 0}, 'BOOTSTRAP_PROOF_MISMATCH')
    return {'postgres': pg, 'redis': observation}


@contextmanager
def configured(m, value, *, secret, password):
    """Same entrypoint and pinned input in web, worker, seed and audit executor."""
    c.require('django' not in sys.modules, 'PRODUCT_ALREADY_IMPORTED')
    old_env = dict(os.environ)
    old_path = list(sys.path)
    os.environ.clear()
    os.environ.update(c.environment(m, secret, password))
    os.environ['SECOND_REVIEW_E2E_OWNERSHIP_FILE'] = str(Path(m['run_root']) / 'postgres-ownership.json')
    sys.path[:0] = [str(ROOT / 'label_studio'), str(ROOT / 'scripts/ci')]
    try:
        import django
        from django.test import override_settings

        # Configure before app ready() callbacks, including the RQ child process.
        settings = c.fixture_settings(value, m) if value else {}
        settings.update({
            'ANNOTATION_IDENTITY_PROVIDER': c.IDENTITY, 'ANNOTATION_PROJECT_OPERATION_PROVIDER': c.PROVIDER,
            'LS84_ACCEPTANCE_RUN': {'run_id': m['run_id'], 'scopes': []} if value is None else settings['LS84_ACCEPTANCE_RUN'],
            'APPROVED_EXPORT_QUEUE': 'approved_exports', 'APPROVED_EXPORT_ROOT': str(Path(m['run_root']) / 'exports'),
            'RQ_QUEUES': {'approved_exports': {'URL': f'redis://127.0.0.1:{m["redis"]["port"]}/0',
                                               'DEFAULT_TIMEOUT': 1800}},
            'STATIC_ROOT': str(Path(m['run_root']) / 'static'), 'ALLOWED_HOSTS': ['127.0.0.1', 'localhost'],
            'WSGI_APPLICATION': 'ls84_web.application',
            'SENTRY_DSN': None, 'FRONTEND_SENTRY_DSN': None,
        })
        with override_settings(**settings):
            django.setup()
            if value:
                validate_database(m, value)
            yield
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        sys.path[:] = old_path


def validate_database(m, value):
    from django.conf import settings
    from projects.models import Project, ProjectMember
    from review_batches import confidence_contract
    from tasks.models import Submission
    from ui_second_review import validate_database as validate_supervision
    from users.models import User

    validate_supervision(value['supervision'])
    c.require(settings.TEST_ENVIRONMENT is True, 'SYNTHETIC_ONLY')
    for scope in value['scopes']:
        project = Project.objects.select_related('created_by').get(pk=scope['project_id'])
        c.require(c.policy_matches(project, scope, test_environment=settings.TEST_ENVIRONMENT), 'DATABASE_SCOPE_CHANGED')
        for name, actor in value['actors'].items():
            c.require(User.objects.filter(pk=actor['id'], email=actor['email'], is_active=True,
                                          active_organization_id=scope['organization_id']).exists(), 'ACTOR_CHANGED')
            member = ProjectMember.objects.filter(project=project, user_id=actor['id'], enabled=True)
            c.require(not member.exists() if name == 'outsider' else member.filter(role='manager').exists(),
                      'ACTOR_MEMBERSHIP_CHANGED')
    for profile in value['confidence']['profiles']:
        c.require(confidence_contract.descriptor(profile['descriptor']) == profile['descriptor']
                  and confidence_contract.digest(profile['descriptor']) == profile['digest'], 'PROFILE_CHANGED')
    for locale, rows in value['pending'].items():
        project_id = value['supervision']['projects'][locale]['id']
        for row in rows:
            c.require(Submission.objects.filter(pk=row['id'], assignment_id=row['assignment_id'],
                      annotation_id=row['annotation_id'], assignment__project_id=project_id,
                      submitted_by_id=value['supervision']['users'][row['owner']]['id'], revision=1).exists(),
                      'PENDING_SOURCE_CHANGED')


def process_receipt(m, command, observations, manifest_hash, fixture_hash, binding_hash, approval, approval_hash):
    return {'schema': 'ls84-process/v1', 'run_id': m['run_id'], 'source_digest': m['source_digest'],
            'manifest_sha256': manifest_hash, 'fixture_sha256': fixture_hash, 'instance_id': m['instance_id'],
            'command': command, 'pid': os.getpid(), 'queue': 'approved_exports',
            'configuration_sha256': binding_hash,
            'authorization_sha256': approval_hash, 'authorization_expires_at': approval['expires_at'],
            'postgres_system_identifier': observations['postgres']['system_identifier'],
            'redis_run_id': observations['redis']['run_id']}


def execute(args):
    m, value = load(args)
    root = Path(m['run_root'])
    approval_raw = c.read_file(root / 'authorization.json')
    c.require(c.sha(approval_raw) == args.authorization_sha256, 'AUTHORIZATION_CHANGED')
    approval = c.decode(approval_raw)
    c.validate_approval(approval, m, args.command)
    credential_hash = credential_binding(m)
    binding_path = root / 'reports/configuration-private.json'
    if args.command != 'seed':
        check_binding(c.decode(c.read_file(binding_path)), m, args.manifest_sha256, credential_hash)
    secret, password = credentials(m)
    observations = probe(m, password, fresh=args.command == 'seed')
    if args.command == 'seed':
        # Exclusive intent survives uncertainty and prevents concurrent migrate/seed.
        c.write_new(root / 'reports/seed-intent.json', {'run_id': m['run_id'], 'source_digest': m['source_digest'],
                                                      'state': 'UNKNOWN_UNTIL_COMMITTED'})
    with configured(m, value, secret=secret, password=password):
        from data_export.audit_contract import formatter
        from django.core.management import call_command

        runtime_hash = c.hashed(formatter())
        if args.command == 'seed':
            c.write_new(binding_path, {'schema': 'ls84-private-configuration/v1', 'run_id': m['run_id'],
                'source_digest': m['source_digest'], 'manifest_sha256': args.manifest_sha256,
                'credentials': credential_hash, 'runtime_sha256': runtime_hash})
        else:
            check_binding(c.decode(c.read_file(binding_path)), m, args.manifest_sha256,
                          credential_binding(m), runtime_hash=runtime_hash)
        receipt = process_receipt(m, args.command, observations, args.manifest_sha256, args.fixture_sha256,
                                  c.sha(c.read_file(binding_path)), approval, args.authorization_sha256)
        c.require_live_authorization(receipt)
        c.write_new(root / 'reports' / f'{args.command}-process-{os.getpid()}-{uuid4()}.json', receipt)
        if args.command == 'seed':
            call_command('migrate', interactive=False, verbosity=0)
            c.require_live_authorization(receipt)
            from ls84_fixture import seed

            seed(m)
            c.write_new(root / 'reports/results.json', c.result_template(m, c.decode(c.read_file(ROOT / c.MATRIX))))
        elif args.command == 'web':
            from ls84_web import configure

            configure(receipt, secret)
            call_command('runserver', f'127.0.0.1:{m["http_port"]}', use_reloader=False)
        elif args.command == 'worker':
            # Native RQ fork worker preserves job timeout and process isolation. No SimpleWorker fallback.
            c.require(os.name == 'posix', 'NATIVE_POSIX_WORKER_REQUIRED')
            from ls84_transport import work

            work(m, receipt)
        elif args.command == 'audit-cycle':
            from ls84_transport import audit_cycle

            audit_cycle(m, value, receipt)
