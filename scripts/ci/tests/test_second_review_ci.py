"""Closed CI coverage and synthetic bootstrap boundaries for second review."""
import copy
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_common import ROOT, canonical, digest, read_json
from ui_inventory import JOB_NAMES, calls, inventory


def fixture():
    users = {
        name: dict(id=i + 1, email=name + '@second-review84.example.test')
        for i, name in enumerate(('manager', 'initial', 'supervisor', 'a', 'b'))
    }
    projects = {}
    for i, locale in enumerate(('en-US', 'zh-CN')):
        sources = [
            dict(
                id=100 + i * 10 + n,
                assignment_id=200 + i * 10 + n,
                task_id=300 + i * 10 + n,
                annotation_id=400 + i * 10 + n,
                owner='a' if n % 2 == 0 else 'b',
            )
            for n in range(4)
        ]
        sources[3]['second_task_id'] = f'11111111-1111-4111-8111-{i+1:012}'
        projects[locale] = dict(id=10 + i, batch_id=20 + i, sources=sources)
    modes = {'9:10': 'fork_review', '9:11': 'fork_review'}
    return dict(
        schema='second-review-e2e/v1',
        password='only-synthetic-password-value',
        users=users,
        projects=projects,
        settings=dict(
            ANNOTATION_SECOND_REVIEW_ENABLED=True,
            ANNOTATION_SECOND_REVIEW_INSTANCE_ID='22222222-2222-4222-8222-222222222222',
            ANNOTATION_SECOND_REVIEW_MODES=modes,
            ANNOTATION_REVIEW_BATCHES_ENABLED=True,
            ANNOTATION_REVIEW_BATCH_MODES=copy.deepcopy(modes),
            ANNOTATION_PROJECT_OPERATION_PROVIDER='core.management.commands.seed_second_review_e2e.SecondReviewE2EPolicy',
        ),
    )


