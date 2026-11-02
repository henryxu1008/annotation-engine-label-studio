"""Fail closed over real job executions and immutable, explicitly selected artifacts."""
import math
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from ui_artifacts import evidence_files, unpack_evidence_zip
from ui_common import (
    RECEIPT_LIMIT,
    REPORT_LIMIT,
    REPORT_UNION_LIMIT,
    artifact_name,
    digest,
    file_hash,
    integer,
    loads,
    public_evidence,
    read_json,
    require,
    same_source,
    sha256,
    write_json,
)
from ui_inventory import JOB_NAMES, calls, inventory, prepare_commands
from ui_results import validate_report


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require(urllib.parse.urlparse(newurl).scheme == 'https', 'Non-HTTPS artifact redirect')
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        if result:
            result.remove_header('Authorization')
            result.remove_header('authorization')
        return result


class GitHub:
    def __init__(self, ctx):
        require(ctx['environment'] == 'github', 'Local receipts cannot satisfy a GitHub required check')
        self.ctx = ctx
        token = os.environ.get('GH_TOKEN', '')
        require(token and '\n' not in token and '\r' not in token, 'Actions read token missing')
        self.headers = {
            'Authorization': 'Bearer ' + token,
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        }
        self.opener = urllib.request.build_opener(SafeRedirect())

    def read(self, path, limit=REPORT_LIMIT):
        require(path.startswith('actions/') and '://' not in path, 'Only fixed Actions read paths allowed')
        url = f"https://api.github.com/repos/{self.ctx['repository']}/{path}"
        request = urllib.request.Request(url, headers=self.headers, method='GET')
        with self.opener.open(request, timeout=30) as response:
            require(response.status == 200, 'Actions read failed')
            data = response.read(limit + 1)
            require(len(data) <= limit, 'Actions response byte budget exceeded')
            return data

    def json(self, path):
        return loads(self.read(path))

    def pages(self, path, key, cap):
        rows = []
        total = None
        page = 1
        while total is None or len(rows) < total:
            value = self.json(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
            n = value.get('total_count')
            require(integer(n, 0, cap) and (total is None or total == n), 'Actions inventory changed/exceeded budget')
            total = n
            batch = value.get(key)
            require(
                isinstance(batch, list) and len(batch) <= 100 and (batch or not total), 'Actions pagination invalid'
            )
            rows.extend(batch)
            require(len(rows) <= total, 'Actions pagination duplicated rows')
            page += 1
        require(len({r['id'] for r in rows}) == len(rows), 'Duplicate Actions IDs')
        return rows

    def jobs(self):
        return self.pages(f"actions/runs/{self.ctx['run_id']}/jobs?filter=all", 'jobs', 256)

    def artifacts(self):
        return self.pages(f"actions/runs/{self.ctx['run_id']}/artifacts", 'artifacts', 256)

    def run(self):
        return self.json(f"actions/runs/{self.ctx['run_id']}")

    def zip(self, identifier):
        require(integer(identifier, 1), 'Artifact ID invalid')
        return self.read(f'actions/artifacts/{identifier}/zip', REPORT_UNION_LIMIT + RECEIPT_LIMIT + 1024**2)


def verify_run(ctx, run):
    require(
        ctx.get('cache_mode') in ('warm', 'cold')
        and (ctx['cache_mode'] == 'warm' or ctx['event'] == 'workflow_dispatch'),
        'Dependency cache mode/event mismatch',
    )
    require(
        run.get('id') == ctx['run_id']
        and run.get('event') == ctx['event']
        and run.get('head_sha') == ctx['metadata_head']
        and run.get('run_attempt') == ctx['attempt']
        and run.get('name') == ctx['workflow'],
        'Workflow run identity/attempt mismatch',
    )
    require(run.get('repository', {}).get('full_name') == ctx['repository'], 'Workflow repository mismatch')
    if ctx['event'] == 'push':
        require(run.get('head_branch') == 'main', 'Push branch mismatch')


def job_time(value):
    require(isinstance(value, str) and len(value) <= 64, 'Job timestamp missing')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None, 'Job timestamp timezone missing')
    return parsed.astimezone(timezone.utc)


def job_times(job):
    created, start, end = (job_time(job.get(k)) for k in ('created_at', 'started_at', 'completed_at'))
    require(start <= end, 'Job execution interval invalid')
    require(created <= start or created > end, 'Job creation overlaps an unexplained execution')
    return created, start, end


