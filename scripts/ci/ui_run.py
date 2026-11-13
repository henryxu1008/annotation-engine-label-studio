"""Execute the closed CI inventory and export only narrow, credential-free evidence."""
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from ui_common import (
    RECEIPT_LIMIT,
    REPORT_LIMIT,
    REPORT_UNION_LIMIT,
    ROOT,
    file_hash,
    public_evidence,
    read_json,
    require,
    verify_context,
    write_json,
)
from ui_inventory import calls, inventory, normalize_commands, prepare_commands
from ui_results import normalize_jest, normalize_node_xml, report_reference, validate_report


def cypress_failure_indices(path):
    """Accept only bounded numeric test positions, never Cypress diagnostics."""
    try:
        value = read_json(path)
        if (not isinstance(value, dict) or set(value) != {'schema', 'total', 'failed', 'categories', 'lines'}
                or value['schema'] != 'ui-cypress-failure-indices/v3'
                or type(value['total']) is not int or not 1 <= value['total'] <= 500
                or not isinstance(value['failed'], list) or not value['failed']
                or any(type(n) is not int or not 1 <= n <= value['total'] for n in value['failed'])
                or value['failed'] != sorted(set(value['failed']))
                or not isinstance(value['categories'], list)
                or len(value['categories']) != len(value['failed'])
                or not isinstance(value['lines'], list) or len(value['lines']) != len(value['failed'])
                or any(type(n) is not int or not 0 <= n <= 5000 for n in value['lines'])
                or any(c not in ('task', 'request', 'timeout', 'timeout-value', 'timeout-element',
                                 'timeout-network', 'uncaught', 'assertion', 'other')
                       for c in value['categories'])):
            return [], [], []
        return value['failed'], value['categories'], value['lines']
    except (OSError, ValueError, TypeError, KeyError):
        return [], [], []


def command_prefix(argv):
    if argv[0] == 'yarn' and os.environ.get('UI_YARN_JS'):
        require(os.environ.get('GITHUB_ACTIONS') != 'true', 'CI must use the configured yarn executable')
        return ['node', os.environ['UI_YARN_JS'], *argv[1:]]
    return argv


