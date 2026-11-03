"""The budget split must preserve every database-specific product selector."""
import collections
import hashlib
import json
import shlex
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts/ci'))
from authorization_gate import PRODUCERS


def pytest_selectors(document):
    rows = []
    for job in document['jobs'].values():
        for step in job['steps']:
            pending = ''
            for line in step.get('run', '').splitlines():
                line = line.strip()
                pending += line[:-1] + ' ' if line.endswith('\\') else line
                if line.endswith('\\'):
                    continue
                argv, pending = shlex.split(pending), ''
                if argv[:3] != ['poetry', 'run', 'pytest']:
                    continue
                options, paths, i = [], [], 3
                while i < len(argv):
                    value = argv[i]
                    if value in ('-m', '-k'):
                        options.extend((value, argv[i + 1]))
                        i += 2
                    elif value.startswith('-'):
                        options.append(value)
                        i += 1
                    else:
                        paths.append(value)
                        i += 1
                database = step.get('env', {}).get('DJANGO_DB', document['env']['DJANGO_DB'])
                if step.get('working-directory') != 'label_studio':
                    raise ValueError('Wrong pytest source directory')
                if step.get('if') or step.get('continue-on-error'):
                    raise ValueError('Product tests cannot be skipped or hidden')
                rows.extend(json.dumps([database, path, options]) for path in paths)
    return collections.Counter(rows)


class AuthorizationWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.load((ROOT / '.github/workflows/fork-pr-gate.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
        cls.baseline = json.loads((Path(__file__).parent / 'fixtures/authorization_inventory.json').read_text(encoding='utf-8'))

    def test_all_original_selectors_markers_and_verbosity_preserved_once_per_database(self):
        wanted = collections.Counter(json.dumps(v) for v in self.baseline['selectors'])
        self.assertEqual(pytest_selectors(self.workflow), wanted)

    def test_events_permissions_concurrency_and_shared_runtime_remain_exact(self):
        for key in ('name', 'on', 'permissions', 'concurrency', 'env'):
            self.assertEqual(self.workflow[key], self.baseline['workflow_contract'][key], key)
        self.assertEqual(set(self.workflow['jobs']), set(PRODUCERS) | {'authorization-tests'})

    def test_each_explicit_producer_has_original_service_runtime_and_25_minute_limit(self):
        for key in PRODUCERS:
            with self.subTest(job=key):
                job = self.workflow['jobs'][key]
                self.assertEqual(job['runs-on'], 'ubuntu-latest')
                self.assertEqual(job['timeout-minutes'], '25')
                for forbidden in ('if', 'continue-on-error', 'strategy', 'permissions', 'needs'):
                    self.assertNotIn(forbidden, job)
                service = hashlib.sha256(json.dumps(job['services'], sort_keys=True).encode()).hexdigest()
                self.assertEqual(service, self.baseline['workflow_contract']['service_sha256'])
                checkout = [s for s in job['steps'] if s.get('uses') == 'actions/checkout@v6']
                python = [s for s in job['steps'] if s.get('uses') == 'actions/setup-python@v6']
                self.assertEqual(len(checkout), 1)
                self.assertEqual(len(python), 1)
                self.assertEqual(python[0]['with'], {'python-version': '3.11', 'cache': 'poetry'})
                for step in job['steps']:
                    self.assertNotIn('continue-on-error', step)

    def test_unique_required_name_always_requires_exact_three_producers(self):
        job = self.workflow['jobs']['authorization-tests']
        self.assertEqual(job['name'], 'Authorization foundation tests')
        self.assertEqual(sum(j['name'] == job['name'] for j in self.workflow['jobs'].values()), 1)
        self.assertEqual(set(job['needs']), PRODUCERS)
        self.assertEqual(len(job['needs']), 3)
        self.assertEqual(job['if'].replace(' ', ''), '${{always()}}')
        self.assertEqual(job['timeout-minutes'], '5')
        step = job['steps'][-1]
        self.assertEqual(step['run'], 'python scripts/ci/authorization_gate.py')
        self.assertEqual(step['env'], {'AUTHORIZATION_NEEDS_JSON': '${{ toJSON(needs) }}'})
        for forbidden in ('continue-on-error', 'permissions', 'strategy'):
            self.assertNotIn(forbidden, job)

    def test_sqlite_retains_all_preconditions_and_postgres_independent_cleanup_checks(self):
        steps = {s.get('name'): s for s in self.workflow['jobs']['authorization-sqlite']['steps']}
        for name in ('Verify isolated candidate build and archive contracts', 'Check migrations are in sync', 'Run request locale and translated template regressions', 'Reject missing or unsafe failure artifacts'):
            self.assertIn(name, steps)
            self.assertNotIn('if', steps[name])
        self.assertIn('test_i18n_failure_artifact_audit.py', steps['Reject missing or unsafe failure artifacts']['run'])
        self.assertIn('makemigrations --check --dry-run', steps['Check migrations are in sync']['run'])
        for key in PRODUCERS - {'authorization-sqlite'}:
            commands = [s for s in self.workflow['jobs'][key]['steps'] if 'poetry run pytest' in s.get('run', '')]
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0]['env'], {'DJANGO_DB': 'postgresql', 'POSTGRE_HOST': 'localhost', 'POSTGRE_NAME': 'session_security', 'POSTGRE_USER': 'postgres', 'POSTGRE_PASSWORD': 'session-security-test-only'})


if __name__ == '__main__':
    unittest.main()
