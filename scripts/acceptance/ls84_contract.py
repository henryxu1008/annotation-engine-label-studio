"""Offline contracts for the explicitly invoked #84 acceptance support.

No product imports, secret discovery, network, provisioning or settings import.
Execution approval is an operator-supplied record, not an IAM credential.
"""
import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

MAX_JSON = 256 * 1024
MATRIX = 'docs/authorization/issue84-v1-closeout-activation-matrix.json'
DIRECTORIES = ('pgdata', 'redis', 'data', 'exports', 'reports', 'tmp', 'static')
ACTIONS = frozenset({'seed', 'web', 'worker', 'audit-cycle'})
PROVIDER = 'ls84_policy.IsolatedAcceptancePolicy'
IDENTITY = 'access_control.identity.LocalIdentityProvider'
DOMAIN = 'second-review84.example.test'
VERSION = 'synthetic-ls84-acceptance/v1'


class SupportError(ValueError):
    """Messages are fixed codes; never echo secrets, paths or responses."""


def require(ok, code):
    if not ok:
        raise SupportError(code)


def exact(value, keys, code='INVALID_SHAPE'):
    require(isinstance(value, dict) and set(value) == set(keys), code)


def integer(v, minimum=1, maximum=2**63 - 1):
    return type(v) is int and minimum <= v <= maximum


def uuid(v):
    try:
        return isinstance(v, str) and str(UUID(v)) == v
    except (TypeError, ValueError, AttributeError):
        return False


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def hashed(v):
    return sha(json.dumps(v, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode())


def decode(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, 'DUPLICATE_KEY')
            value[key] = item
        return value

    def invalid(_):
        raise SupportError('NONFINITE_JSON')

    require(isinstance(raw, bytes) and len(raw) <= MAX_JSON, 'JSON_TOO_LARGE')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid)
        require(isinstance(value, dict), 'JSON_OBJECT_REQUIRED')
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise SupportError('INVALID_JSON') from None


def is_link(p):
    try:
        s = p.lstat()
        return stat.S_ISLNK(s.st_mode) or bool(getattr(s, 'st_file_attributes', 0) & 0x400)
    except FileNotFoundError:
        return False


def safe_path(raw, *, exists=True, directory=False):
    require(isinstance(raw, (str, Path)), 'INVALID_PATH')
    p = Path(raw)
    require(p.is_absolute() and '..' not in p.parts, 'ABSOLUTE_CANONICAL_PATH_REQUIRED')
    require(not str(p).startswith(('\\\\', '//')), 'LOCAL_PATH_REQUIRED')
    for part in (p, *p.parents):
        require(not is_link(part), 'LINK_OR_REPARSE_POINT')
    require(not exists or (p.is_dir() if directory else p.is_file()), 'PATH_MISSING_OR_WRONG_TYPE')
    return p.resolve()


def read_file(path, maximum=MAX_JSON):
    p = safe_path(path)
    require(p.stat().st_size <= maximum, 'FILE_TOO_LARGE')
    with p.open('rb') as handle:
        raw = handle.read(maximum + 1)
    require(len(raw) <= maximum, 'FILE_TOO_LARGE')
    return raw


def validate_manifest(v, *, workspace, protected):
    exact(v, {'schema', 'run_id', 'workspace', 'source_digest', 'run_root', 'instance_id', 'http_port',
              'postgres', 'redis'})
    require(v['schema'] == 'ls84-isolated-run/v1' and uuid(v['run_id']) and uuid(v['instance_id']), 'INVALID_RUN')
    require(isinstance(v['source_digest'], str) and re.fullmatch('[a-f0-9]{64}', v['source_digest']), 'INVALID_SOURCE')
    w = safe_path(v['workspace'], directory=True)
    require(w == Path(workspace).resolve(), 'WRONG_WORKSPACE')
    root = safe_path(v['run_root'], directory=True)
    for item in [w, *protected]:
        p = Path(item).resolve()
        require(not root.is_relative_to(p) and not p.is_relative_to(root), 'PROTECTED_PATH_OVERLAP')
    for name in DIRECTORIES:
        safe_path(root / name, directory=True)
    require(not (root / 'data/.env').exists(), 'AMBIENT_DOTENV_FORBIDDEN')
    pg, redis = v['postgres'], v['redis']
    exact(pg, {'host', 'port', 'database', 'user', 'system_identifier', 'pid'})
    exact(redis, {'host', 'port', 'run_id', 'pid'})
    require(pg['host'] == redis['host'] == '127.0.0.1', 'LOOPBACK_REQUIRED')
    ports = [v['http_port'], pg['port'], redis['port']]
    require(all(integer(p, 1024, 65535) for p in ports) and len(set(ports)) == 3, 'INVALID_PORTS')
    require(isinstance(pg['database'], str) and re.fullmatch('ui_ci_second_review_ls84_[a-z0-9_]{1,40}', pg['database']),
            'ISOLATED_DATABASE_REQUIRED')
    require(isinstance(pg['user'], str) and re.fullmatch('[a-z][a-z0-9_]{0,62}', pg['user']), 'INVALID_DATABASE_USER')
    require(isinstance(pg['system_identifier'], str) and re.fullmatch('[0-9]{1,24}', pg['system_identifier']),
            'INVALID_CLUSTER_ID')
    require(integer(pg['pid']) and integer(redis['pid']) and pg['pid'] != redis['pid'], 'INVALID_RESOURCE_PIDS')
    require(isinstance(redis['run_id'], str) and re.fullmatch('[a-f0-9]{40}', redis['run_id']), 'INVALID_REDIS_ID')
    return copy.deepcopy(v)