def execution_fingerprint(job):
    """Physical execution fields; IDs, attempt and creation describe API views."""
    _, start, end = job_times(job)
    require(
        integer(job.get('runner_id'), 1)
        and isinstance(job.get('runner_name'), str)
        and 0 < len(job['runner_name']) <= 256,
        'Runner execution identity missing',
    )
    require(
        'runner_group_id' in job and (job['runner_group_id'] is None or integer(job['runner_group_id'])),
        'Runner group metadata invalid',
    )
    # Hosted API copies have been observed changing only group sentinel 0 to null.
    group = job['runner_group_id']
    group = 0 if group is None else group
    labels = job.get('labels')
    require(
        isinstance(labels, list)
        and 0 < len(labels) <= 32
        and all(isinstance(v, str) and 0 < len(v) <= 128 for v in labels)
        and len(set(labels)) == len(labels),
        'Runner labels incomplete',
    )
    steps = job.get('steps')
    require(isinstance(steps, list) and 0 < len(steps) <= 256, 'Execution steps missing')
    normalized = []
    previous = 0
    for step in steps:
        require(
            isinstance(step, dict)
            and set(step) == {'number', 'name', 'status', 'conclusion', 'started_at', 'completed_at'},
            'Execution step fields incomplete',
        )
        require(
            integer(step['number'], previous + 1, 10000)
            and isinstance(step['name'], str)
            and 0 < len(step['name'].encode('utf-8')) <= 1024
            and step['status'] == 'completed'
            and step['conclusion'] in ('success', 'skipped'),
            'Execution step identity/status invalid',
        )
        previous = step['number']
        left, right = step['started_at'], step['completed_at']
        if left is None and right is None:
            require(step['conclusion'] == 'skipped', 'Executed step timestamps missing')
        else:
            left, right = job_time(left), job_time(right)
            require(start <= left <= right <= end, 'Execution step interval invalid')
            left, right = left.isoformat(), right.isoformat()
        normalized.append({**step, 'started_at': left, 'completed_at': right})
    return digest(
        {
            'run_id': job['run_id'],
            'head_sha': job['head_sha'],
            'name': job['name'],
            'status': job['status'],
            'conclusion': job['conclusion'],
            'started_at': start.isoformat(),
            'completed_at': end.isoformat(),
            'runner_id': job['runner_id'],
            'runner_name': job['runner_name'],
            'runner_group_id': group,
            'labels': sorted(labels),
            'steps': normalized,
            **{k: job.get(k) for k in ('workflow_name', 'head_branch', 'run_url', 'runner_group_name')},
        }
    )


def producer_for_view(view, history):
    """Resolve only a uniquely proven copied success, never an older green fallback."""
    require(
        view.get('status') == 'completed' and view.get('conclusion') == 'success',
        'Latest required execution not successful',
    )
    created, start, _ = job_times(view)
    fingerprint = execution_fingerprint(view)
    producer = view
    if created > start:
        candidates = []
        for older in history:
            if older['run_attempt'] >= view['run_attempt']:
                continue
            if older.get('status') != 'completed' or older.get('conclusion') != 'success':
                continue
            old_created, old_start, _ = job_times(older)
            if old_created <= old_start and execution_fingerprint(older) == fingerprint:
                candidates.append(older)
        require(len(candidates) == 1, 'Copied success lacks a unique actual producer')
        producer = candidates[0]
        for middle in history:
            if not producer['run_attempt'] < middle['run_attempt'] < view['run_attempt']:
                continue
            require(
                middle.get('status') == 'completed' and middle.get('conclusion') == 'success',
                'Intervening non-success blocks copied reuse',
            )
            mid_created, _, mid_end = job_times(middle)
            require(
                mid_created > mid_end and execution_fingerprint(middle) == fingerprint,
                'Intervening actual or unknown execution blocks copied reuse',
            )
    signature = digest(
        {
            'producer_id': producer['id'],
            'producer_attempt': producer['run_attempt'],
            'producer_created': job_time(producer['created_at']).isoformat(),
            'view_id': view['id'],
            'view_attempt': view['run_attempt'],
            'view_created': created.isoformat(),
            'producer_execution': execution_fingerprint(producer),
            'view_execution': fingerprint,
        }
    )
    return {**producer, '_view': view, '_mapping_signature': signature}


