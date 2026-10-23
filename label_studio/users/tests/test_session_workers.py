import json
import os
import subprocess
import sys

import pytest


def run_worker(env, payload):
    completed = subprocess.run(
        [sys.executable, '-m', 'users.tests.session_worker'],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.fixture(scope='module')
def workers(tmp_path_factory):
    data_dir = str(tmp_path_factory.mktemp('session-workers'))
    env = {
        **os.environ,
        'BASE_DATA_DIR': data_dir,
        'DATABASE_NAME': os.path.join(data_dir, 'sessions.sqlite3'),
        'DEBUG': 'true',
        'TEST_ENVIRONMENT': '1',
        'DJANGO_DB': 'sqlite',
        # Independently started workers must share the same synthetic signer;
        # never inherit a randomly generated/expanded key from the pytest parent.
        'SECRET_KEY': 'session-workers-synthetic-signing-key-only',
    }
    assert run_worker(env, {'action': 'init'})['initialized']
    return lambda action, **kwargs: run_worker(env, {'action': action, **kwargs})


def test_logout_in_one_process_denies_exact_cookie_replay_in_another(workers):
    login = workers('login')
    assert login['status'] == 302
    cookie = login['cookie']
    before = workers('whoami', cookie=cookie)['status']
    logout = workers('logout', cookie=cookie)['status']
    whoami = workers('whoami', cookie=cookie)['status']
    projects = workers('projects', cookie=cookie)['status']
    assert (before, logout, whoami, projects) == (200, 302, 401, 401)


def test_global_revocation_is_shared_by_independent_processes(workers):
    first, second = workers('login'), workers('login')
    assert first['status'] == second['status'] == 302
    assert workers('revoke')['revoked']
    for login in (first, second):
        status = workers('whoami', cookie=login['cookie'])['status']
        assert status == 401
    new_login = workers('login')
    assert new_login['status'] == 302
    status = workers('whoami', cookie=new_login['cookie'])['status']
    assert status == 200


@pytest.mark.parametrize('debug', [None, 'false', 'true'])
@pytest.mark.parametrize(
    'host,secure',
    [(None, False), ('http://labels.example.test/ls-root', False), ('https://labels.example.test/ls-root', True)],
)
def test_session_settings_follow_public_host_scheme_with_hardened_cookies(tmp_path, debug, host, secure):
    env = {**os.environ, 'BASE_DATA_DIR': str(tmp_path), 'DEBUG': debug}
    for prefix in ('', 'LABEL_STUDIO_', 'HEARTEX_'):
        for name in ('HOST', 'DEBUG', 'SESSION_COOKIE_SECURE'):
            env.pop(f'{prefix}{name}', None)
    if debug is not None:
        env['DEBUG'] = debug
    if host is not None:
        env['LABEL_STUDIO_HOST'] = host
    result = run_worker(env, {'action': 'settings'})
    assert result == {
        'engine': 'django.contrib.sessions.backends.db',
        'secure': secure,
        'httponly': True,
        'samesite': 'Lax',
    }


@pytest.mark.parametrize('value,secure', [('false', False), ('true', True), ('0', False), ('1', True)])
def test_session_settings_accept_explicit_http_and_https_cookie_policy(tmp_path, value, secure):
    env = {
        **os.environ,
        'BASE_DATA_DIR': str(tmp_path),
        'DEBUG': 'false',
        'LABEL_STUDIO_HOST': 'http://labels.example.test' if secure else 'https://labels.example.test',
        'LABEL_STUDIO_SESSION_COOKIE_SECURE': value,
    }
    result = run_worker(env, {'action': 'settings'})
    assert result == {
        'engine': 'django.contrib.sessions.backends.db',
        'secure': secure,
        'httponly': True,
        'samesite': 'Lax',
    }


@pytest.mark.parametrize(
    'engine,debug',
    [
        ('django.contrib.sessions.backends.signed_cookies', 'false'),
        ('django.contrib.sessions.backends.signed_cookies', 'true'),
        ('django.contrib.sessions.backends.cached_db', 'false'),
        ('django.contrib.sessions.backends.cache', 'false'),
        ('django.contrib.sessions.backends.file', 'false'),
        ('custom.unreviewed_backend', 'false'),
    ],
)
def test_unsupported_session_engine_fails_at_startup(tmp_path, engine, debug):
    env = {**os.environ, 'BASE_DATA_DIR': str(tmp_path), 'SESSION_ENGINE': engine, 'DEBUG': debug}
    completed = subprocess.run(
        [sys.executable, '-m', 'users.tests.session_worker'],
        input=json.dumps({'action': 'settings'}),
        text=True,
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert completed.returncode != 0
    assert 'ImproperlyConfigured' in completed.stderr