def validate_approval(value, manifest, action, *, now=None):
    exact(value, {'schema', 'run_id', 'source_digest', 'phase', 'owner', 'approved_by', 'authorization_reference',
                  'actions', 'expires_at'})
    require(value['schema'] == 'ls84-execution-authorization/v1'
            and value['run_id'] == manifest['run_id'] and value['source_digest'] == manifest['source_digest']
            and value['phase'] == 'isolated-synthetic', 'AUTHORIZATION_BINDING_MISMATCH')
    require(all(isinstance(value[k], str) and 1 <= len(value[k]) <= 256
                for k in ('owner', 'approved_by', 'authorization_reference')), 'AUTHORIZATION_REFERENCE_REQUIRED')
    actions = value['actions']
    require(isinstance(actions, list) and all(isinstance(a, str) for a in actions)
            and len(set(actions)) == len(actions) and set(actions) <= ACTIONS and action in actions,
            'ACTION_NOT_AUTHORIZED')
    require_live_authorization({'authorization_expires_at': value['expires_at']}, now=now)


def require_live_authorization(receipt, *, now=None):
    try:
        expiry = datetime.fromisoformat(receipt['authorization_expires_at'])
        require(expiry.utcoffset() is not None and expiry > (now or datetime.now(timezone.utc)), 'AUTHORIZATION_EXPIRED')
        return (expiry - (now or datetime.now(timezone.utc))).total_seconds()
    except (KeyError, TypeError, ValueError):
        raise SupportError('AUTHORIZATION_EXPIRED') from None


def validate_observations(m, pg, redis, *, fresh):
    p, r, root = m['postgres'], m['redis'], Path(m['run_root'])
    require(all(pg.get(k) == p[k] for k in ('database', 'user', 'port', 'system_identifier', 'pid'))
            and pg.get('cluster') == str(root / 'pgdata')
            and integer(pg.get('public_tables'), 0), 'POSTGRES_OWNERSHIP_MISMATCH')
    require(all(redis.get(k) == r[k] for k in ('run_id', 'pid', 'port'))
            and redis.get('directory') == str(root / 'redis') and redis.get('cluster_enabled') == 0
            and redis.get('role') == 'master' and isinstance(redis.get('keyspace'), dict), 'REDIS_OWNERSHIP_MISMATCH')
    if fresh:
        require(pg['public_tables'] == 0 and redis['keyspace'] == {}, 'FRESH_RESOURCES_REQUIRED')