def latest_jobs(ctx, rows):
    require(isinstance(rows, list) and len(rows) <= 256, 'Job count exceeded')
    expected = set(JOB_NAMES.values())
    require(
        all(r.get('name') in expected | {'Current enterprise UI validation'} for r in rows), 'Unexpected logical job'
    )
    require(len({r.get('id') for r in rows}) == len(rows), 'Duplicate Actions job identity')
    latest = {}
    for logical, name in JOB_NAMES.items():
        found = [r for r in rows if r.get('name') == name]
        require(found, 'Missing logical job: ' + logical)
        for r in found:
            require(
                r.get('run_id') == ctx['run_id']
                and r.get('head_sha') == ctx['metadata_head']
                and integer(r.get('run_attempt'), 1, 20)
                and r['run_attempt'] <= ctx['attempt']
                and integer(r.get('id'), 1),
                'Job source/attempt invalid',
            )
        require(len({r['run_attempt'] for r in found}) == len(found), 'Ambiguous duplicate logical execution')
        attempt = max(r['run_attempt'] for r in found)
        newest = [r for r in found if r['run_attempt'] == attempt]
        require(len(newest) == 1, 'Ambiguous duplicate logical execution')
        r = newest[0]
        require(
            r.get('status') == 'completed' and r.get('conclusion') == 'success',
            'Latest required execution not successful: ' + logical,
        )
        latest[logical] = producer_for_view(r, found)
    return latest


def select_artifact(ctx, artifacts, kind, logical, job):
    producer = {**ctx, 'attempt': job['run_attempt']}
    found = [a for a in artifacts if a.get('name') == artifact_name(producer, kind, logical)]
    require(len(found) == 1, 'Missing/duplicate immutable artifact')
    a = found[0]
    require(integer(a.get('id'), 1) and a.get('expired') is False, 'Artifact identity/expiry invalid')
    require(a.get('workflow_run', {}).get('id', ctx['run_id']) == ctx['run_id'], 'Artifact belongs to another run')
    created = datetime.fromisoformat(a['created_at'].replace('Z', '+00:00'))
    start = datetime.fromisoformat(job['started_at'].replace('Z', '+00:00'))
    end = datetime.fromisoformat(job['completed_at'].replace('Z', '+00:00'))
    require(start <= created <= end, 'Artifact not produced by selected execution')
    return a


def validate_receipt(
    ctx, logical, job, folder, receipt, build_artifact_id, build_manifest_hash, prepare_attempt, archive_hash
):
    require(
        set(receipt) == {'schema', 'context', 'logical', 'status', 'calls', 'build', 'diagnostics'},
        'Receipt extra/missing fields',
    )
    public_evidence(receipt)
    require(
        receipt.get('schema') == 'ui-shard-receipt/v1'
        and receipt.get('logical') == logical
        and receipt.get('status') == 'success',
        'Receipt status/identity invalid',
    )
    same_source(ctx, receipt['context'])
    from ui_common import canonical
    from ui_dependencies import expected_evidence

    require(
        canonical(receipt.get('diagnostics', {}).get('dependency_cache'))
        == canonical(expected_evidence(ctx['cache_mode'])),
        'Dependency cache evidence invalid',
    )
    require(receipt['context']['attempt'] == job['run_attempt'], 'Receipt producer attempt mismatch')
    expected = inventory()['prepare'] if logical == 'prepare' else calls(logical)
    records = receipt.get('calls')
    require(isinstance(records, list) and len(records) == len(expected), 'Missing/extra invocation receipt')
    evidence_files(folder, receipt)
    used = 0
    for actual, wanted in zip(records, expected):
        require(
            actual.get('id') == wanted['id'] and actual.get('call') == wanted and actual.get('status') == 'success',
            'Invocation order/identity/status mismatch',
        )
        if logical == 'prepare':
            require(
                actual.get('actual_commands') == prepare_commands(wanted),
                'Actual prepare argv differs from reporter contract',
            )
        ref = actual['report']
        used += ref['size']
        require(used <= REPORT_UNION_LIMIT, 'Report union budget exceeded')
        report = read_json(Path(folder) / ref['path'])
        public_evidence(report)
        validate_report(report, wanted)
        seconds = actual.get('seconds')
        require(
            type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0, 'Invocation duration invalid'
        )
        if logical != 'prepare' and wanted['timeout_seconds']:
            require(seconds <= wanted['timeout_seconds'] + wanted['kill_grace_seconds'], 'Per-spec limit exceeded')
    build = receipt.get('build')
    keys = {'manifest_sha256', 'producer_attempt', 'archive_sha256'} | (
        {'artifact_id'} if logical != 'prepare' else set()
    )
    require(
        isinstance(build, dict)
        and set(build) == keys
        and sha256(build_manifest_hash)
        and sha256(build.get('archive_sha256'))
        and build.get('manifest_sha256') == build_manifest_hash
        and build.get('archive_sha256') == archive_hash
        and type(build.get('producer_attempt')) is int
        and build['producer_attempt'] == prepare_attempt,
        'Build receipt mismatch',
    )
    if logical != 'prepare':
        require(build.get('artifact_id') == build_artifact_id, 'Consumer used another artifact ID')
    return {
        'logical': logical,
        'job_id': job['id'],
        'attempt': job['run_attempt'],
        'url': job['html_url'],
        'receipt_sha256': file_hash(Path(folder) / 'receipt.json', RECEIPT_LIMIT),
        'view_job_id': job['_view']['id'],
        'view_attempt': job['_view']['run_attempt'],
        'copied_success': job['_view']['id'] != job['id'],
        'producer_runner_group_id': job['runner_group_id'],
        'view_runner_group_id': job['_view']['runner_group_id'],
    }


