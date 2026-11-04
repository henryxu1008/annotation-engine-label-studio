"""Offline support contracts. No Django, sockets, database, worker or service."""
import copy
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

SUPPORT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUPPORT))
import ls84_contract as c


def combined(m):
    sys.path.insert(0, str(SUPPORT.parents[1] / 'scripts/ci/tests'))
    from test_second_review_ci import fixture

    supervision = fixture()
    supervision['settings']['ANNOTATION_SECOND_REVIEW_FOLLOWUP_ENABLED'] = True
    actors = {name: {'id': 30 + i, 'email': name + '@' + c.DOMAIN}
              for i, name in enumerate(('followup', 'auditor', 'producer', 'outsider'))}
    value = {'schema': 'ls84-combined-fixture/v1', 'run_id': m['run_id'], 'source_digest': m['source_digest'],
             'instance_id': m['instance_id'], 'supervision': supervision, 'actors': actors, 'scopes': [],
             'confidence': {'profiles': [], 'grants': []}, 'pending': {}, 'classification': {}}
    for locale, p in supervision['projects'].items():
        value['scopes'].append({'project_id': p['id'], 'organization_id': 9, 'creator_id': 1,
                                'creator_email': 'manager@' + c.DOMAIN, 'title': 'Synthetic supervision ' + locale})
        d = {'profile_id': str(uuid4()), 'producer_namespace': str(uuid4()), 'model_id': 'synthetic-no-model',
             'evaluator_id': 'synthetic-offline'}
        value['confidence']['profiles'].append({'organization_id': 9, 'project_id': p['id'], 'descriptor': d,
                                               'digest': 'd' * 64, 'fresh': True})
        value['confidence']['grants'].append({'organization_id': 9, 'project_id': p['id'],
            'principal_id': f'label_studio_local:{actors["producer"]["id"]}', 'source': 'label_studio_local',
            'actor_id': actors['producer']['id'], 'producer_namespace': d['producer_namespace'],
            'profile_ids': [d['profile_id']]})
        value['pending'][locale] = [{'id': 500 + p['id'] * 10 + i, 'assignment_id': 600 + p['id'] * 10 + i,
            'task_id': 700 + p['id'] * 10 + i // 2, 'annotation_id': 800 + p['id'] * 10 + i,
            'owner': ('a', 'b')[i % 2], 'kind': ('agreement', 'conflict')[i // 2]} for i in range(4)]
        value['classification'][locale] = {'id': 900 + p['id'], 'manifest_hash': 'e' * 64}
    return value


def manifest(root):
    return {
        'schema': 'ls84-isolated-run/v1', 'run_id': str(uuid4()), 'workspace': str(SUPPORT.parents[1]),
        'source_digest': 'a' * 64, 'run_root': str(root), 'instance_id': str(uuid4()), 'http_port': 18484,
        'postgres': {'host': '127.0.0.1', 'port': 18485, 'database': 'ui_ci_second_review_ls84_test',
                     'user': 'ls84_test', 'system_identifier': '123456789', 'pid': 41001},
        'redis': {'host': '127.0.0.1', 'port': 18486, 'run_id': 'b' * 40, 'pid': 41002},
    }


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.value = manifest(self.root)
        for name in c.DIRECTORIES:
            (self.root / name).mkdir()

    def check(self, value=None):
        return c.validate_manifest(value or self.value, workspace=SUPPORT.parents[1], protected=[])

    def test_valid_manifest_is_copied_and_has_no_services_side_effect(self):
        before = set(self.root.rglob('*'))
        clean = self.check()
        self.assertEqual(clean, self.value)
        self.assertIsNot(clean, self.value)
        self.assertEqual(before, set(self.root.rglob('*')))
        self.assertNotIn('django', sys.modules)

    def test_json_duplicate_nonfinite_and_oversize_rejected(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'\xef\xbb\xbf{}', b'[]'):
            with self.subTest(raw=raw), self.assertRaises(c.SupportError):
                c.decode(raw)
        with self.assertRaises(c.SupportError):
            c.decode(b' ' * (c.MAX_JSON + 1))

    def test_unknown_or_mutable_settings_are_not_a_startup_interface(self):
        for key in ('settings', 'command', 'provider', 'password', 'redis_url'):
            v = copy.deepcopy(self.value)
            v[key] = 'arbitrary'
            with self.subTest(key=key), self.assertRaises(c.SupportError):
                self.check(v)

    def test_remote_default_db_shared_redis_and_boolean_port_rejected(self):
        for section, key, value in (('postgres', 'host', 'localhost'), ('redis', 'host', '10.0.0.1'),
                                    ('postgres', 'database', 'postgres'), ('redis', 'port', True),
                                    ('postgres', 'pid', 0), ('redis', 'run_id', 'unknown')):
            v = copy.deepcopy(self.value)
            v[section][key] = value
            with self.subTest(section=section, key=key), self.assertRaises(c.SupportError):
                self.check(v)

    def test_ports_must_be_distinct(self):
        self.value['redis']['port'] = self.value['http_port']
        with self.assertRaises(c.SupportError):
            self.check()

    def test_protected_directory_overlap_both_directions_rejected(self):
        for protected in (self.root, self.root.parent, self.root / 'child'):
            with self.subTest(protected=protected), self.assertRaises(c.SupportError):
                c.validate_manifest(self.value, workspace=SUPPORT.parents[1], protected=[protected])

    def test_linked_ancestor_rejected(self):
        with patch.object(c, 'is_link', side_effect=lambda p: p == self.root):
            with self.assertRaises(c.SupportError):
                self.check()

    def test_dotenv_not_read_or_allowed(self):
        p = self.root / 'data/.env'
        p.write_text('SYNTHETIC ONLY', encoding='utf-8')
        with self.assertRaises(c.SupportError):
            self.check()

    def test_approval_is_phase_action_run_source_and_time_bound(self):
        approval = {'schema': 'ls84-execution-authorization/v1', 'run_id': self.value['run_id'],
                    'source_digest': self.value['source_digest'], 'phase': 'isolated-synthetic',
                    'owner': 'synthetic owner', 'approved_by': 'synthetic authority',
                    'authorization_reference': 'synthetic-offline-test', 'actions': ['seed', 'web', 'worker', 'audit-cycle'],
                    'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        c.validate_approval(approval, self.value, 'seed')
        for key, value in (('phase', 'production'), ('run_id', str(uuid4())), ('source_digest', 'c' * 64),
                           ('expires_at', '2000-01-01T00:00:00+00:00'), ('actions', ['web']),
                           ('approved_by', ''), ('actions', ['seed', 'deploy'])):
            v = copy.deepcopy(approval)
            v[key] = value
            with self.subTest(key=key), self.assertRaises(c.SupportError):
                c.validate_approval(v, self.value, 'seed')

    def test_ownership_does_not_accept_empty_namespace_in_shared_server(self):
        pg = {'database': self.value['postgres']['database'], 'user': 'ls84_test', 'port': 18485,
              'cluster': str(self.root / 'pgdata'), 'system_identifier': '123456789', 'pid': 41001,
              'public_tables': 0}
        redis = {'run_id': 'b' * 40, 'pid': 41002, 'port': 18486, 'directory': str(self.root / 'redis'),
                 'keyspace': {}, 'cluster_enabled': 0, 'role': 'master'}
        c.validate_observations(self.value, pg, redis, fresh=True)
        for target, key, value in (('pg', 'system_identifier', 'different'), ('pg', 'public_tables', 1),
                                    ('redis', 'pid', 999), ('redis', 'directory', str(self.root.parent)),
                                    ('redis', 'keyspace', {'db1': {'keys': 1}}), ('redis', 'role', 'slave')):
            p, r = copy.deepcopy(pg), copy.deepcopy(redis)
            (p if target == 'pg' else r)[key] = value
            with self.subTest(target=target, key=key), self.assertRaises(c.SupportError):
                c.validate_observations(self.value, p, r, fresh=True)
        pg['public_tables'] = 32
        redis['keyspace'] = {'db0': {'keys': 5}}
        c.validate_observations(self.value, pg, redis, fresh=False)

    def test_startup_environment_does_not_inherit_cloud_or_provider(self):
        env = c.environment(self.value, 'synthetic-secret', 'synthetic-pg-password', ambient={
            'PATH': 'path', 'AWS_SECRET_ACCESS_KEY': 'do-not-copy', 'ANNOTATION_IDENTITY_PROVIDER': 'evil.Provider',
            'DJANGO_SETTINGS_MODULE': 'evil.settings', 'HTTP_PROXY': 'do-not-copy', 'POSTGRE_HOST': 'remote',
        })
        self.assertEqual(env['POSTGRE_HOST'], '127.0.0.1')
        self.assertEqual(env['SECRET_KEY'], 'synthetic-secret')
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', env)
        self.assertNotIn('HTTP_PROXY', env)
        self.assertEqual(env['ANNOTATION_IDENTITY_PROVIDER'], 'access_control.identity.LocalIdentityProvider')

    def test_matrix_remains_not_run_and_separates_phases(self):
        matrix = json.loads((SUPPORT.parents[1] / c.MATRIX).read_text(encoding='utf-8'))
        result = c.result_template(self.value, matrix)
        self.assertEqual(len(result['cases']), 72)
        self.assertEqual(result['phase_counts'], {'E': 69, 'P': 2, 'O': 1})
        self.assertTrue(all(x['status'] == 'NOT_RUN' for x in result['cases']))
        self.assertIsNone(result['isolated_acceptance'])

    def test_combined_settings_preserve_second_review_frozen_namespace(self):
        v = combined(self.value)
        settings = c.fixture_settings(v, self.value)
        self.assertEqual(settings['ANNOTATION_SECOND_REVIEW_INSTANCE_ID'],
                         v['supervision']['settings']['ANNOTATION_SECOND_REVIEW_INSTANCE_ID'])
        self.assertEqual(settings['ANNOTATION_REVIEW_AUDIT_POLICY']['instance_id'], self.value['instance_id'])
        self.assertEqual(settings['ANNOTATION_PROJECT_OPERATION_PROVIDER'], c.PROVIDER)
        self.assertNotIn('APPROVED_EXPORT_LIMITS', settings)
        self.assertNotIn('django', sys.modules)

    def test_combined_settings_deny_tampered_scope_and_actor_grants(self):
        for mutator in (lambda v: v['scopes'][0].update(creator_id=999),
                        lambda v: v['actors']['producer'].update(id=1),
                        lambda v: v['confidence']['grants'][0].update(actor_id=999),
                        lambda v: v['confidence']['profiles'][0]['descriptor'].update(model_id='real-model'),
                        lambda v: v['scopes'].append(v['scopes'][0]),
                        lambda v: v.update(source_digest='f' * 64)):
            v = combined(self.value)
            mutator(v)
            with self.subTest(mutator=mutator), self.assertRaises(c.SupportError):
                c.fixture_settings(v, self.value)

    def test_policy_predicate_requires_exact_scope_and_test_environment(self):
        s = combined(self.value)['scopes'][0]
        p = SimpleNamespace(pk=s['project_id'], organization_id=s['organization_id'], created_by_id=s['creator_id'],
                            title=s['title'], created_by=SimpleNamespace(email=s['creator_email']))
        self.assertTrue(c.policy_matches(p, s, test_environment=True))
        self.assertFalse(c.policy_matches(p, s, test_environment=False))
        for key in ('project_id', 'organization_id', 'creator_id', 'creator_email', 'title'):
            other = dict(s, **{key: 'wrong'})
            self.assertFalse(c.policy_matches(p, other, test_environment=True))

    def test_fresh_seed_refuses_occupied_data_before_connect_or_django(self):
        import ls84_runtime as runtime

        manifest_file = self.root / 'manifest.json'
        c.write_new(manifest_file, self.value)
        authorization = {'schema': 'ls84-execution-authorization/v1', 'run_id': self.value['run_id'],
            'source_digest': self.value['source_digest'], 'phase': 'isolated-synthetic', 'owner': 'synthetic',
            'approved_by': 'synthetic', 'authorization_reference': 'OFFLINE TEST ONLY', 'actions': ['seed'],
            'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        c.write_new(self.root / 'authorization.json', authorization)
        for name in ('secret.txt', 'postgres-password.txt', 'postgres-ownership.json'):
            (self.root / name).write_text('synthetic', encoding='utf-8')
            (self.root / name).chmod(0o600)
        (self.root / 'data/occupied').write_text('synthetic', encoding='utf-8')
        args = Namespace(manifest=manifest_file, manifest_sha256=c.sha(manifest_file.read_bytes()),
            authorization_sha256=c.sha((self.root / 'authorization.json').read_bytes()), command='seed',
            fixture_sha256=None)
        with patch.object(c, 'source_digest', return_value='a' * 64), self.assertRaisesRegex(c.SupportError, 'FRESH_OUTPUT'):
            runtime.load(args)
        self.assertNotIn('django', sys.modules)

    def test_existing_evidence_file_is_never_overwritten(self):
        p = self.root / 'reports/intent.json'
        c.write_new(p, {'state': 'UNKNOWN'})
        original = p.read_bytes()
        with self.assertRaises(FileExistsError):
            c.write_new(p, {'state': 'new capture'})
        self.assertEqual(p.read_bytes(), original)


class ExecutorTests(unittest.TestCase):
    def test_expired_worker_cannot_dequeue_or_enter_product_job(self):
        from ls84_transport import evidence_worker_class

        class Stop(Exception):
            pass

        class FakeWorker:
            def dequeue_job_and_maintain_ttl(self, *args, **kwargs):
                raise AssertionError('must not dequeue')

            def perform_job(self, *args, **kwargs):
                raise AssertionError('must not execute')

        cls = evidence_worker_class({'run_root': str(SUPPORT)},
                                    {'authorization_expires_at': '2000-01-01T00:00:00+00:00'}, FakeWorker, Stop)
        with self.assertRaises(Stop):
            cls().dequeue_job_and_maintain_ttl(100)
        with self.assertRaisesRegex(c.SupportError, 'AUTHORIZATION_EXPIRED'):
            cls().perform_job(SimpleNamespace(func_name='data_export.review_audit.generate'), None)

    def test_live_worker_wait_is_limited_to_remaining_authority(self):
        from ls84_transport import evidence_worker_class

        calls = []

        class FakeWorker:
            def dequeue_job_and_maintain_ttl(self, timeout, max_idle_time=None):
                calls.append(max_idle_time)
                return 'synthetic-dequeue'

            def perform_job(self, *args, **kwargs):
                return 'original export path'

        expiry = (datetime.now(timezone.utc) + timedelta(seconds=3)).isoformat()
        cls = evidence_worker_class({'run_root': str(SUPPORT)}, {'authorization_expires_at': expiry}, FakeWorker,
                                    RuntimeError)
        self.assertEqual(cls().dequeue_job_and_maintain_ttl(300), 'synthetic-dequeue')
        self.assertGreaterEqual(calls[0], 1)
        self.assertLessEqual(calls[0], 3)
        for name in ('data_export.approved.generate', 'data_export.snapshots.generate'):
            self.assertEqual(cls().perform_job(SimpleNamespace(func_name=name), None), 'original export path')

    def test_expired_web_authority_denies_new_business_before_product(self):
        import ls84_web

        called = []
        with patch.object(ls84_web, 'CONTEXT', {'authorization_expires_at': '2000-01-01T00:00:00+00:00'}), \
                patch.object(ls84_web, 'SECRET', 'synthetic'), \
                patch.object(ls84_web, 'PRODUCT', lambda *args: self.fail('must not enter product')):
            response = b''.join(ls84_web.application({'PATH_INFO': '/api/projects/1/review-audit-packages/',
                'REQUEST_METHOD': 'POST'}, lambda status, headers: called.append(status)))
        self.assertEqual(called, ['503 Service Unavailable'])
        self.assertIn(b'AUTHORIZATION_EXPIRED', response)
        self.assertNotIn('django', sys.modules)

    def test_expiry_checks_apply_to_executor_request_admission(self):
        from ls84_transport import HTTP

        client = object.__new__(HTTP)
        client.web_receipt = {'authorization_expires_at': '2000-01-01T00:00:00+00:00'}
        client.deadline = float('inf')
        client.session = SimpleNamespace(get=lambda *args, **kwargs: self.fail('must not send'))
        with self.assertRaisesRegex(c.SupportError, 'AUTHORIZATION_EXPIRED'):
            client.response('POST', '/api/projects/1/review-audit-packages/')

    def test_expiry_is_aware_and_has_remaining_seconds(self):
        now = datetime(2026, 11, 4, tzinfo=timezone.utc)
        receipt = {'authorization_expires_at': '2026-11-04T00:00:03+00:00'}
        self.assertEqual(c.require_live_authorization(receipt, now=now), 3)
        with self.assertRaises(c.SupportError):
            c.require_live_authorization(receipt, now=now + timedelta(seconds=3))
        with self.assertRaises(c.SupportError):
            c.require_live_authorization({'authorization_expires_at': '2026-11-04T00:00:03'}, now=now)

    def test_private_binding_rejects_secret_runtime_or_manifest_drift(self):
        from ls84_runtime import check_binding

        m = {'run_id': str(uuid4()), 'source_digest': 'a' * 64}
        credentials = {'secret.txt': 'b' * 64, 'postgres-password.txt': 'c' * 64}
        v = {'schema': 'ls84-private-configuration/v1', **m, 'manifest_sha256': 'd' * 64,
             'credentials': credentials, 'runtime_sha256': 'e' * 64}
        check_binding(v, m, 'd' * 64, credentials, runtime_hash='e' * 64)
        for credentials_hash, runtime, manifest_hash in (({'secret.txt': 'changed'}, 'e' * 64, 'd' * 64),
                                                       (credentials, 'changed', 'd' * 64),
                                                       (credentials, 'e' * 64, 'changed')):
            with self.assertRaises(c.SupportError):
                check_binding(v, m, manifest_hash, credentials_hash, runtime_hash=runtime)

    def test_attestation_serves_pinned_process_without_product_import(self):
        import ls84_web

        receipt = {'run_id': str(uuid4()), 'pid': 41012, 'manifest_sha256': 'a' * 64, 'fixture_sha256': 'b' * 64,
                   'source_digest': 'c' * 64, 'configuration_sha256': 'd' * 64,
                   'authorization_sha256': 'e' * 64,
                   'authorization_expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        nonce = str(uuid4())
        ls84_web.configure(receipt, 'synthetic-offline-secret')
        statuses = []
        response = b''.join(ls84_web.application({'PATH_INFO': ls84_web.ATTESTATION_PATH, 'REQUEST_METHOD': 'GET',
                    'QUERY_STRING': 'challenge=' + nonce}, lambda status, headers: statuses.append(status)))
        value = c.decode(response)
        signature = value.pop('proof')
        self.assertEqual(value['challenge'], nonce)
        self.assertEqual(value['pid'], receipt['pid'])
        self.assertEqual(signature, ls84_web.proof('synthetic-offline-secret', value))
        self.assertNotEqual(signature, ls84_web.proof('different-secret', value))
        self.assertEqual(statuses, ['200 OK'])
        self.assertNotIn('django', sys.modules)

    def test_http_budget_prevents_sending_to_unattested_listener(self):
        from ls84_transport import HTTP

        client = object.__new__(HTTP)
        client.web_receipt = {'authorization_expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        client.deadline = 1
        client.session = SimpleNamespace(get=lambda *args, **kwargs: self.fail('must not send'))
        with patch('ls84_transport.time.monotonic', return_value=2), self.assertRaises(c.SupportError):
            client.response('GET', '/api/projects/1/')

    def test_missing_execution_authority_exits_without_runtime_import(self):
        import contextlib
        import io

        import ls84

        # No credentials, manifest, product imports or network before this denial.
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(ls84.main(['audit-cycle']), 2)
        self.assertIn('STOPPED', output.getvalue())
        self.assertNotIn('django', sys.modules)

    def test_transport_chain_requires_exact_job_token_and_separate_worker(self):
        from ls84_transport import verify_chain

        m = {'run_id': str(uuid4())}
        package = SimpleNamespace(pk=uuid4(), token=uuid4())
        receipt = {k: 'synthetic' for k in ('source_digest', 'manifest_sha256', 'fixture_sha256', 'instance_id',
                  'postgres_system_identifier', 'redis_run_id', 'queue', 'configuration_sha256',
                  'authorization_sha256', 'authorization_expires_at')}
        receipt['run_id'] = m['run_id']
        processes = {'web': {'pid': 41010}, 'worker': {'pid': 41011}}
        job = SimpleNamespace(id=f'audit-{package.pk}-{package.token}', func_name='data_export.review_audit.generate',
            args=[str(package.pk), str(package.token)], worker_name='ls84-' + m['run_id'],
            get_status=lambda **kwargs: 'finished')
        evidence = dict(receipt, job_id=job.id, package_id=str(package.pk), attempt_token=str(package.token),
                        worker_name=job.worker_name, worker_pid=41011, workhorse_pid=41012)
        verify_chain(m, package, receipt, processes, job=job, evidence=evidence)
        for key, value in (('worker_pid', 41010), ('workhorse_pid', 41011), ('attempt_token', str(uuid4())),
                           ('fixture_sha256', 'another input'), ('redis_run_id', 'another server')):
            v = dict(evidence, **{key: value})
            with self.subTest(key=key), self.assertRaises(c.SupportError):
                verify_chain(m, package, receipt, processes, job=job, evidence=v)
        job.get_status = lambda **kwargs: 'queued'
        with self.assertRaises(c.SupportError):
            verify_chain(m, package, receipt, processes, job=job, evidence=evidence)

    def test_import_support_does_not_import_django_or_connect(self):
        import subprocess

        code = "import sys; sys.path.insert(0, sys.argv[1]); import ls84,ls84_runtime,ls84_fixture,ls84_policy,ls84_transport; assert 'django' not in sys.modules; assert 'psycopg' not in sys.modules; assert 'redis' not in sys.modules"
        result = subprocess.run([sys.executable, '-B', '-S', '-c', code, str(SUPPORT)], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))


if __name__ == '__main__':
    unittest.main()