class SecondReviewInventoryTests(unittest.TestCase):
    def test_original_24_call_inventory_is_unchanged(self):
        value = inventory()
        value['shards'].pop('second-review')
        # Canonical inventory of the reviewed implementation head bd2423d4f.
        self.assertEqual(digest(value), '2781297b92ff71eb2f7ee2f26e00abab68ed9eda9658d4c2625cefd6d6dc8020')

    def test_new_bilingual_call_is_required_and_bounded(self):
        groups = inventory()['shards']
        self.assertEqual((len(groups), sum(map(len, groups.values()))), (10, 25))
        call = calls('second-review')[0]
        self.assertEqual(len(calls('second-review')), 1)
        self.assertEqual(call['id'], 'second-review')
        self.assertEqual(call['languages'], ['en-US', 'zh-CN'])
        self.assertEqual(call['config_file'], 'cypress.second-review.config.ts')
        self.assertEqual((call['timeout_seconds'], call['kill_grace_seconds']), (300, 15))
        self.assertEqual(JOB_NAMES['second-review'], 'Enterprise UI / second-review')

    def test_workflow_isolates_second_review_database_and_sqlite_selects_new_suite(self):
        import yaml

        flow = yaml.load(
            (ROOT / '.github/workflows/enterprise-browser-e2e.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader
        )
        browser = flow['jobs']['browser']
        self.assertIn('second-review', browser['strategy']['matrix']['shard'])
        expected = "${{ matrix.shard == 'second-review' && 'ui_ci_second_review_browser' || 'ui_ci_browser' }}"
        self.assertEqual(browser['env']['POSTGRE_NAME'], expected)
        self.assertEqual(browser['services']['browser-postgres']['env']['POSTGRES_DB'], expected)
        self.assertEqual(browser['env']['POSTGRE_HOST'], '127.0.0.1')
        gate = yaml.load(
            (ROOT / '.github/workflows/fork-pr-gate.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader
        )
        steps = gate['jobs']['authorization-sqlite']['steps']
        sqlite = next(s['run'] for s in steps if s.get('name') == 'Run authorization regression tests')
        for name in (
            'test_second_review.py',
            'test_second_review_authority.py',
            'test_second_review_budgets.py',
            'test_second_review_concurrency.py',
            'test_second_review_integration.py',
            'test_second_review_ledger.py',
            'test_second_review_sources.py',
        ):
            self.assertIn('review_batches/tests/' + name, sqlite)


class SecondReviewFixtureTests(unittest.TestCase):
    def test_followup_flag_is_explicit_boolean_and_preserves_legacy_fixture(self):
        from ui_second_review import validate_fixture
        original = fixture()
        self.assertIs(validate_fixture(original)['ANNOTATION_SECOND_REVIEW_FOLLOWUP_ENABLED'], False)
        current = copy.deepcopy(original)
        current['settings']['ANNOTATION_SECOND_REVIEW_FOLLOWUP_ENABLED'] = True
        self.assertIs(validate_fixture(current)['ANNOTATION_SECOND_REVIEW_FOLLOWUP_ENABLED'], True)
        for invalid in (False, 1, 'true', None):
            current['settings']['ANNOTATION_SECOND_REVIEW_FOLLOWUP_ENABLED'] = invalid
            with self.assertRaises(ValueError):
                validate_fixture(current)

    def test_valid_fixture_returns_copy_and_no_arbitrary_settings(self):
        from ui_second_review import validate_fixture

        value = fixture()
        before = copy.deepcopy(value)
        configured = validate_fixture(value)
        configured['LOCALE_PATHS'] = ['runtime-only']
        configured['ANNOTATION_SECOND_REVIEW_MODES'].clear()
        self.assertEqual(value, before)

    def test_wrong_schema_settings_principals_ids_and_scope_reject(self):
        from ui_second_review import validate_fixture

        mutations = [
            lambda v: v.update(schema='review-confidence-e2e/v1'),
            lambda v: v['settings'].update(LOCALE_PATHS=['injected']),
            lambda v: v['settings'].update(ANNOTATION_PROJECT_OPERATION_PROVIDER='arbitrary.Import'),
            lambda v: v['settings'].update(ANNOTATION_SECOND_REVIEW_ENABLED=1),
            lambda v: v['settings'].update(ANNOTATION_SECOND_REVIEW_INSTANCE_ID='invalid'),
            lambda v: v['settings']['ANNOTATION_SECOND_REVIEW_MODES'].update({'9:999': 'fork_review'}),
            lambda v: v['settings']['ANNOTATION_REVIEW_BATCH_MODES'].update({'9:10': 'core_only_internal'}),
            lambda v: v['users']['supervisor'].update(id=v['users']['initial']['id']),
            lambda v: v['users']['a'].update(email='foreign@example.test'),
            lambda v: v['projects']['en-US'].update(id=True),
            lambda v: v['projects']['en-US']['sources'][0].update(id=1.0),
            lambda v: v['projects']['en-US']['sources'][1].update(owner='supervisor'),
            lambda v: v['projects']['zh-CN'].update(sources=v['projects']['en-US']['sources']),
            lambda v: v['projects']['en-US']['sources'][3].update(second_task_id='bad'),
        ]
        for i, mutate in enumerate(mutations):
            value = fixture()
            mutate(value)
            with self.subTest(case=i), self.assertRaises((ValueError, KeyError, TypeError)):
                validate_fixture(value)

    def test_bootstrap_mode_must_match_scope_shard(self):
        from ui_server import fixture_mode

        for shard, flags in [
            ('second-review', dict(second_review=True)),
            ('confidence', dict(confidence=True)),
            ('enterprise-ui', {}),
        ]:
            self.assertEqual(fixture_mode({'shard': shard}, **flags), shard if flags else 'legacy')
        for shard, flags in [
            ('second-review', {}),
            ('confidence', dict(second_review=True)),
            ('enterprise-ui', dict(second_review=True)),
            ('unknown', {}),
        ]:
            with self.subTest(shard=shard), self.assertRaises(ValueError):
                fixture_mode({'shard': shard}, **flags)


class SecondReviewOwnershipTests(unittest.TestCase):
    def test_actual_readonly_identity_is_pinned_and_server_only_rechecks(self):
        from ui_run import validate_database_owner

        with tempfile.TemporaryDirectory() as d:
            private = Path(d) / 'private'
            private.mkdir()
            database = 'ui_ci_second_review_browser'
            env = dict(
                GITHUB_ACTIONS='true',
                RUNNER_TEMP=d,
                POSTGRE_PORT='5432',
                POSTGRE_HOST='127.0.0.1',
                POSTGRE_NAME=database,
                POSTGRE_USER='synthetic',
                POSTGRE_PASSWORD='synthetic',
            )
            db = MagicMock()
            db.execute.return_value.fetchone.side_effect = [(database,), ('/owned/pg', '5432', 123), (0,)]
            scope = dict(database=database, shard='second-review')
            with patch.dict(os.environ, env, clear=True), patch('ui_server.isolation', return_value=scope), patch(
                'psycopg.connect'
            ) as connect:
                connect.return_value.__enter__.return_value = db
                validate_database_owner(dict(environment='github'), private)
                self.assertEqual(connect.call_args.kwargs['options'], '-c default_transaction_read_only=on')
                proof = read_json(private / 'second-review-bootstrap.json')
                self.assertEqual(
                    (proof['cluster'], proof['system_identifier'], proof['fresh_public_tables']),
                    ('/owned/pg', '123', 0),
                )
                original = (private / 'second-review-bootstrap.json').read_bytes()
                db.execute.reset_mock()
                db.execute.return_value.fetchone.side_effect = [(database,), ('/owned/pg', '5432', 123)]
                validate_database_owner(dict(environment='github'), private, fresh=False)
                self.assertEqual((private / 'second-review-bootstrap.json').read_bytes(), original)
                self.assertFalse(any('information_schema' in str(call) for call in db.execute.call_args_list))
                for directory, port, identifier in [
                    ('/foreign/pg', '5432', 123),
                    ('/owned/pg', '5432', 456),
                    ('/owned/pg', '5433', 123),
                ]:
                    db.execute.return_value.fetchone.side_effect = [(database,), (directory, port, identifier)]
                    with self.assertRaises(ValueError):
                        validate_database_owner(dict(environment='github'), private, fresh=False)
                self.assertEqual((private / 'second-review-bootstrap.json').read_bytes(), original)

    def test_nonempty_or_wrong_database_stops_before_management(self):
        import ui_run

        for actual, count in [('ui_ci_second_review_browser', 1), ('foreign_database', 0)]:
            with self.subTest(actual=actual), tempfile.TemporaryDirectory() as d:
                private = Path(d) / 'private'
                private.mkdir()
                env = dict(
                    TEST_ENVIRONMENT='1',
                    DJANGO_DB='postgresql',
                    GITHUB_ACTIONS='true',
                    RUNNER_TEMP=d,
                    POSTGRE_PORT='5432',
                    POSTGRE_HOST='127.0.0.1',
                    POSTGRE_NAME='ui_ci_second_review_browser',
                    POSTGRE_USER='synthetic',
                    POSTGRE_PASSWORD='synthetic',
                    BASE_DATA_DIR=str(private / 'data'),
                )
                db = MagicMock()
                db.execute.return_value.fetchone.side_effect = [(actual,), ('/owned/pg', '5432', 123), (count,)]
                with patch.dict(os.environ, env, clear=True), patch('psycopg.connect') as connect, patch.object(
                    ui_run, 'management'
                ) as management:
                    connect.return_value.__enter__.return_value = db
                    with self.assertRaises(ValueError):
                        ui_run.seed(dict(environment='github'), 'second-review', private)
                    management.assert_not_called()
                    self.assertFalse((private / 'second-review-bootstrap.json').exists())


class SecondReviewAggregateTests(unittest.TestCase):
    def test_partial_second_review_rerun_keeps_original_build_and_producers(self):
        from test_ui_contracts import ExecutionViewTests, Fixture
        from ui_aggregate import aggregate

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sample = Fixture(root, 2, mixed=True, rerun='second-review')
            ExecutionViewTests.add_copied_views(sample)
            result = aggregate(sample.context, root / 'out', sample)
            mapping = {row['logical']: row for row in result['records']}
            self.assertEqual((len(mapping), result['browser_invocations']), (11, 25))
            self.assertEqual((mapping['prepare']['attempt'], mapping['prepare']['view_attempt']), (1, 2))
            self.assertEqual((mapping['second-review']['attempt'], mapping['second-review']['view_attempt']), (2, 2))

    def test_missing_or_latest_failed_second_review_cannot_borrow_old_green(self):
        from test_ui_contracts import ExecutionViewTests, Fixture
        from ui_aggregate import aggregate

        for state in ('missing', 'failure', 'cancelled', 'timed_out', 'skipped'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                sample = Fixture(root, 2, mixed=True, rerun='confidence')
                previous = next(row for row in sample.rows if row['name'] == JOB_NAMES['second-review'])
                if state == 'missing':
                    sample.rows.remove(previous)
                else:
                    latest = ExecutionViewTests.copied(previous)
                    latest.update(conclusion=state)
                    sample.rows.append(latest)
                with self.assertRaises(ValueError):
                    aggregate(sample.context, root / 'out', sample)

    def test_second_review_report_rejects_wrong_config_and_missing_language_even_with_valid_hashes(self):

        from test_ui_contracts import Fixture
        from ui_aggregate import aggregate
        from ui_results import report_reference

        for mutation in ('config', 'language', 'spec'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                sample = Fixture(root)
                folder = root / 'second-review'
                report_file = folder / 'reports/0.json'
                report = read_json(report_file)
                if mutation == 'language':
                    report['tests'] = report['tests'][:1]
                    report['passed'] = 1
                else:
                    report['actual_config' if mutation == 'config' else 'actual_spec'] = 'wrong'
                report_file.write_bytes(canonical(report))
                receipt = read_json(folder / 'receipt.json')
                receipt['calls'][0]['report'] = report_reference(folder, 'reports/0.json')
                (folder / 'receipt.json').write_bytes(canonical(receipt))
                content = io.BytesIO()
                with zipfile.ZipFile(content, 'w') as archive:
                    for path in folder.rglob('*.json'):
                        archive.writestr(path.relative_to(folder).as_posix(), path.read_bytes())
                artifact = next(a for a in sample.items if '-second-review-evidence' in a['name'])
                sample.payload[artifact['id']] = content.getvalue()
                with self.assertRaises(ValueError):
                    aggregate(sample.context, root / 'out', sample)

    def test_artifact_history_cap_stays_fail_closed_before_download(self):
        from test_ui_contracts import Fixture
        from ui_aggregate import aggregate

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sample = Fixture(root)
            sample.items += [dict(id=10000 + i) for i in range(257 - len(sample.items))]
            with patch.object(sample, 'zip', wraps=sample.zip) as download, self.assertRaises(ValueError):
                aggregate(sample.context, root / 'out', sample)
            download.assert_not_called()


if __name__ == '__main__':
    unittest.main()
