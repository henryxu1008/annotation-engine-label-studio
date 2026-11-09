"""Future real HTTP/RQ audit chain. No enqueue/generate patch or sync fallback.

Receipts are narrow observations, never whole-case/matrix PASS. UNKNOWN intent
stays occupied; this executor does not create another capture to recover it.
"""
import hmac
import math
import os
import time
from pathlib import Path
from uuid import uuid4

import ls84_contract as c

JSON_LIMIT = 512 * 1024
ARTIFACT_LIMIT = 16 * 1024 * 1024
CYCLE_TIMEOUT = 1850  # New standalone local budget; never inserted into an existing CI/browser call.
AUDIT_JOBS = ('data_export.review_audit.generate', 'data_export.review_audit.generate_bound_v1')


def evidence_worker_class(m, receipt, base_worker, stop_requested):
    """Pure class factory permits offline admission/timeout tests without importing RQ/Django."""
    root = Path(m['run_root'])

    class EvidenceWorker(base_worker):
        def dequeue_job_and_maintain_ttl(self, timeout, max_idle_time=None):
            try:
                remaining = c.require_live_authorization(receipt)
            except c.SupportError:
                raise stop_requested() from None
            return super().dequeue_job_and_maintain_ttl(timeout, max_idle_time=max(1, math.ceil(remaining)))

        def perform_job(self, job, queue):
            c.require_live_authorization(receipt)
            # Called in the native work horse, observing the original production function.
            if job.func_name in ('data_export.approved.generate', 'data_export.snapshots.generate'):
                # Same owned queue: preserve existing export/snapshot implementations, without
                # claiming their separate #79/#80 acceptance or generating audit proof for them.
                return super().perform_job(job, queue)
            c.require(job.func_name in AUDIT_JOBS and len(job.args) == 2
                      and all(c.uuid(a) for a in job.args), 'UNEXPECTED_SYNTHETIC_JOB')
            observations = dict(receipt, schema='ls84-rq-job/v1', job_id=job.id,
                                package_id=job.args[0], attempt_token=job.args[1],
                                worker_name=self.name, worker_pid=receipt['pid'], workhorse_pid=os.getpid())
            c.require(observations['workhorse_pid'] != observations['worker_pid'], 'NATIVE_FORK_REQUIRED')
            c.write_new(root / 'reports' / f'rq-job-{job.id}-{uuid4()}.json', observations)
            return super().perform_job(job, queue)

    return EvidenceWorker


def work(m, receipt):
    from django.db import connections
    from django_rq import get_worker
    from rq import Worker
    from rq.exceptions import StopRequested

    # Reuse django-rq's queue/job setup and RQ's native fork/timeout machinery.
    connections.close_all()
    worker = get_worker('approved_exports', worker_class=evidence_worker_class(m, receipt, Worker, StopRequested),
                        name='ls84-' + m['run_id'])
    worker.work(burst=False, with_scheduler=False, logging_level='ERROR')