def aggregate(ctx, out, client=None):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    rows = []
    audit = {'stage': 'run'}
    try:
        return _aggregate(ctx, out, client, lambda name: audit.update(stage=name), rows)
    except Exception:
        # Fixed error code and narrow job scalars only; never raw API bodies/headers/test errors.
        safe = []
        known = set(JOB_NAMES.values()) | {'Current enterprise UI validation'}
        for j in rows[:256]:
            if not isinstance(j, dict):
                safe.append(
                    {'id': None, 'name': 'invalid', 'attempt': None, 'status': 'invalid', 'conclusion': 'invalid'}
                )
                continue
            safe.append(
                {
                    'id': j.get('id') if integer(j.get('id'), 1) else None,
                    'name': j.get('name') if isinstance(j.get('name'), str) and j['name'] in known else 'unknown',
                    'attempt': j.get('run_attempt') if integer(j.get('run_attempt'), 1, 20) else None,
                    'status': j.get('status')
                    if j.get('status') in ('queued', 'in_progress', 'completed')
                    else 'invalid',
                    'conclusion': j.get('conclusion')
                    if j.get('conclusion')
                    in ('success', 'failure', 'cancelled', 'timed_out', 'skipped', 'neutral', None)
                    else 'invalid',
                }
            )
        write_json(
            out / 'result.json',
            {
                'schema': 'ui-aggregate/v1',
                'context': ctx,
                'status': 'failed',
                'code': 'UI_AGGREGATE_EVIDENCE_REJECTED',
                'stage': audit['stage'],
                'jobs': safe,
            },
            128 * 1024,
        )
        raise


def _aggregate(ctx, out, client, progress, rows):
    api = client or GitHub(ctx)
    verify_run(ctx, api.run())
    progress('jobs')
    rows.extend(api.jobs())
    jobs = latest_jobs(ctx, rows)
    progress('artifacts')
    artifacts = api.artifacts()
    require(len(artifacts) <= 256, 'Artifact inventory budget exceeded')
    build = select_artifact(ctx, artifacts, 'build', 'prepare', jobs['prepare'])
    records = []
    manifest_hash = None
    archive_hash = None
    total = 0
    for logical, job in jobs.items():
        progress('receipts/' + logical)
        artifact = select_artifact(ctx, artifacts, 'evidence', logical, job)
        folder = out / logical
        receipt = unpack_evidence_zip(api.zip(artifact['id']), folder)
        if logical == 'prepare':
            manifest_hash = receipt.get('build', {}).get('manifest_sha256')
            archive_hash = receipt.get('build', {}).get('archive_sha256')
        record = validate_receipt(
            ctx,
            logical,
            job,
            folder,
            receipt,
            build['id'],
            manifest_hash,
            jobs['prepare']['run_attempt'],
            archive_hash,
        )
        record['artifact_id'] = artifact['id']
        records.append(record)
        total += sum(c['report']['size'] for c in receipt['calls'])
        require(total <= 160 * 1024**2, 'Aggregate report union exceeded')
    # A rerun started while we downloaded must not let this older execution go green.
    progress('final-current')
    verify_run(ctx, api.run())
    require(
        {k: v['_mapping_signature'] for k, v in latest_jobs(ctx, api.jobs()).items()}
        == {k: v['_mapping_signature'] for k, v in jobs.items()},
        'Latest execution changed during aggregation',
    )
    result = {
        'schema': 'ui-aggregate/v1',
        'context': ctx,
        'status': 'success',
        'records': records,
        'browser_invocations': 24,
        'build_artifact_id': build['id'],
        'build_manifest_sha256': manifest_hash,
    }
    write_json(out / 'result.json', result)
    return result