def environment(m, secret, password, *, ambient=None):
    # In particular, no PYTHONPATH, proxy, cloud credentials, feature opt-ins or DB defaults survive.
    ambient = os.environ if ambient is None else ambient
    env = {k: ambient[k] for k in ('PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'LANG') if k in ambient}
    root, p = Path(m['run_root']), m['postgres']
    env.update({
        'PYTHONDONTWRITEBYTECODE': '1', 'TEST_ENVIRONMENT': '1', 'DJANGO_DB': 'postgresql',
        'DJANGO_SETTINGS_MODULE': 'core.settings.label_studio', 'POSTGRE_HOST': p['host'],
        'POSTGRE_PORT': str(p['port']), 'POSTGRE_NAME': p['database'], 'POSTGRE_USER': p['user'],
        'POSTGRE_PASSWORD': password, 'SECRET_KEY': secret, 'BASE_DATA_DIR': str(root / 'data'),
        'TEMP': str(root / 'tmp'), 'TMP': str(root / 'tmp'), 'TMPDIR': str(root / 'tmp'),
        'HOME': str(root / 'data'), 'USERPROFILE': str(root / 'data'),
        'APPROVED_EXPORT_ROOT': str(root / 'exports'),
        'APPROVED_EXPORT_REDIS_URL': f'redis://127.0.0.1:{m["redis"]["port"]}/0',
        'ANNOTATION_IDENTITY_PROVIDER': IDENTITY, 'ANNOTATION_PROJECT_OPERATION_PROVIDER': PROVIDER,
        'DEBUG': '0', 'LOG_LEVEL': 'ERROR', 'DISABLE_SENTRY': '1', 'FEATURE_FLAGS_OFFLINE': '1',
        'SENTRY_DSN': '', 'FRONTEND_SENTRY_DSN': '', 'LABEL_STUDIO_ENABLE_LEGACY_API_TOKEN': '1',
        'ENABLE_LOCAL_FILES_STORAGE': '0', 'SYNC_ON_TARGET_STORAGE_CREATION': '0',
    })
    return env


def git(workspace, *args):
    return subprocess.check_output(['git', '--no-optional-locks', *args], cwd=workspace, stderr=subprocess.DEVNULL)


def protected_worktrees(workspace):
    return [Path(line[9:]) for line in git(workspace, 'worktree', 'list', '--porcelain').decode().splitlines()
            if line.startswith('worktree ')]


def source_digest(workspace):
    w = safe_path(workspace, directory=True)
    index = git(w, 'ls-files', '--stage', '-z').decode('utf-8').split('\0')
    paths = {}
    for entry in filter(None, index):
        header, relative = entry.split('\t', 1)
        mode, _, stage = header.split(' ')
        require(stage == '0' and mode in ('100644', '100755', '120000') and relative not in paths,
                'UNMERGED_OR_UNSUPPORTED_SOURCE')
        paths[relative] = mode
    for relative in filter(None, git(w, 'ls-files', '--others', '--exclude-standard', '-z').decode('utf-8').split('\0')):
        require(relative not in paths, 'DUPLICATE_SOURCE')
        paths[relative] = 'untracked'
    try:
        placeholders = git(w, 'config', '--bool', '--get', 'core.symlinks').decode().strip() == 'false'
    except subprocess.CalledProcessError as exc:
        require(exc.returncode == 1, 'SOURCE_CONFIGURATION_UNAVAILABLE')
        placeholders = False
    rows = []
    for relative, mode in sorted(paths.items()):
        require(not Path(relative).is_absolute() and '..' not in Path(relative).parts, 'INVALID_SOURCE_PATH')
        p = w / relative
        for parent in p.parents:
            require(not is_link(parent), 'SOURCE_ANCESTOR_LINK')
            if parent == w:
                break
        info = p.lstat()
        if mode == '120000':
            if stat.S_ISLNK(info.st_mode):
                content = os.fsencode(os.readlink(p))  # Link text only, including directory/dangling links.
            else:
                require(placeholders and stat.S_ISREG(info.st_mode) and not is_link(p), 'SOURCE_LINK_TYPE_CHANGED')
                content = read_file(p, 16 * 1024)  # Git core.symlinks=false placeholder, never a target read.
            rows.append([relative, mode, sha(content)])
        else:
            require(stat.S_ISREG(info.st_mode) and not is_link(p), 'SOURCE_NOT_REGULAR')
            rows.append([relative, mode, sha(p.read_bytes())])
    return hashed({'head': git(w, 'rev-parse', 'HEAD').decode().strip(), 'files': rows})


def result_template(m, matrix):
    cases = matrix['cases']
    require(len(cases) == 72 and len({c['id'] for c in cases}) == 72, 'MATRIX_CHANGED')
    phases = {k: sum(c['acceptance_phase'] == k for c in cases) for k in ('E', 'P', 'O')}
    require(phases == {'E': 69, 'P': 2, 'O': 1}, 'MATRIX_PHASES_CHANGED')
    return {'schema': 'ls84-execution-results/v1', 'run_id': m['run_id'], 'source_digest': m['source_digest'],
            'phase_counts': phases, 'isolated_acceptance': None, 'real_enablement': 'NOT_RUN',
            'cases': [{'id': x['id'], 'acceptance_phase': x['acceptance_phase'],
                       'status': 'NOT_RUN', 'execution_receipt': None} for x in cases]}


def write_new(path, value):
    p = safe_path(path, exists=False)
    safe_path(p.parent, directory=True)
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def policy_matches(project, scope, *, test_environment):
    """Pure predicate shared by the synthetic provider and offline negative tests."""
    return (test_environment is True and isinstance(scope, dict)
            and scope.get('project_id') == project.pk and scope.get('organization_id') == project.organization_id
            and scope.get('creator_id') == project.created_by_id
            and scope.get('title') == project.title
            and scope.get('creator_email') == project.created_by.email
            and project.created_by.email == 'manager@' + DOMAIN
            and project.title in ('Synthetic supervision en-US', 'Synthetic supervision zh-CN'))


def register_seed_scope(run, project, expected_xml, *, test_environment):
    """Called only by the guarded command immediately after a new persisted project."""
    exact(run, {'run_id', 'phase', 'scopes'})
    require(uuid(run['run_id']) and run['phase'] == 'seed' and test_environment is True
            and isinstance(run['scopes'], list) and len(run['scopes']) < 2, 'SEED_PHASE_REQUIRED')
    require(getattr(getattr(project, '_state', None), 'adding', True) is False
            and all(integer(v) for v in (project.pk, project.organization_id, project.created_by_id))
            and project.label_config == expected_xml, 'NEW_STATIC_PROJECT_REQUIRED')
    scope = {'project_id': project.pk, 'organization_id': project.organization_id, 'creator_id': project.created_by_id,
             'creator_email': project.created_by.email, 'title': project.title}
    require(policy_matches(project, scope, test_environment=test_environment), 'SEED_SCOPE_INVALID')
    for old in run['scopes']:
        require(old['project_id'] != scope['project_id'] and old['title'] != scope['title']
                and all(old[k] == scope[k] for k in ('organization_id', 'creator_id', 'creator_email')),
                'SEED_SCOPE_REUSED_OR_CHANGED')
    run['scopes'].append(scope)


def seed_submission_allowed(project, run, *, test_environment):
    return (isinstance(run, dict) and run.get('phase') == 'seed' and uuid(run.get('run_id'))
            and any(policy_matches(project, scope, test_environment=test_environment) for scope in run.get('scopes', [])))


def fixture_settings(value, m, *, building=False):
    """Only exact synthetic fixture fields; never deserialize arbitrary Django settings."""
    exact(value, {'schema', 'run_id', 'source_digest', 'instance_id', 'supervision', 'actors', 'scopes',
                  'confidence', 'pending', 'classification'})
    require(value['schema'] == 'ls84-combined-fixture/v1' and value['run_id'] == m['run_id']
            and value['source_digest'] == m['source_digest'] and value['instance_id'] == m['instance_id'],
            'FIXTURE_RUN_MISMATCH')
    # Reuse the existing closed supervision fixture contract without importing Django.
    previous_path = list(sys.path)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ci'))
        from ui_second_review import validate_fixture

        base = validate_fixture(value['supervision'])
    finally:
        sys.path[:] = previous_path
    require(base['ANNOTATION_SECOND_REVIEW_FOLLOWUP_ENABLED'] is True, 'FOLLOWUP_FIXTURE_REQUIRED')
    require(set(value['actors']) == {'followup', 'auditor', 'producer', 'outsider'}, 'ACTOR_ROSTER_INVALID')
    seen = {u['id'] for u in value['supervision']['users'].values()}
    for name, actor in value['actors'].items():
        exact(actor, {'id', 'email'})
        require(integer(actor['id']) and actor['id'] not in seen
                and actor['email'] == name + '@' + DOMAIN, 'ACTOR_ROSTER_INVALID')
        seen.add(actor['id'])
    scopes = value['scopes']
    require(isinstance(scopes, list) and len(scopes) == 2, 'SYNTHETIC_SCOPE_INVALID')
    projects = value['supervision']['projects']
    require(set(value['pending']) == set(value['classification']) == set(projects), 'COMBINED_PROJECTS_INVALID')
    for locale, project in projects.items():
        matching = [s for s in scopes if isinstance(s, dict) and s.get('project_id') == project['id']]
        require(len(matching) == 1, 'SYNTHETIC_SCOPE_INVALID')
        s = matching[0]
        exact(s, {'project_id', 'organization_id', 'creator_id', 'creator_email', 'title'})
        require(integer(s['organization_id']) and s['creator_id'] == value['supervision']['users']['manager']['id']
                and s['creator_email'] == 'manager@' + DOMAIN and s['title'] == 'Synthetic supervision ' + locale,
                'SYNTHETIC_SCOPE_INVALID')
        require(f'{s["organization_id"]}:{s["project_id"]}' in base['ANNOTATION_REVIEW_BATCH_MODES'],
                'SYNTHETIC_SCOPE_INVALID')
        rows = value['pending'][locale]
        require(isinstance(rows, list) and len(rows) == 4, 'PENDING_FIXTURE_INVALID')
        for row in rows:
            exact(row, {'id', 'assignment_id', 'task_id', 'annotation_id', 'owner', 'kind'})
            require(all(integer(row[k]) for k in ('id', 'assignment_id', 'task_id', 'annotation_id'))
                    and row['owner'] in ('a', 'b') and row['kind'] in ('agreement', 'conflict'), 'PENDING_FIXTURE_INVALID')
        if not building:
            analysis = value['classification'][locale]
            exact(analysis, {'id', 'manifest_hash'})
            require(integer(analysis['id']) and isinstance(analysis['manifest_hash'], str)
                    and re.fullmatch('[0-9a-f]{64}', analysis['manifest_hash']), 'CLASSIFICATION_FIXTURE_INVALID')
    confidence = value['confidence']
    exact(confidence, {'profiles', 'grants'})
    profiles, grants = confidence['profiles'], confidence['grants']
    require(isinstance(profiles, list) and isinstance(grants, list) and len(profiles) == len(grants) == 2,
            'CONFIDENCE_SCOPE_INVALID')
    for scope in scopes:
        ps = [p for p in profiles if p.get('project_id') == scope['project_id']]
        gs = [g for g in grants if g.get('project_id') == scope['project_id']]
        require(len(ps) == len(gs) == 1, 'CONFIDENCE_SCOPE_INVALID')
        p, g = ps[0], gs[0]
        exact(p, {'organization_id', 'project_id', 'descriptor', 'digest', 'fresh'})
        exact(g, {'organization_id', 'project_id', 'principal_id', 'source', 'actor_id', 'producer_namespace', 'profile_ids'})
        require(p['organization_id'] == g['organization_id'] == scope['organization_id'] and p['fresh'] is True,
                'CONFIDENCE_SCOPE_INVALID')
        d = p['descriptor']
        require(isinstance(d, dict) and uuid(d.get('profile_id')) and uuid(d.get('producer_namespace'))
                and d.get('model_id') == 'synthetic-no-model' and d.get('evaluator_id') == 'synthetic-offline',
                'SYNTHETIC_DESCRIPTOR_REQUIRED')
        require(g['actor_id'] == value['actors']['producer']['id']
                and g['principal_id'] == f'label_studio_local:{g["actor_id"]}' and g['source'] == 'label_studio_local'
                and g['producer_namespace'] == d['producer_namespace'] and g['profile_ids'] == [d['profile_id']],
                'CONFIDENCE_GRANT_INVALID')
    modes = base['ANNOTATION_REVIEW_BATCH_MODES']
    base.update({
        'ANNOTATION_PROJECT_OPERATION_PROVIDER': PROVIDER,
        'ANNOTATION_REVIEW_AUDIT_ENABLED': True,
        'ANNOTATION_REVIEW_AUDIT_POLICY': {'instance_id': m['instance_id'], 'projects': {
            key: {'mode': 'fork_review', 'version': VERSION} for key in modes}},
        'ANNOTATION_REVIEW_CONFIDENCE_ENABLED': True,
        'ANNOTATION_REVIEW_CONFIDENCE_INSTANCE_ID': m['instance_id'],
        'ANNOTATION_REVIEW_CONFIDENCE_PROFILES': profiles,
        'ANNOTATION_REVIEW_CONFIDENCE_GRANTS': grants,
        'ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED': True,
        'ANNOTATION_CLASSIFICATION_BATCHES_ENABLED': True,
        'ANNOTATION_CLASSIFICATION_ARBITRATION_ENABLED': True,
        'ANNOTATION_REVIEW_ARBITRATION_HANDOFF_ENABLED': True,
        'ANNOTATION_CLASSIFICATION_DISAGREEMENT_MODES': modes,
        'ANNOTATION_CLASSIFICATION_ARBITRATION_MODES': modes,
        'ANNOTATION_FIXED_PACKAGES_ENABLED': True,
        'LS84_ACCEPTANCE_RUN': {'run_id': m['run_id'], 'phase': 'ready', 'scopes': scopes},
    })
    return base
