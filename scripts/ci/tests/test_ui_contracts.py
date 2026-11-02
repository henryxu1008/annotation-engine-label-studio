import copy
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_aggregate import GitHub, SafeRedirect, aggregate, latest_jobs, verify_run
from ui_artifacts import pack_build, prepare_export, unpack_build, unpack_evidence_zip
from ui_common import (
    REPOSITORY,
    ROOT,
    WORKFLOW,
    artifact_name,
    canonical,
    digest,
    file_hash,
    loads,
    public_evidence,
    read_json,
    relative_path,
    same_source,
    write_json,
)
from ui_inventory import JOB_NAMES, PREPARE, SHARDS, calls, inventory, prepare_commands
from ui_results import normalize_node_xml, report_reference, unit_skips, validate_report, validate_unit
from ui_server import isolation, validate_fixture


def ctx(attempt=1):
    return {
        'environment': 'github',
        'repository': REPOSITORY,
        'run_id': 123,
        'attempt': attempt,
        'event': 'push',
        'checkout': 'a' * 40,
        'metadata_head': 'a' * 40,
        'pr_head': None,
        'pr_base': None,
        'workflow': WORKFLOW,
        'inventory': 'b' * 64,
        'recipe': 'c' * 64,
    }


def report(call):
    if 'spec' not in call:
        tests = [{'title': 'synthetic::executed', 'state': 'passed'}] if call['kind'] in ('jest', 'node') else []
        tests += [{'title': t, 'state': 'pending'} for t in unit_skips().get(call['id'], {})]
        return {'schema': 'ui-test-report/v1', 'call': call, 'kind': call['kind'], 'exit_code': 0, 'tests': tests}
    titles = [locale + ' executed' for locale in call['languages']]
    if call['language_evidence'] == 'in-spec-switch':
        titles = [
            'suite / ' + s
            for s in [
                'keeps member role codes stable while translating the live roster',
                'preserves selected assignee and assignment version when switching inside its modal',
                'keeps the typed rejection reason and exact pending hash through a modal switch',
                'releases only the selected approved revision after a live language change',
            ]
        ]
    return {
        'schema': 'ui-test-report/v1',
        'kind': 'cypress',
        'call': call,
        'actual_spec': call['spec'],
        'actual_config': call['config_file'],
        'browser': 'electron',
        'languages': call['languages'],
        'tests': [{'title': s, 'state': 'passed', 'attempts': 1} for s in titles],
        'passed': len(titles),
        'failed': 0,
        'pending': 0,
        'skipped': 0,
        'duration_ms': 50,
    }


class Fixture:
    """Synthetic execution metadata only, never evidence of an actual GitHub run."""

    def __init__(self, root, attempt=1, mixed=False):
        self.context = ctx(attempt)
        self.rows = []
        self.items = []
        self.payload = {}
        self.runs_read = 0
        self.jobs_read = 0
        start = '2026-11-02T00:00:00Z'
        end = '2026-11-02T01:00:00Z'
        created = '2026-11-02T00:30:00Z'
        for index, (logical, name) in enumerate(JOB_NAMES.items()):
            a = 1 if mixed and logical != 'confidence' else attempt
            job = {
                'id': index + 1,
                'run_id': 123,
                'name': name,
                'run_attempt': a,
                'head_sha': 'a' * 40,
                'status': 'completed',
                'conclusion': 'success',
                'started_at': start,
                'completed_at': end,
                'html_url': f'https://github.com/{REPOSITORY}/actions/runs/123/job/{index+1}',
            }
            self.rows.append(job)
            pctx = {**self.context, 'attempt': a}
            folder = Path(root) / logical
            folder.mkdir()
            records = []
            for n, call in enumerate(inventory()['prepare'] if logical == 'prepare' else calls(logical)):
                rel = f'reports/{n}.json'
                write_json(folder / rel, report(call))
                records.append(
                    {
                        'id': call['id'],
                        'call': call,
                        'status': 'success',
                        'seconds': 1,
                        'report': report_reference(folder, rel),
                    }
                )
                if logical == 'prepare':
                    records[-1]['actual_commands'] = prepare_commands(call)
            build = {
                'manifest_sha256': 'd' * 64,
                'archive_sha256': 'e' * 64,
                'producer_attempt': 1 if mixed else attempt,
            }
            if logical != 'prepare':
                build['artifact_id'] = 100
            receipt = {
                'schema': 'ui-shard-receipt/v1',
                'context': pctx,
                'logical': logical,
                'status': 'success',
                'calls': records,
                'build': build,
                'diagnostics': {'policy': 'structured-only'},
            }
            write_json(folder / 'receipt.json', receipt)
            out = io.BytesIO()
            with zipfile.ZipFile(out, 'w') as z:
                for p in folder.rglob('*.json'):
                    z.writestr(p.relative_to(folder).as_posix(), p.read_bytes())
            artifact = {
                'id': 101 + index,
                'name': artifact_name(pctx, 'evidence', logical),
                'expired': False,
                'created_at': created,
                'workflow_run': {'id': 123},
            }
            self.items.append(artifact)
            self.payload[artifact['id']] = out.getvalue()
        self.items.append(
            {
                'id': 100,
                'name': artifact_name({**self.context, 'attempt': 1 if mixed else attempt}, 'build', 'prepare'),
                'expired': False,
                'created_at': created,
                'workflow_run': {'id': 123},
            }
        )

    def run(self):
        self.runs_read += 1
        return {
            'id': 123,
            'event': 'push',
            'head_sha': 'a' * 40,
            'run_attempt': self.context['attempt'],
            'name': WORKFLOW,
            'repository': {'full_name': REPOSITORY},
            'head_branch': 'main',
        }

    def jobs(self):
        self.jobs_read += 1
        return copy.deepcopy(self.rows)

    def artifacts(self):
        return copy.deepcopy(self.items)

    def zip(self, identifier):
        return self.payload[identifier]