class HTTP:
    def __init__(self, m, token, web_receipt, secret):
        import requests

        self.base = f'http://127.0.0.1:{m["http_port"]}'
        self.session = requests.Session()
        self.session.trust_env = False
        self.token, self.web_receipt, self.secret = token, web_receipt, secret
        self.deadline = None

    def check_time(self):
        c.require_live_authorization(self.web_receipt)
        c.require(self.deadline is not None and time.monotonic() <= self.deadline, 'CYCLE_BUDGET_EXCEEDED')

    def response(self, method, path, *, body=None, key=None):
        self.check_time()
        self.attest()
        c.require(isinstance(path, str) and path.startswith('/api/projects/') and not path.startswith('//')
                  and '..' not in path and '?' not in path and '#' not in path, 'UNSAFE_HTTP_PATH')
        headers = {'Idempotency-Key': key} if key else {}
        headers['Authorization'] = 'Token ' + self.token
        remaining = max(0.001, self.deadline - time.monotonic())
        return self.session.request(method, self.base + path, json=body, headers=headers,
                                    timeout=(min(3, remaining), min(5, remaining)), stream=True, allow_redirects=False)

    def attest(self):
        from ls84_web import ATTESTATION_PATH, proof

        self.check_time()
        nonce = str(uuid4())
        # No token/cookie/business request goes to an unproved listener.
        with self.session.get(self.base + ATTESTATION_PATH, params={'challenge': nonce}, timeout=(3, 5),
                              stream=True, allow_redirects=False) as response:
            c.require(response.status_code == 200, 'WEB_ATTESTATION_REQUIRED')
            raw = bytearray()
            for chunk in response.iter_content(4096):
                self.check_time()
                raw.extend(chunk)
                c.require(len(raw) <= 4096, 'WEB_ATTESTATION_TOO_LARGE')
        payload = c.decode(bytes(raw))
        signature = payload.pop('proof', None)
        expected = {key: self.web_receipt[key] for key in ('run_id', 'pid', 'manifest_sha256', 'fixture_sha256',
                    'source_digest', 'configuration_sha256', 'authorization_sha256', 'authorization_expires_at')}
        expected['challenge'] = nonce
        c.require(payload == expected and isinstance(signature, str)
                  and hmac.compare_digest(signature, proof(self.secret, payload)), 'WEB_PROCESS_CONFIG_MISMATCH')
        self.check_time()

    def json(self, method, path, *, body=None, key=None, expected=200):
        with self.response(method, path, body=body, key=key) as response:
            c.require(response.status_code == expected, 'HTTP_STATUS_REJECTED')
            raw = bytearray()
            for chunk in response.iter_content(16384):
                self.check_time()
                raw.extend(chunk)
                c.require(len(raw) <= JSON_LIMIT, 'HTTP_JSON_TOO_LARGE')
            self.check_time()
            return c.decode(bytes(raw))

    def download(self, path, destination):
        with self.response('GET', path) as response:
            c.require(response.status_code == 200, 'DOWNLOAD_STATUS_REJECTED')
            fd = os.open(c.safe_path(destination, exists=False), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                count = 0
                for chunk in response.iter_content(65536):
                    self.check_time()
                    count += len(chunk)
                    c.require(count <= ARTIFACT_LIMIT, 'DOWNLOAD_TOO_LARGE')
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
                self.check_time()

    def close(self):
        self.session.close()


def matching_processes(m, receipt):
    root = Path(m['run_root']) / 'reports'
    result = {}
    for command in ('web', 'worker'):
        matches = []
        for path in root.glob(command + '-process-*.json'):
            row = c.decode(c.read_file(path))
            keys = ('run_id', 'source_digest', 'manifest_sha256', 'fixture_sha256', 'instance_id',
                    'postgres_system_identifier', 'redis_run_id', 'queue', 'configuration_sha256',
                    'authorization_sha256', 'authorization_expires_at')
            if all(row.get(k) == receipt[k] for k in keys) and row.get('command') == command:
                c.require(c.integer(row.get('pid')) and row['pid'] != os.getpid(), 'SEPARATE_PROCESS_REQUIRED')
                matches.append(row)
        c.require(len(matches) == 1, 'ONE_MATCHING_PROCESS_REQUIRED')
        result[command] = matches[0]
    c.require(result['web']['pid'] != result['worker']['pid'], 'SEPARATE_PROCESS_REQUIRED')
    return result


def verify_chain(m, package, receipt, processes, *, job, evidence):
    """Pure association checks, usable by offline tests; it never runs a job."""
    keys = ('run_id', 'source_digest', 'manifest_sha256', 'fixture_sha256', 'instance_id',
            'postgres_system_identifier', 'redis_run_id', 'queue', 'configuration_sha256',
            'authorization_sha256', 'authorization_expires_at')
    token = str(package.token)
    expected_job = f'audit-{package.pk}-{token}'
    c.require(job.id == expected_job and job.func_name in AUDIT_JOBS
              and list(job.args) == [str(package.pk), token] and job.worker_name == 'ls84-' + m['run_id']
              and str(job.get_status(refresh=True)) in ('finished', 'JobStatus.FINISHED'), 'RQ_JOB_NOT_PROVEN')
    c.require(all(evidence.get(k) == receipt[k] for k in keys)
              and evidence.get('job_id') == expected_job and evidence.get('package_id') == str(package.pk)
              and evidence.get('attempt_token') == token and evidence.get('worker_name') == job.worker_name
              and evidence.get('worker_pid') == processes['worker']['pid']
              and c.integer(evidence.get('workhorse_pid'))
              and evidence['workhorse_pid'] not in (os.getpid(), processes['web']['pid'], processes['worker']['pid']),
              'WORKER_EVIDENCE_MISMATCH')


def audit_cycle(m, value, receipt):
    from data_export import audit_format
    from data_export.audit_models import AuditArtifact, AuditAttempt, AuditPackage
    from redis import Redis
    from rq.job import Job
    from users.models import User

    root = Path(m['run_root'])
    processes = matching_processes(m, receipt)
    # This exclusive file fences two executors and occupies any UNKNOWN operation.
    c.write_new(root / 'reports/audit-cycle-intent.json', {'schema': 'ls84-cycle-intent/v1',
                'run_id': m['run_id'], 'source_digest': m['source_digest'], 'state': 'INTENT_RECORDED'})
    actor = User.objects.get(pk=value['actors']['auditor']['id'])
    client = HTTP(m, actor.get_token().key, processes['web'], os.environ['SECRET_KEY'])
    try:
        for locale, info in value['supervision']['projects'].items():
            started = time.monotonic()
            client.deadline = started + CYCLE_TIMEOUT
            base = f'/api/projects/{info["id"]}/review-audit-packages/'
            preview = client.json('POST', base + 'preview/', body={'review_batch_id': info['batch_id']})
            body = {'review_batch_id': info['batch_id'], 'profile': 'review-audit-redacted/v1',
                    'expected_batch_binding_hash': preview['batch_binding_hash'],
                    'expected_capture_preview_hash': preview['capture_preview_hash']}
            key = str(uuid4())
            # Persist exact key/body before sending. No post timeout retry, new key or new T.
            intent = {'schema': 'ls84-capture-intent/v1', 'run_id': m['run_id'], 'locale': locale,
                      'path': base, 'key': key, 'body': body, 'outcome': 'UNKNOWN_UNTIL_OBSERVED'}
            c.write_new(root / 'reports' / f'capture-intent-{locale}.json', intent)
            created = client.json('POST', base, body=body, key=key, expected=202)
            c.require(c.uuid(created.get('package_id')), 'CAPTURE_ID_INVALID')
            package_id = created['package_id']
            c.write_new(root / 'reports' / f'capture-ack-{locale}.json',
                        {'package_id': package_id, 'key': key, 'state': created['state']})
            path = base + package_id + '/'
            while True:
                c.require(time.monotonic() - started <= CYCLE_TIMEOUT, 'CYCLE_BUDGET_EXCEEDED')
                detail = client.json('GET', path)
                if detail['state'] == 'ready':
                    break
                c.require(detail['state'] in ('queued', 'running', 'dispatch_pending'), 'PACKAGE_NOT_READY')
                time.sleep(0.5)
            package = AuditPackage.objects.get(pk=package_id, project_id=info['id'], idempotency_key=key,
                                              creator_principal=f'label_studio_local:{actor.pk}', state='ready')
            c.require(AuditAttempt.objects.filter(pk=package.token, package=package, state='ready').exists(),
                      'DATABASE_ATTEMPT_NOT_READY')
            job_id = f'audit-{package.pk}-{package.token}'
            # Publication precedes the worker's RQ success bookkeeping. Wait for the same job only.
            with Redis(host='127.0.0.1', port=m['redis']['port'], db=0, socket_timeout=3) as redis:
                job = Job.fetch(job_id, connection=redis)
                while str(job.get_status(refresh=True)) not in ('finished', 'JobStatus.FINISHED'):
                    c.require(time.monotonic() - started <= CYCLE_TIMEOUT, 'CYCLE_BUDGET_EXCEEDED')
                    time.sleep(0.5)
                evidence_files = list((root / 'reports').glob(f'rq-job-{job_id}-*.json'))
                c.require(len(evidence_files) == 1, 'RQ_EXECUTION_EVIDENCE_REQUIRED')
                evidence = c.decode(c.read_file(evidence_files[0]))
                verify_chain(m, package, receipt, processes, job=job, evidence=evidence)
            artifact = AuditArtifact.objects.get(package=package, attempt_id=package.token)
            c.require(len(detail['artifacts']) == 1
                      and detail['artifacts'][0]['artifact_id'] == str(artifact.pk), 'ARTIFACT_ID_MISMATCH')
            # Construct the URL from validated UUIDs; never follow a server-supplied redirect/host.
            download = path + f'artifacts/{artifact.pk}/download/'
            target = root / 'reports' / f'http-audit-{package_id}.zip'
            client.download(download, target)
            with target.open('rb') as handle:
                client.check_time()
                c.require(target.stat().st_size == artifact.size_bytes
                          and c.sha(handle.read(ARTIFACT_LIMIT + 1)) == artifact.sha256, 'HTTP_ARTIFACT_MISMATCH')
                handle.seek(0)
                audit_format.verify(handle, package)  # Reuse exact production frozen ZIP/snapshot verification.
                client.check_time()
            c.write_new(root / 'reports' / f'audit-chain-{locale}.json', {
                'schema': 'ls84-audit-chain/v1', 'run_id': m['run_id'], 'source_digest': m['source_digest'],
                'observation': 'REAL_HTTP_RQ_DOWNLOAD_VERIFIED', 'whole_matrix': 'NOT_CLAIMED',
                'case_support': ['G01', 'G02', 'H01'], 'locale': locale, 'key': key, 'package_id': package_id,
                'attempt_token': str(package.token), 'job_id': job_id, 'worker_pid': processes['worker']['pid'],
                'workhorse_pid': evidence['workhorse_pid'], 'web_pid': processes['web']['pid'],
                'artifact_sha256': artifact.sha256, 'manifest_sha256': package.manifest_sha256,
                'elapsed_seconds': time.monotonic() - started,
            })
    finally:
        client.close()