def terminate(proc, interrupt=False):
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        # Only this Popen-owned tree, never a port/name/global process search.
        subprocess.run(
            ['taskkill.exe', '/PID', str(proc.pid), '/T', '/F'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGINT if interrupt else signal.SIGTERM)
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait(timeout=20)


def run(argv, log, *, cwd=ROOT, timeout=None):
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    flags = (
        {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == 'nt'
        else {'start_new_session': True}
    )
    with log.open('xb') as stream:
        proc = subprocess.Popen(
            command_prefix(argv), cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, env=os.environ, **flags
        )
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate(proc, interrupt=True)
            return 124, time.monotonic() - start
        except BaseException:
            terminate(proc)
            raise
    return code, time.monotonic() - start


def export_receipt(ctx, logical, out, records, success, build=None):
    from ui_dependencies import evidence

    verify_context(ctx)
    out = Path(out)
    require(
        len(records) <= 128
        and sum(r.get('report', {}).get('size', 0) for r in records if r.get('report')) <= REPORT_UNION_LIMIT,
        'Evidence union budget exceeded',
    )
    receipt = {
        'schema': 'ui-shard-receipt/v1',
        'context': ctx,
        'logical': logical,
        'status': 'success' if success else 'failed',
        'calls': records,
        'build': build,
        'diagnostics': {
            'policy': 'structured-only',
            'raw_logs': 'private_not_archived',
            'screenshots_video': 'generated_per_original_config_not_archived',
            'dropped_private_material': True,
            'dependency_cache': evidence(ctx),
        },
    }
    write_json(out / 'receipt.json', public_evidence(receipt), RECEIPT_LIMIT)
    from ui_artifacts import prepare_export

    prepare_export(out, receipt)
    verify_context(ctx)
    return receipt


def prepare(ctx, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    (out / 'reports').mkdir()
    private = out.parent / (out.name + '-private')
    private.mkdir(exist_ok=False)
    # No cached Nx result is accepted as a fresh test execution.
    os.environ.update(NX_DAEMON='false', NX_NO_CLOUD='true', NX_SKIP_NX_CACHE='true', NX_SKIP_REMOTE_CACHE='true')
    expected = inventory()['prepare']
    records = []
    success = True
    for item in expected:
        name = item['id']
        raw = private / (name + '.json')
        xml = private / (name + '.xml')
        actual = prepare_commands(item, private)
        code = 0
        duration = 0
        for n, cmd in enumerate(actual):
            code, elapsed = run(cmd, private / f'{name}-{n}.log', cwd=ROOT / 'web', timeout=25 * 60)
            duration += elapsed
            if code:
                break
        report = {'schema': 'ui-test-report/v1', 'kind': item['kind'], 'call': item, 'exit_code': code, 'tests': []}
        valid = code == 0
        try:
            if item['kind'] == 'jest':
                report['tests'] = normalize_jest(read_json(raw))
            elif item['kind'] == 'node':
                with xml.open('rb') as f:
                    report['tests'] = normalize_node_xml(f.read(REPORT_LIMIT + 1))
            validate_report(report, item)
        except (ValueError, KeyError, OSError, TypeError):
            valid = False
        relative = f'reports/{name}.json'
        try:
            public_evidence(report)
        except ValueError:
            valid = False
            report = {'schema': 'ui-test-report/v1', 'kind': item['kind'], 'call': item, 'exit_code': 1, 'tests': []}
        write_json(out / relative, report)
        records.append(
            {
                'id': name,
                'call': item,
                'actual_commands': normalize_commands(actual, private),
                'status': 'success' if valid else 'failed',
                'seconds': duration,
                'report': report_reference(out, relative),
            }
        )
        success = success and valid
        print('UI_PREPARE', name, 'SUCCESS' if valid else 'FAILED', flush=True)
    build = None
    if success:
        verify_context(ctx)
        from ui_artifacts import pack_build

        manifest = pack_build(ctx, out.parent / (out.name + '-build'))
        build = {
            'manifest_sha256': file_hash(out.parent / (out.name + '-build') / 'manifest.json'),
            'producer_attempt': ctx['attempt'],
            'archive_sha256': manifest['archive_sha256'],
        }
    export_receipt(ctx, 'prepare', out, records, success, build)
    return success


def management(command, log):
    return run([sys.executable, str(ROOT / 'label_studio/manage.py'), *command], log, timeout=10 * 60)[0]


def validate_database_owner(ctx, private, *, fresh=True):
    from ui_server import isolation

    scope = isolation()
    second_review = scope.get('shard') in ('second-review','review-audit')
    port = os.environ.get('POSTGRE_PORT', '')
    require(port.isdecimal() and 1024 <= int(port) <= 65535, 'Synthetic PostgreSQL port invalid')
    marker = None
    if ctx['environment'] == 'github':
        require(
            os.environ.get('GITHUB_ACTIONS') == 'true'
            and port == '5432'
            and os.environ['POSTGRE_NAME'] == ('ui_ci_second_review_audit_browser' if scope.get('shard')=='review-audit' else 'ui_ci_second_review_browser' if second_review else 'ui_ci_browser')
            and Path(private).parent.resolve() == Path(os.environ['RUNNER_TEMP']).resolve(),
            "Only this job's ephemeral service is supported",
        )
    if second_review:
        require(
            os.environ['POSTGRE_HOST'] == '127.0.0.1'
            and os.environ['POSTGRE_NAME'].startswith('ui_ci_second_review_'),
            'Second review requires its dedicated loopback database',
        )
    if ctx['environment'] != 'github':
        marker_path = Path(os.environ['UI_NATIVE_CLUSTER_FILE'])
        require(
            marker_path.resolve() == Path(private).parent.resolve() / 'native-cluster.json',
            'Task-local cluster marker required',
        )
        marker = read_json(marker_path)
        require(
            marker.get('host') == '127.0.0.1'
            and marker.get('port') == int(port)
            and Path(marker['data_directory']).resolve().is_relative_to(Path(private).parent.resolve()),
            'Native cluster is not owned by this acceptance run',
        )
    import psycopg

    with psycopg.connect(
        host=os.environ['POSTGRE_HOST'],
        port=int(port),
        dbname=os.environ['POSTGRE_NAME'],
        user=os.environ['POSTGRE_USER'],
        password=os.environ['POSTGRE_PASSWORD'],
        connect_timeout=5,
        options='-c default_transaction_read_only=on',
    ) as db:
        require(
            db.execute('SELECT current_database()').fetchone()[0] == scope['database'], 'Database identity mismatch'
        )
        if marker:
            actual_dir = db.execute('SHOW data_directory').fetchone()[0]
            identity = str(db.execute('SELECT system_identifier FROM pg_control_system()').fetchone()[0])
            require(
                Path(actual_dir).resolve() == Path(marker['data_directory']).resolve()
                and identity == marker['system_identifier'],
                'Native server identity mismatch',
            )
        if second_review:
            directory, actual_port, identifier = db.execute(
                "SELECT current_setting('data_directory'), current_setting('port'), system_identifier FROM pg_control_system()"
            ).fetchone()
            require(int(actual_port) == int(port), 'Second-review server port mismatch')
            bootstrap = {
                'schema': 'second-review-owned-native/v1',
                'database': scope['database'],
                'port': int(actual_port),
                'cluster': directory,
                'system_identifier': str(identifier),
                'fresh_public_tables': 0,
            }
        if fresh:
            count = db.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
            ).fetchone()[0]
            require(count == 0, 'Fresh owned database required; never reset a retained schema')
    if second_review:
        path = Path(private) / 'second-review-bootstrap.json'
        if fresh:
            write_json(path, bootstrap, 4096)
        else:
            require(read_json(path, 4096) == bootstrap, 'Second-review bootstrap identity changed')
    proof = {
        'database': scope['database'],
        'fresh_public_tables': 0,
        'environment': ctx['environment'],
        'port': int(port),
    }
    if fresh:
        write_json(Path(private) / 'database-owner-proof.json', proof)
    else:
        require(read_json(Path(private) / 'database-owner-proof.json') == proof, 'Missing fresh ownership proof')


def compile_catalogs(ctx, private):
    if os.name == 'nt':
        require(ctx['environment'] == 'local', 'Windows runtime is local acceptance only')
    code, _ = run(
        [sys.executable, str(ROOT / 'scripts/ci/ui_catalogs.py'), '--private', str(private)],
        Path(private) / 'compilemessages.log',
        timeout=120,
    )
    require(code == 0, 'Private catalog compilation failed')
    from ui_catalogs import validate_catalogs

    validate_catalogs(private)


def seed(ctx, shard, private):
    from ui_server import validate_fixture

    private = Path(private)
    require(not os.environ.get('ENTERPRISE_E2E_CONTAINER'), 'Container mode forbidden')
    require(os.environ.get('TEST_ENVIRONMENT') == '1', 'Synthetic TEST_ENVIRONMENT required')
    require(os.environ.get('POSTGRE_NAME', '').startswith('ui_ci_'), 'Synthetic database namespace required')
    os.environ['DJANGO_SETTINGS_MODULE'] = 'core.settings.label_studio'
    require(
        Path(os.environ['BASE_DATA_DIR']).resolve().is_relative_to(private.resolve()),
        'Synthetic data must be job-private',
    )
    scope = {
        'workspace': str(ROOT.resolve()),
        'database': os.environ['POSTGRE_NAME'],
        'port': os.environ['POSTGRE_PORT'],
        'data_dir': os.environ['BASE_DATA_DIR'],
        'context': ctx,
        'shard': shard,
    }
    os.environ['UI_SCOPE_FILE'] = str(private / 'scope.json')
    write_json(os.environ['UI_SCOPE_FILE'], scope)
    # Check host/backend/port/ownership and freshness BEFORE the first database write.
    validate_database_owner(ctx, private)
    compile_catalogs(ctx, private)
    commands = [['migrate', '--noinput'], ['collectstatic', '--noinput']]
    if shard in ('second-review','review-audit'):
        os.environ['SECOND_REVIEW_E2E_OWNERSHIP_FILE'] = str(private / 'second-review-bootstrap.json')
        commands += [['seed_review_audit_e2e' if shard=='review-audit' else 'seed_second_review_e2e']]
    else:
        commands += (
            [['seed_review_confidence_e2e']]
            if shard == 'confidence'
            else [
                ['seed_enterprise_e2e'],
                ['seed_review_batches_e2e'],
                ['seed_classification_e2e'],
                ['seed_classification_batches_e2e'],
            ]
        )
    for n, cmd in enumerate(commands):
        require(management(cmd, private / f'seed-{n}.log') == 0, 'Synthetic preparation failed')
    fixtures = ROOT / 'web/apps/labelstudio-e2e'
    if shard == 'confidence':
        validate_fixture(read_json(fixtures / '.review-confidence-e2e.json'))
    if shard == 'second-review':
        from ui_second_review import validate_fixture as validate_second_review

        validate_second_review(read_json(fixtures / '.second-review-e2e.json'))
    if shard == 'review-audit':
        from ui_audit import validate_fixture as validate_audit
        validate_audit(read_json(fixtures / '.review-audit-e2e.json'))
    secrets = []
    for filename in (
        ('.review-audit-e2e.json',)
        if shard == 'review-audit'
        else ('.second-review-e2e.json',)
        if shard == 'second-review'
        else ('.review-confidence-e2e.json',)
        if shard == 'confidence'
        else (
            '.enterprise-e2e.json',
            '.review84-e2e.json',
            '.classification81-e2e.json',
            '.classification-batches81-e2e.json',
        )
    ):
        data=read_json(fixtures / filename)
        value = (data['supervision'] if shard=='review-audit' else data).get('password')
        if isinstance(value, str) and '\n' not in value and '\r' not in value:
            secrets.append(value)
    os.environ['UI_SECRET_FILE'] = str(private / 'secret-filter.json')
    write_json(os.environ['UI_SECRET_FILE'], secrets, 16 * 1024)
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        # Mask generated values before Cypress can print request failures.
        for value in secrets:
            print('::add-mask::' + value, flush=True)


def verify_installed_build(ctx, manifest, destination=None):
    from ui_artifacts import DIST
    from ui_common import regular_tree, same_source

    require(manifest.get('schema') == 'ui-build/v1', 'Installed build schema invalid')
    same_source(ctx, manifest['context'])
    require(regular_tree(destination or DIST) == manifest['files'], 'Installed build changed during browser execution')


def browser(ctx, shard, out, build_identity, build_manifest):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    (out / 'reports').mkdir()
    private = out.parent / (out.name + '-private')
    private.mkdir(exist_ok=False)
    os.environ['BASE_DATA_DIR'] = str(private / 'django-data')
    verify_context(ctx)
    verify_installed_build(ctx, build_manifest)
    seed(ctx, shard, private)
    port = int(os.environ.get('UI_PORT', '8080'))
    os.environ['UI_BASE_URL'] = f'http://127.0.0.1:{port}'
    args = [sys.executable, str(ROOT / 'scripts/ci/ui_server.py'), '--port', str(port)]
    if shard == 'confidence':
        args.append('--confidence')
    if shard == 'second-review':
        args.append('--second-review')
    if shard == 'review-audit':
        args.append('--review-audit')
    flags = (
        {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == 'nt'
        else {'start_new_session': True}
    )
    records = []
    success = False
    with (private / 'server.log').open('xb') as log:
        proc = subprocess.Popen(args, cwd=ROOT, env=os.environ, stdout=log, stderr=subprocess.STDOUT, **flags)
        try:
            ready = False
            for _ in range(90):
                if proc.poll() is not None:
                    break
                try:
                    for url in ('/health/', '/react-app/runtime.js', '/react-app/vendor.js', '/react-app/main.js'):
                        with urllib.request.urlopen(os.environ['UI_BASE_URL'] + url, timeout=2) as response:
                            require(response.status == 200, 'Readiness failed')
                    ready = True
                    break
                except (OSError, ValueError):
                    time.sleep(1)
            require(ready, 'Synthetic server not ready')
            success = True
            for index, call in enumerate(calls(shard)):
                verify_context(ctx)
                verify_installed_build(ctx, build_manifest)
                if call['reset']:
                    require(
                        management(['seed_enterprise_e2e'], private / f'reset-{index}.log') == 0,
                        'Synthetic reset failed',
                    )
                input_path = private / f'call-{index}.json'
                write_json(input_path, call)
                relative = f"reports/{call['id']}.json"
                diagnostic = private / f'cypress-failure-{index}.json'
                code, elapsed = run(
                    ['node', str(ROOT / 'scripts/ci/run_cypress.cjs'), str(input_path), str(out / relative), str(diagnostic)],
                    private / f'browser-{index}.log',
                    cwd=ROOT / 'web',
                    timeout=call['timeout_seconds'],
                )
                valid = code == 0
                try:
                    validate_report(read_json(out / relative), call)
                except (OSError, ValueError, KeyError, TypeError):
                    valid = False
                records.append(
                    {
                        'id': call['id'],
                        'call': call,
                        'status': 'success' if valid else 'failed',
                        'seconds': elapsed,
                        'report': report_reference(out, relative) if (out / relative).is_file() else None,
                    }
                )
                print('UI_BROWSER', call['id'], 'SUCCESS' if valid else 'FAILED', flush=True)
                if not valid:
                    indices, categories, lines = cypress_failure_indices(diagnostic)
                    if indices:
                        print('UI_BROWSER_FAILED_TEST_POSITIONS', call['id'], ','.join(map(str, indices)), flush=True)
                        print('UI_BROWSER_FAILURE_CATEGORIES', call['id'], ','.join(categories), flush=True)
                        print('UI_BROWSER_FAILURE_LINES', call['id'], ','.join(map(str, lines)), flush=True)
                if not valid:
                    success = False
                    break
            # Includes the last spec, not only the boundary before the next one.
            verify_context(ctx)
            verify_installed_build(ctx, build_manifest)
        finally:
            terminate(proc)
    export_receipt(ctx, shard, out, records, success, build_identity)
    return success