class InventoryTests(unittest.TestCase):
    def test_migration_is_complete_and_has_no_duplicate_calls(self):
        groups = inventory()['shards']
        old = {
            'review-batches',
            'classification-disagreement',
            'classification-batches',
            'enterprise-current-ui',
            'project-members',
            'task-assignments',
            'reviewer-workspace',
            'submission-release',
            'dm-entity-boundary',
            'auth-tips-timeout',
            'app-locale',
            'dm-editor-locale',
            'dm-task-switch',
            'dm-keyboard-ime',
            'dm-entity-draft',
            'dm-image-draft',
            'annotator-label-stream',
            'dm-export-locale',
            'collaboration-locale',
            'full-enterprise-workflow',
            'fixed-task-packages',
            'import-initialization',
        }
        flat = [c for g in groups.values() for c in g]
        self.assertEqual(len(groups), 9)
        self.assertEqual(len(flat), 24)
        self.assertEqual({Path(c['spec']).name.removesuffix('.cy.ts') for c in flat}, old | {'review-confidence'})
        self.assertEqual(len({c['id'] for c in flat}), 24)

    def test_parameters_languages_resets_and_limits_are_not_weakened(self):
        self.assertEqual(calls('full-en')[0]['env'], {'fullFlowLocale': 'en-US'})
        self.assertEqual(calls('full-zh')[0]['env'], {'fullFlowLocale': 'zh-CN'})
        self.assertTrue(all(c['env'] == {'loginLocale': 'en-US'} for c in calls('enterprise-ui')))
        self.assertEqual(calls('locale-a')[0]['timeout_seconds'], 300)
        self.assertEqual(calls('locale-a')[0]['kill_grace_seconds'], 15)
        for s in ('locale-a', 'locale-b', 'locale-c', 'full-en', 'full-zh', 'packages-import'):
            self.assertTrue(all(c['reset'] for c in calls(s)))
        self.assertEqual(calls('confidence')[0]['config_file'], 'cypress.review-confidence.config.ts')
        self.assertTrue(all(c['browser'] == 'electron' and c['video'] is True for s in SHARDS for c in calls(s)))

    def test_original_prepare_commands_and_ui_paths_are_retained(self):
        self.assertEqual(len(PREPARE), 10)
        self.assertEqual(sum(k != 'build' for k, _, _ in PREPARE), 9)
        ui = next(v for k, v, t in PREPARE if k == 'ui')
        self.assertEqual(
            ui[-3:],
            [
                'libs/ui/src/lib/dropdown/dropdown.test.tsx',
                'libs/ui/src/lib/dropdown/dropdown-transition.test.tsx',
                'libs/ui/src/lib/toast/toast.test.tsx',
            ],
        )

    def test_unknown_shard_rejects(self):
        with self.assertRaises(ValueError):
            calls('empty')

    def test_workflow_required_gate_and_events_are_unconditional(self):
        import yaml

        w = yaml.load((ROOT / '.github/workflows/enterprise-browser-e2e.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(w['on']), {'push', 'pull_request', 'workflow_dispatch'})
        self.assertEqual(set(w['on']['pull_request']), {'branches', 'types'})
        self.assertEqual(
            w['on']['pull_request']['types'], ['opened', 'synchronize', 'reopened', 'ready_for_review', 'edited']
        )
        self.assertEqual(w['on']['push']['branches'], ['main'])
        jobs = w['jobs']
        self.assertEqual(set(jobs), {'prepare-ui', 'browser', 'current-ui-validation'})
        gate = jobs['current-ui-validation']
        self.assertEqual(gate['name'], 'Current enterprise UI validation')
        self.assertEqual(gate['if'], '$' + '{{ always() }}')
        self.assertEqual(gate['needs'], ['prepare-ui', 'browser'])
        self.assertEqual(gate['permissions'], {'contents': 'read', 'actions': 'read'})
        self.assertEqual(w['permissions'], {'contents': 'read'})
        self.assertEqual(jobs['browser']['strategy']['matrix']['shard'], list(SHARDS))
        self.assertEqual(jobs['browser']['strategy']['max-parallel'], '3')
        self.assertEqual(jobs['browser']['strategy']['fail-fast'], 'false')
        self.assertEqual([jobs[k]['timeout-minutes'] for k in jobs], ['25', '40', '5'])
        for job in jobs.values():
            self.assertNotIn('continue-on-error', job)
            checkout = next(s for s in job['steps'] if s.get('uses', '').startswith('actions/checkout@'))
            self.assertNotIn('with', checkout)
        for key in ('NX_SKIP_NX_CACHE', 'NX_SKIP_REMOTE_CACHE'):
            self.assertEqual(w['env'][key], 'true')
        for key, folder in [('prepare-ui', 'prepare'), ('browser', 'browser')]:
            upload = next(s for s in jobs[key]['steps'] if s.get('name', '').startswith('Retain bounded'))
            self.assertEqual(upload['with']['path'], '$' + '{{ runner.temp }}/ui-' + folder + '-evidence-export/')


class ReportTests(unittest.TestCase):
    def test_all_closed_invocations_accept_structured_success(self):
        for call in inventory()['prepare'] + [c for s in SHARDS for c in calls(s)]:
            validate_report(report(call), call)

    def test_browser_zero_failed_pending_skip_retry_and_missing_language_reject(self):
        call = calls('confidence')[0]
        for mutate in [
            lambda r: r.update(tests=[], passed=0),
            lambda r: r.update(failed=1),
            lambda r: r.update(pending=1),
            lambda r: r.update(skipped=1),
            lambda r: r['tests'][0].update(attempts=2),
            lambda r: r['tests'][0].update(state='failed'),
            lambda r: r['tests'][1].update(title='wrong language'),
            lambda r: r.update(actual_spec='wrong.cy.ts'),
            lambda r: r.update(actual_config='cypress.config.ts'),
            lambda r: r.update(browser='chrome'),
            lambda r: r.update(passed=True),
        ]:
            v = report(call)
            mutate(v)
            with self.subTest(value=v), self.assertRaises(ValueError):
                validate_report(v, call)

    def test_preparation_false_exit_and_zero_tests_reject(self):
        call = inventory()['prepare'][3]
        for change in ({'tests': []}, {'exit_code': False}, {'tests': [{'title': 'x', 'state': 'pending'}]}):
            v = report(call)
            v.update(change)
            with self.assertRaises(ValueError):
                validate_report(v, call)

    def test_explicit_skip_allowance_is_exact_and_cannot_skip_everything(self):
        with patch('ui_results.unit_skips', return_value={'editor': {'b': 'Existing deliberate coverage boundary'}}):
            self.assertEqual(
                validate_unit([{'title': 'a', 'state': 'passed'}, {'title': 'b', 'state': 'pending'}], 'editor')[
                    'passed'
                ],
                1,
            )
            with self.assertRaises(ValueError):
                validate_unit([{'title': 'b', 'state': 'pending'}], 'editor')
            with self.assertRaises(ValueError):
                validate_unit([{'title': 'a', 'state': 'passed'}], 'editor')

    def test_node_junit_rejects_empty_failed_and_entity_reports(self):
        good = b'<testsuites><testsuite><testcase classname="suite" name="ran"/></testsuite></testsuites>'
        self.assertEqual(normalize_node_xml(good), [{'title': 'suite::ran', 'state': 'passed'}])
        for raw in (b'<testsuites/>', b'<!DOCTYPE x><testsuites/>'):
            with self.assertRaises(ValueError):
                normalize_node_xml(raw)
        tests = normalize_node_xml(b'<testsuite><testcase name="bad"><failure/></testcase></testsuite>')
        with self.assertRaises(ValueError):
            validate_unit(tests, 'diagnostics')

    def test_json_bytes_duplicates_nonfinite_and_false_ids(self):
        self.assertEqual(loads(b'{"a":1}', 7), {'a': 1})
        for raw, cap in [(b'{"a":1}', 6), (b'{"a":1,"a":2}', 100), (b'{"a":NaN}', 100)]:
            with self.assertRaises(ValueError):
                loads(raw, cap)

    def test_credentials_private_fields_and_known_password_are_not_exportable(self):
        for v in (
            {'password': 'fake'},
            {'settings': {}},
            {'title': 'Cookie: sessionid=abcdefghijklmnop'},
            {'title': 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz'},
        ):
            with self.assertRaises(ValueError):
                public_evidence(v)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'secret.json'
            write_json(path, ['synthetic-private-password'])
            with patch.dict(os.environ, {'UI_SECRET_FILE': str(path)}):
                with self.assertRaises(ValueError):
                    public_evidence({'title': 'synthetic-private-password'})
                public_evidence({'title': 'checks safe state'})


class ArtifactTests(unittest.TestCase):
    def producer_fixture(self, root, payload=None):
        folder = Path(root) / 'evidence'
        write_json(folder / 'reports/known.json', {'status': 'passed'} if payload is None else payload)
        receipt = {
            'schema': 'ui-shard-receipt/v1',
            'calls': [{'status': 'success', 'report': report_reference(folder, 'reports/known.json')}],
        }
        write_json(folder / 'receipt.json', receipt)
        return folder, receipt

    def test_producer_exports_only_verified_receipt_and_referenced_reports(self):
        with tempfile.TemporaryDirectory() as d:
            folder, receipt = self.producer_fixture(d)
            output = prepare_export(folder, receipt)
            self.assertEqual(
                sorted(p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()),
                ['receipt.json', 'reports/known.json'],
            )
            for name in ('receipt.json', 'reports/known.json'):
                self.assertEqual((folder / name).read_bytes(), (output / name).read_bytes())
            with self.assertRaises(ValueError):
                prepare_export(folder, receipt)

    def test_producer_rejects_private_and_unreferenced_files_before_staging(self):
        for name in ('fixture.json', 'settings.json', 'raw.log', 'reports/unreferenced.json'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                folder, receipt = self.producer_fixture(d)
                (folder / name).write_text('{}')
                with self.assertRaisesRegex(ValueError, 'Extra/private producer evidence'):
                    prepare_export(folder, receipt)
                self.assertFalse((Path(d) / 'evidence-export').exists())

    def test_producer_rejects_private_report_content_and_known_secrets_before_staging(self):
        for payload in (
            {'password': 'test-password'},
            {'detail': 'Cookie: synthetic-value'},
            {'detail': 'fixture-secret-value'},
        ):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as d:
                secret = Path(d) / 'private-secrets.json'
                write_json(secret, ['fixture-secret-value'])
                folder, receipt = self.producer_fixture(d, payload)
                with patch.dict(os.environ, {'UI_SECRET_FILE': str(secret)}), self.assertRaises(ValueError):
                    prepare_export(folder, receipt)
                self.assertFalse((Path(d) / 'evidence-export').exists())

    def test_producer_rejects_receipt_changed_before_export(self):
        with tempfile.TemporaryDirectory() as d:
            folder, receipt = self.producer_fixture(d)
            (folder / 'receipt.json').write_bytes(canonical({**receipt, 'status': 'changed'}))
            with self.assertRaisesRegex(ValueError, 'Receipt changed'):
                prepare_export(folder, receipt)
            self.assertFalse((Path(d) / 'evidence-export').exists())

    def test_roundtrip_is_bound_to_source_and_verifies_before_install(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            src = p / 'dist'
            src.mkdir()
            (src / 'main.js').write_text('compiled')
            pack_build(ctx(), p / 'package', src)
            unpack_build(ctx(), p / 'package', p / 'installed')
            self.assertEqual((p / 'installed/main.js').read_text(), 'compiled')
            with self.assertRaises(ValueError):
                unpack_build(ctx(), p / 'package', p / 'installed')
            changed = ctx()
            changed['checkout'] = 'f' * 40
            with self.assertRaises(ValueError):
                unpack_build(changed, p / 'package', p / 'other')

    def test_content_tamper_never_installs(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            src = p / 'src'
            src.mkdir()
            (src / 'main.js').write_text('original')
            pack_build(ctx(), p / 'pkg', src)
            with (p / 'pkg/frontend.tar').open('ab') as f:
                f.write(b'changed')
            with self.assertRaises(ValueError):
                unpack_build(ctx(), p / 'pkg', p / 'dest')
            self.assertFalse((p / 'dest').exists())

    def test_tar_links_duplicate_and_traversal_reject_even_if_archive_digest_matches(self):
        for name, kind in [
            ('../outside', tarfile.REGTYPE),
            ('/outside', tarfile.REGTYPE),
            ('main.js', tarfile.SYMTYPE),
            ('main.js', tarfile.LNKTYPE),
        ]:
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as d:
                p = Path(d)
                src = p / 'src'
                src.mkdir()
                (src / 'main.js').write_bytes(b'x')
                manifest = pack_build(ctx(), p / 'pkg', src)
                with tarfile.open(p / 'pkg/frontend.tar', 'w') as z:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    info.linkname = '../outside'
                    info.size = 1 if kind == tarfile.REGTYPE else 0
                    z.addfile(info, io.BytesIO(b'x') if info.size else None)
                manifest['archive_sha256'] = file_hash(p / 'pkg/frontend.tar')
                (p / 'pkg/manifest.json').write_bytes(canonical(manifest))
                with self.assertRaises(ValueError):
                    unpack_build(ctx(), p / 'pkg', p / 'dest')
                self.assertFalse((p / 'dest').exists())
                self.assertFalse((p / 'outside').exists())

    def test_unsafe_paths_and_build_union_boundary(self):
        for name in ('../x', '/x', 'a\\x', 'C:/x', 'a//x', 'a/./x', 'a/../x'):
            with self.subTest(path=name), self.assertRaises(ValueError):
                relative_path(name)
        from ui_common import regular_tree

        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / 'a').write_bytes(b'xx')
            (p / 'b').write_bytes(b'yy')
            self.assertEqual(len(regular_tree(p, 2, 4, 2)), 2)
            for count, total, single in ((1, 4, 2), (2, 3, 2), (2, 4, 1)):
                with self.assertRaises(ValueError):
                    regular_tree(p, count, total, single)

    def test_zip_never_extracts_private_paths_links_duplicates_or_excess_bytes(self):
        for name, mode in [('../x', 0), ('/x', 0), ('fixture.json', 0), ('receipt.json', 0o120777 << 16)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, 'w') as z:
                    info = zipfile.ZipInfo(name)
                    info.external_attr = mode
                    z.writestr(info, b'{}')
                with self.assertRaises(ValueError):
                    unpack_evidence_zip(buf.getvalue(), Path(d) / 'out')


class AggregateTests(unittest.TestCase):
    def test_complete_positive_fixture_and_exact_execution_map(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            f = Fixture(p)
            value = aggregate(f.context, p / 'aggregate', f)
            self.assertEqual(value['status'], 'success')
            self.assertEqual(len(value['records']), 10)
            self.assertEqual(value['browser_invocations'], 24)
            self.assertEqual(f.jobs_read, 2)

    def test_partial_rerun_can_reuse_proven_old_success_without_relabeling(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            f = Fixture(p, attempt=2, mixed=True)
            value = aggregate(f.context, p / 'out', f)
            mapping = {r['logical']: r['attempt'] for r in value['records']}
            self.assertEqual(mapping['prepare'], 1)
            self.assertEqual(mapping['confidence'], 2)

    def test_latest_failed_cancelled_skipped_neutral_missing_never_uses_old_success(self):
        for state in ('failure', 'cancelled', 'timed_out', 'skipped', 'neutral', None):
            with self.subTest(conclusion=state), tempfile.TemporaryDirectory() as d:
                p = Path(d)
                f = Fixture(p, 2, mixed=True)
                old = copy.deepcopy(f.rows[-1])
                old.update(id=99, run_attempt=1)
                f.rows.append(old)
                f.rows[-2]['conclusion'] = state
                with self.assertRaises(ValueError):
                    aggregate(f.context, p / 'out', f)
        with tempfile.TemporaryDirectory() as d:
            f = Fixture(Path(d))
            f.rows.pop()
            with self.assertRaises(ValueError):
                latest_jobs(f.context, f.rows)

    def test_duplicate_same_attempt_or_unknown_job_reject(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fixture(Path(d))
            f.rows.append(copy.deepcopy(f.rows[0]))
            with self.assertRaises(ValueError):
                latest_jobs(f.context, f.rows)
            f.rows[-1]['name'] = 'unexpected'
            with self.assertRaises(ValueError):
                latest_jobs(f.context, f.rows)

    def test_receipt_tamper_wrong_sha_attempt_or_artifact_id_reject(self):
        for mutate in (
            lambda r: r['context'].update(checkout='f' * 40),
            lambda r: r['context'].update(attempt=2),
            lambda r: r['build'].update(artifact_id=999),
            lambda r: r['calls'].pop(),
            lambda r: r.update(password='never archive'),
            lambda r: r['build'].update(manifest_sha256=None),
            lambda r: r['build'].update(archive_sha256='f' * 64),
        ):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as d:
                p = Path(d)
                f = Fixture(p)
                identifier = f.items[-2]['id']
                inp = zipfile.ZipFile(io.BytesIO(f.payload[identifier]))
                content = {n: inp.read(n) for n in inp.namelist()}
                r = json.loads(content['receipt.json'])
                mutate(r)
                content['receipt.json'] = canonical(r)
                out = io.BytesIO()
                with zipfile.ZipFile(out, 'w') as z:
                    for n, raw in content.items():
                        z.writestr(n, raw)
                f.payload[identifier] = out.getvalue()
                with self.assertRaises((ValueError, KeyError)):
                    aggregate(f.context, p / 'out', f)

    def test_prepare_missing_or_changed_actual_argv_rejects(self):
        for change in ('missing', 'empty', 'missing-ui-path', 'different-command'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as d:
                p = Path(d)
                f = Fixture(p)
                identifier = f.items[0]['id']
                inp = zipfile.ZipFile(io.BytesIO(f.payload[identifier]))
                content = {n: inp.read(n) for n in inp.namelist()}
                r = json.loads(content['receipt.json'])
                actual = r['calls'][6]
                if change == 'missing':
                    actual.pop('actual_commands')
                elif change == 'empty':
                    actual['actual_commands'] = []
                elif change == 'missing-ui-path':
                    actual['actual_commands'][0].remove('libs/ui/src/lib/toast/toast.test.tsx')
                else:
                    actual['actual_commands'][0][0] = 'echo'
                content['receipt.json'] = canonical(r)
                out = io.BytesIO()
                with zipfile.ZipFile(out, 'w') as z:
                    for n, raw in content.items():
                        z.writestr(n, raw)
                f.payload[identifier] = out.getvalue()
                with self.assertRaises(ValueError):
                    aggregate(f.context, p / 'out', f)

    def test_missing_expired_duplicate_artifacts_and_old_timestamp_reject(self):
        for change in ('missing', 'expired', 'duplicate', 'old'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as d:
                p = Path(d)
                f = Fixture(p)
                if change == 'missing':
                    f.items.pop(0)
                elif change == 'expired':
                    f.items[0]['expired'] = True
                elif change == 'duplicate':
                    f.items.append(copy.deepcopy(f.items[0]))
                else:
                    f.items[0]['created_at'] = '2026-11-01T00:00:00Z'
                with self.assertRaises(ValueError):
                    aggregate(f.context, p / 'out', f)

    def test_rerun_race_after_download_rejects(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            f = Fixture(p)
            original = f.run

            def run():
                value = original()
                if f.runs_read > 1:
                    value['run_attempt'] = 2
                return value

            f.run = run
            with self.assertRaises(ValueError):
                aggregate(f.context, p / 'out', f)

    def test_failure_map_is_bounded_and_retains_non_green_execution(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            f = Fixture(p)
            f.rows[-1]['conclusion'] = 'cancelled'
            with self.assertRaises(ValueError):
                aggregate(f.context, p / 'out', f)
            value = read_json(p / 'out/result.json')
            self.assertEqual(value['status'], 'failed')
            self.assertEqual(value['stage'], 'jobs')
            self.assertEqual(value['jobs'][-1]['conclusion'], 'cancelled')
            self.assertEqual(value['code'], 'UI_AGGREGATE_EVIDENCE_REJECTED')
            self.assertLess((p / 'out/result.json').stat().st_size, 128 * 1024)
            public_evidence(value)

    def test_run_event_pr_metadata_and_source_types(self):
        a = ctx()
        b = copy.deepcopy(a)
        b['attempt'] = 2
        same_source(a, b)
        b['run_id'] = True
        with self.assertRaises(ValueError):
            same_source(a, b)
        value = {
            'id': 123,
            'event': 'push',
            'head_sha': 'a' * 40,
            'run_attempt': 1,
            'name': WORKFLOW,
            'repository': {'full_name': REPOSITORY},
            'head_branch': 'main',
        }
        verify_run(a, value)
        for k, v in (
            ('event', 'pull_request'),
            ('head_branch', 'develop'),
            ('head_sha', 'f' * 40),
            ('run_attempt', 2),
        ):
            bad = {**value, k: v}
            with self.assertRaises(ValueError):
                verify_run(a, bad)

    def test_read_api_paginates_filter_all_and_rejects_budget_or_incomplete_pages(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'synthetic-read-token'}):
            api = GitHub(ctx())
        pages = [
            {'total_count': 101, 'jobs': [{'id': i} for i in range(100)]},
            {'total_count': 101, 'jobs': [{'id': 100}]},
        ]
        with patch.object(api, 'json', side_effect=pages) as get:
            self.assertEqual(len(api.jobs()), 101)
            self.assertIn('filter=all', get.call_args_list[0].args[0])
        for value in ({'total_count': 257, 'jobs': []}, {'total_count': 1, 'jobs': []}):
            with patch.object(api, 'json', return_value=value), self.assertRaises(ValueError):
                api.jobs()

    def test_redirect_strips_token_and_rejects_non_https(self):
        from urllib.request import Request

        req = Request('https://api.github.com/x', headers={'Authorization': 'Bearer synthetic-read-token'})
        redirect = SafeRedirect().redirect_request(req, None, 302, '', {}, 'https://blob.example.test/file')
        self.assertIsNone(redirect.get_header('Authorization'))
        with self.assertRaises(ValueError):
            SafeRedirect().redirect_request(req, None, 302, '', {}, 'http://blob.example.test/file')


def synthetic_fixture():
    profile = '11111111-1111-4111-8111-111111111111'
    namespace = '22222222-2222-4222-8222-222222222222'
    users = {
        n: {'id': i + 1, 'email': n + '@review-confidence84.example.test'}
        for i, n in enumerate(('manager', 'producer', 'reviewer', 'a', 'b'))
    }
    descriptor = {'profile_id': profile, 'producer_namespace': namespace}
    projects = {
        locale: {
            'id': 10 + i,
            'sources': [
                {
                    'id': 100 + i * 10 + n,
                    'assignment_id': 200 + n,
                    'annotation_id': 300 + n,
                    'task_id': 400 + n,
                    'owner': 'a',
                }
                for n in range(5)
            ],
        }
        for i, locale in enumerate(('en-US', 'zh-CN'))
    }
    settings = {
        'ANNOTATION_REVIEW_BATCHES_ENABLED': True,
        'ANNOTATION_REVIEW_CONFIDENCE_ENABLED': True,
        'ANNOTATION_REVIEW_CONFIDENCE_INSTANCE_ID': '33333333-3333-4333-8333-333333333333',
        'ANNOTATION_REVIEW_CONFIDENCE_PROFILES': [
            {
                'organization_id': 9,
                'project_id': p['id'],
                'descriptor': descriptor,
                'digest': digest(descriptor),
                'fresh': True,
            }
            for p in projects.values()
        ],
        'ANNOTATION_REVIEW_CONFIDENCE_GRANTS': [
            {
                'organization_id': 9,
                'project_id': p['id'],
                'principal_id': 'label_studio_local:2',
                'source': 'label_studio_local',
                'actor_id': 2,
                'producer_namespace': namespace,
                'profile_ids': [profile],
            }
            for p in projects.values()
        ],
        'ANNOTATION_REVIEW_BATCH_MODES': {'9:10': 'fork_review', '9:11': 'fork_review'},
        'ANNOTATION_PROJECT_OPERATION_PROVIDER': 'core.management.commands.seed_review_confidence_e2e.ConfidenceE2EPolicy',
    }
    return {
        'schema': 'review-confidence-e2e/v1',
        'password': 'only-synthetic-password-value',
        'users': users,
        'projects': projects,
        'settings': settings,
        'profile_id': profile,
        'profile_digest': digest(descriptor),
    }


class BootstrapTests(unittest.TestCase):
    def test_runtime_overrides_do_not_mutate_frozen_fixture_settings(self):
        value = synthetic_fixture()
        before = copy.deepcopy(value)
        runtime = validate_fixture(value)
        runtime['LOCALE_PATHS'] = ['job-private-catalogs']
        runtime['ANNOTATION_REVIEW_CONFIDENCE_GRANTS'][0]['actor_id'] = 999
        self.assertEqual(value, before)
        self.assertEqual(validate_fixture(value), before['settings'])

    def test_fixed_allowlist_and_synthetic_bindings(self):
        value = synthetic_fixture()
        self.assertEqual(validate_fixture(value), value['settings'])
        for change in (
            lambda v: v['settings'].update(DATABASES={}),
            lambda v: v['settings'].update(ANNOTATION_PROJECT_OPERATION_PROVIDER='arbitrary.executable'),
            lambda v: v['settings']['ANNOTATION_REVIEW_CONFIDENCE_GRANTS'][0].update(project_id=999),
            lambda v: v['settings']['ANNOTATION_REVIEW_CONFIDENCE_GRANTS'][0].update(organization_id=99),
            lambda v: v['settings']['ANNOTATION_REVIEW_CONFIDENCE_PROFILES'][0].update(digest='f' * 64),
            lambda v: v['users']['manager'].update(email='real@example.com'),
            lambda v: v['settings'].update(ANNOTATION_REVIEW_CONFIDENCE_ENABLED=1),
        ):
            v = synthetic_fixture()
            change(v)
            with self.assertRaises(ValueError):
                validate_fixture(v)

    def test_native_isolation_refuses_non_test_or_container_or_foreign_database(self):
        for env in (
            {'TEST_ENVIRONMENT': '0'},
            {'TEST_ENVIRONMENT': '1', 'ENTERPRISE_E2E_CONTAINER': 'retained'},
            {
                'TEST_ENVIRONMENT': '1',
                'DJANGO_DB': 'postgresql',
                'POSTGRE_HOST': '127.0.0.1',
                'POSTGRE_NAME': 'retained',
            },
            {
                'TEST_ENVIRONMENT': '1',
                'DJANGO_DB': 'postgresql',
                'POSTGRE_HOST': 'remote',
                'POSTGRE_NAME': 'ui_ci_browser',
            },
        ):
            with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
                isolation()

    def test_seed_rejects_remote_backend_before_any_management_or_child_process(self):
        import ui_run

        for fields in (
            {'POSTGRE_HOST': 'remote.example.test'},
            {'DJANGO_DB': 'sqlite'},
            {'ENTERPRISE_E2E_CONTAINER': 'retained'},
        ):
            with self.subTest(fields=fields), tempfile.TemporaryDirectory() as d:
                p = Path(d)
                private = p / 'private'
                private.mkdir()
                env = {
                    'TEST_ENVIRONMENT': '1',
                    'DJANGO_DB': 'postgresql',
                    'POSTGRE_HOST': '127.0.0.1',
                    'POSTGRE_PORT': '5432',
                    'POSTGRE_NAME': 'ui_ci_browser',
                    'BASE_DATA_DIR': str(private / 'data'),
                    **fields,
                }
                with patch.dict(os.environ, env, clear=True), patch.object(
                    ui_run, 'management'
                ) as manage, patch.object(ui_run.subprocess, 'Popen') as proc:
                    with self.assertRaises(ValueError):
                        ui_run.seed(ctx(), 'confidence', private)
                    manage.assert_not_called()
                    proc.assert_not_called()


class ExecutionBoundaryTests(unittest.TestCase):
    def test_tracked_product_drift_is_detected_despite_same_commit_and_inventory(self):
        from ui_common import verify_context

        def git(*args):
            return ' M label_studio/tasks/models.py' if args[0] == 'status' else 'a' * 40

        with patch('ui_common.git', side_effect=git), patch(
            'ui_common.inventory_digest', return_value='b' * 64
        ), patch('ui_common.recipe_digest', return_value='c' * 64):
            with self.assertRaisesRegex(ValueError, 'dirty'):
                verify_context(ctx())

    def test_last_helper_drift_is_detected(self):
        from ui_common import verify_context

        with patch('ui_common.git', return_value='a' * 40), patch(
            'ui_common.inventory_digest', return_value='f' * 64
        ), patch('ui_common.recipe_digest', return_value='c' * 64):
            with self.assertRaisesRegex(ValueError, 'Source changed'):
                verify_context(ctx())

    def test_installed_build_drift_after_last_spec_rejects(self):
        from ui_common import regular_tree
        from ui_run import verify_installed_build

        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / 'main.js').write_text('original')
            manifest = {'schema': 'ui-build/v1', 'context': ctx(), 'files': regular_tree(p)}
            verify_installed_build(ctx(), manifest, p)
            (p / 'main.js').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'Installed build changed'):
                verify_installed_build(ctx(), manifest, p)

    def test_cached_exit_without_fresh_unit_reports_never_passes_prepare(self):
        import ui_run
        from ui_common import context

        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=False), patch.object(
            ui_run, 'run', return_value=(0, 0.1)
        ):
            value = ui_run.prepare(context(local=True), Path(d) / 'out')
            self.assertFalse(value)
            result = read_json(Path(d) / 'out/receipt.json')
            self.assertEqual(result['status'], 'failed')
            self.assertTrue(any(c['status'] == 'failed' for c in result['calls']))

    def test_local_identity_is_explicit_and_never_a_github_check(self):
        from ui_common import context

        value = 'local-11111111-1111-4111-8111-111111111111'
        with patch.dict(os.environ, {'UI_LOCAL_RUN_ID': value}):
            a = context(local=True)
            b = context(local=True)
            same_source(a, b)
            self.assertEqual(a['run_id'], value)
            with self.assertRaises(ValueError):
                GitHub(a)


class CatalogTests(unittest.TestCase):
    def test_actual_root_locales_are_required_and_both_outputs_verified(self):
        import shutil

        from ui_catalogs import LOCALES, validate_catalogs

        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            proof = {
                'schema': 'ui-catalogs/v1',
                'method': 'fixture of existing tracked MO; not a compiler run',
                'locales': {},
            }
            for locale in LOCALES:
                src = ROOT / 'label_studio/locale' / locale / 'LC_MESSAGES/django.po'
                self.assertTrue(src.is_file())
                mo = ROOT / 'label_studio/locale' / locale / 'LC_MESSAGES/django.mo'
                target = p / 'catalogs' / locale / 'LC_MESSAGES/django.mo'
                target.parent.mkdir(parents=True)
                shutil.copyfile(mo, target)
                proof['locales'][locale] = {'source_sha256': file_hash(src), 'mo_sha256': file_hash(target)}
            write_json(p / 'catalogs.json', proof)
            self.assertEqual(validate_catalogs(p), p / 'catalogs')
            (p / 'catalogs/zh_Hans/LC_MESSAGES/django.mo').write_bytes(b'tampered')
            with self.assertRaises(ValueError):
                validate_catalogs(p)

    def test_exact_receipt_and_report_utf8_limits(self):
        from ui_common import RECEIPT_LIMIT, REPORT_LIMIT

        for cap in (RECEIPT_LIMIT, REPORT_LIMIT):
            base = len(canonical({'x': ''}))
            value = {'x': 'a' * (cap - base)}
            raw = canonical(value, cap)
            self.assertEqual(len(raw), cap)
            self.assertEqual(loads(raw, cap), value)
            with self.assertRaises(ValueError):
                canonical({'x': value['x'] + 'a'}, cap)
            with self.assertRaises(ValueError):
                loads(raw + b' ', cap)
        self.assertEqual(len(canonical({'x': '中'})) - len(canonical({'x': ''})), 3)


class DefaultBudgetTests(unittest.TestCase):
    def test_real_default_build_single_union_and_count_edges(self):
        from ui_common import BUILD_COUNT, BUILD_FILE_LIMIT, BUILD_LIMIT, regular_tree

        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            for n in range(4):
                with (p / str(n)).open('wb') as f:
                    f.truncate(BUILD_FILE_LIMIT)
            rows = regular_tree(p)
            self.assertEqual(sum(r['size'] for r in rows), BUILD_LIMIT)
            (p / 'extra').write_bytes(b'x')
            with self.assertRaisesRegex(ValueError, 'union budget'):
                regular_tree(p)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            with (p / 'over').open('wb') as f:
                f.truncate(BUILD_FILE_LIMIT + 1)
            with self.assertRaisesRegex(ValueError, 'File byte budget'):
                regular_tree(p)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            for n in range(BUILD_COUNT):
                (p / str(n)).write_bytes(b'')
            self.assertEqual(len(regular_tree(p)), BUILD_COUNT)
            (p / 'one-too-many').write_bytes(b'')
            with self.assertRaisesRegex(ValueError, 'union budget'):
                regular_tree(p)

    def test_real_report_union_and_receipt_call_edges(self):
        from ui_artifacts import evidence_files
        from ui_common import REPORT_LIMIT, REPORT_UNION_LIMIT

        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            records = []
            for n in range(5):
                rel = f'reports/{n}.json'
                write_json(p / rel, {'x': 'a' * (REPORT_LIMIT - len(canonical({'x': ''})))})
                records.append({'status': 'success', 'report': report_reference(p, rel)})
            receipt = {'schema': 'ui-shard-receipt/v1', 'calls': records[:4]}
            write_json(p / 'receipt.json', receipt)
            self.assertEqual(sum(r['size'] for r in records[:4] for r in [r['report']]), REPORT_UNION_LIMIT)
            self.assertEqual(len(evidence_files(p, receipt)), 5)
            receipt['calls'] = records
            with self.assertRaisesRegex(ValueError, 'union budget'):
                evidence_files(p, receipt)
            receipt['calls'] = [{'status': 'failed', 'report': None} for _ in range(128)]
            self.assertEqual(len(evidence_files(p, receipt)), 1)
            receipt['calls'].append({'status': 'failed', 'report': None})
            with self.assertRaisesRegex(ValueError, 'call budget'):
                evidence_files(p, receipt)

    def test_duplicate_tar_and_zip_members_never_install(self):
        import warnings

        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            src = p / 'src'
            src.mkdir()
            (src / 'main.js').write_bytes(b'x')
            manifest = pack_build(ctx(), p / 'pkg', src)
            with tarfile.open(p / 'pkg/frontend.tar', 'w') as z:
                for _ in range(2):
                    info = tarfile.TarInfo('main.js')
                    info.size = 1
                    z.addfile(info, io.BytesIO(b'x'))
            manifest['archive_sha256'] = file_hash(p / 'pkg/frontend.tar')
            (p / 'pkg/manifest.json').write_bytes(canonical(manifest))
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                unpack_build(ctx(), p / 'pkg', p / 'dest')
            self.assertFalse((p / 'dest').exists())
        with tempfile.TemporaryDirectory() as d, warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w') as z:
                z.writestr('receipt.json', b'{}')
                z.writestr('receipt.json', b'{}')
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                unpack_evidence_zip(buf.getvalue(), Path(d) / 'out')

    def test_empty_matrix_and_missing_invocation_are_rejected(self):
        with patch('ui_inventory.SHARDS', {}), self.assertRaises(ValueError):
            inventory()
        call = calls('confidence')[0]
        bad = report(call)
        bad['call'] = {**call, 'languages': ['en-US']}
        with self.assertRaises(ValueError):
            validate_report(bad, call)


if __name__ == '__main__':
    unittest.main()
