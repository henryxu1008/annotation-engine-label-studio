"""Bounded evidence primitives for this workflow, not product authorization."""
import hashlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = 'henryxu1008/annotation-engine-label-studio'
WORKFLOW = 'Enterprise Browser E2E'
RECEIPT_LIMIT = 256 * 1024
REPORT_LIMIT = 4 * 1024**2
REPORT_UNION_LIMIT = 16 * 1024**2
BUILD_LIMIT = 256 * 1024**2
BUILD_FILE_LIMIT = 64 * 1024**2
BUILD_COUNT = 5000
BUILD_ARCHIVE_LIMIT = BUILD_LIMIT + 16 * 1024**2
DIAGNOSTIC_LIMIT = 128 * 1024**2
DIAGNOSTIC_FILE_LIMIT = 32 * 1024**2
DIAGNOSTIC_COUNT = 500


def require(value, message):
    if not value:
        raise ValueError(message)


def integer(v, low=0, high=2**53 - 1):
    return type(v) is int and low <= v <= high


def sha(v):
    return isinstance(v, str) and re.fullmatch(r'[0-9a-f]{40}', v) is not None


def sha256(v):
    return isinstance(v, str) and re.fullmatch(r'[0-9a-f]{64}', v) is not None


def pairs(values):
    out = {}
    for key, value in values:
        require(key not in out, 'Duplicate JSON key')
        out[key] = value
    return out


def loads(raw, limit=REPORT_LIMIT):
    require(len(raw) <= limit, 'JSON byte budget exceeded')
    return json.loads(
        raw, object_pairs_hook=pairs, parse_constant=lambda v: (_ for _ in ()).throw(ValueError('Nonfinite JSON'))
    )


def read_json(path, limit=REPORT_LIMIT):
    p = Path(path)
    require(not p.is_symlink() and p.is_file(), 'Missing regular evidence file')
    with p.open('rb') as f:
        return loads(f.read(limit + 1), limit)


def canonical(value, limit=REPORT_LIMIT):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    require(len(raw) <= limit, 'JSON byte budget exceeded')
    return raw


def public_evidence(value):
    """Narrow JSON never contains credential-bearing fields, headers or known fixture secrets."""
    forbidden = {
        'password',
        'cookie',
        'cookies',
        'authorization',
        'headers',
        'settings',
        'stack',
        'request',
        'response',
    }

    def inspect(v):
        if isinstance(v, dict):
            require(not forbidden.intersection(k.lower() for k in v), 'Private diagnostic field forbidden')
            for item in v.values():
                inspect(item)
        elif isinstance(v, list):
            for item in v:
                inspect(item)

    inspect(value)
    raw = canonical(value).decode('utf-8')
    require(
        not re.search(
            r'(?i)(?:cookie|authorization|set-cookie)\s*:\s*\S+|(?:sessionid|csrftoken)\s*[=:]\s*[A-Za-z0-9_-]{16,}|\bBearer\s+[A-Za-z0-9._~+/=-]{16,}',
            raw,
        ),
        'Credential-bearing diagnostic forbidden',
    )
    secret_path = os.environ.get('UI_SECRET_FILE')
    if secret_path:
        secrets = read_json(secret_path, 16 * 1024)
        require(
            isinstance(secrets, list) and all(isinstance(s, str) and 8 <= len(s) <= 256 for s in secrets),
            'Secret filter invalid',
        )
        require(not any(s in raw for s in secrets), 'Synthetic credential in public evidence')
    return value


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def write_json(path, value, limit=REPORT_LIMIT):
    raw = canonical(value, limit)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    require(not p.exists() and not p.is_symlink(), 'Never overwrite evidence')
    with p.open('xb') as f:
        f.write(raw)


def file_hash(path, limit=BUILD_FILE_LIMIT):
    p = Path(path)
    require(p.is_file() and not p.is_symlink() and p.stat().st_size <= limit, 'File byte budget/type invalid')
    h = hashlib.sha256()
    total = 0
    with p.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            total += len(block)
            require(total <= limit, 'File grew beyond budget')
            h.update(block)
    require(total == p.stat().st_size, 'File changed while hashing')
    return h.hexdigest()


def relative_path(name):
    require(isinstance(name, str) and 0 < len(name.encode('utf-8')) < 1024, 'Invalid artifact path')
    require('\\' not in name and ':' not in name and '\x00' not in name, 'Unsafe artifact path')
    p = PurePosixPath(name)
    require(not p.is_absolute() and all(v not in ('', '.', '..') for v in name.split('/')), 'Unsafe artifact path')
    for component in name.split('/'):
        require(not component.endswith(('.', ' ')) and not any(ord(c) < 32 for c in component), 'Unsafe portable path')
        require(
            not re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])', component.split('.')[0]),
            'Reserved device path',
        )
    return p


def regular_tree(root, count=BUILD_COUNT, total=BUILD_LIMIT, single=BUILD_FILE_LIMIT):
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), 'Regular directory required')
    rows = []
    used = 0
    names = set()
    for p in sorted(root.rglob('*')):
        require(not p.is_symlink(), 'Artifact symlink forbidden')
        if p.is_dir():
            continue
        require(p.is_file(), 'Nonregular artifact member')
        name = p.relative_to(root).as_posix()
        relative_path(name)
        require(name.casefold() not in names, 'Duplicate artifact path')
        names.add(name.casefold())
        used += p.stat().st_size
        require(len(rows) < count and used <= total, 'Artifact union budget exceeded')
        rows.append({'path': name, 'size': p.stat().st_size, 'sha256': file_hash(p, single)})
    require(rows, 'Empty artifact')
    return rows


def git(*args):
    return (
        subprocess.check_output(['git', *args], cwd=ROOT, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
        .decode()
        .strip()
    )


def inventory_digest():
    from ui_inventory import PROJECT, inventory

    value = inventory()
    files = {c['spec'] for group in value['shards'].values() for c in group}
    files.update(PROJECT + '/' + c['config_file'] for group in value['shards'].values() for c in group)
    # Receipt consumers must execute the same inventory and CI implementation.
    source = (
        list((ROOT / 'scripts/ci').glob('*.py'))
        + list((ROOT / 'scripts/ci').glob('*.cjs'))
        + list((ROOT / 'scripts/ci').glob('*.json'))
    )
    hashes = {str(p.relative_to(ROOT)).replace('\\', '/'): file_hash(p) for p in source}
    hashes.update({'web/' + p: file_hash(ROOT / 'web' / p) for p in sorted(files)})
    return digest({'inventory': value, 'source_hashes': hashes})


def recipe_digest():
    paths = [
        'web/yarn.lock',
        'web/package.json',
        'web/webpack.config.js',
        'poetry.lock',
        'pyproject.toml',
        '.github/workflows/enterprise-browser-e2e.yml',
    ]
    return digest({p: file_hash(ROOT / p) for p in paths})


def context(local=False):
    checkout = git('rev-parse', 'HEAD')
    require(sha(checkout), 'Checkout identity invalid')
    clean = not git('status', '--porcelain=v1')
    if local:
        run = os.environ.get('UI_LOCAL_RUN_ID', 'local-' + str(uuid.uuid4()))
        require(run.startswith('local-') and str(uuid.UUID(run[6:])) == run[6:], 'Local run identity invalid')
        return {
            'environment': 'local',
            'repository': REPOSITORY,
            'run_id': run,
            'attempt': 1,
            'event': 'local',
            'checkout': checkout,
            'metadata_head': checkout,
            'pr_head': None,
            'pr_base': None,
            'workflow': WORKFLOW,
            'inventory': inventory_digest(),
            'recipe': recipe_digest(),
            'checkout_clean': clean,
        }
    require(clean, 'GitHub evidence requires a clean checkout')
    require(
        os.environ.get('GITHUB_REPOSITORY') == REPOSITORY and os.environ.get('GITHUB_WORKFLOW') == WORKFLOW,
        'Only the controlled fork/workflow may publish CI receipts',
    )
    event = os.environ.get('GITHUB_EVENT_NAME')
    require(event in ('push', 'pull_request', 'workflow_dispatch'), 'Untrusted event')
    require(os.environ.get('GITHUB_SHA') == checkout, 'Checkout/event SHA mismatch')
    run = os.environ.get('GITHUB_RUN_ID', '')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '')
    require(
        run.isdecimal() and int(run) > 0 and attempt.isdecimal() and 1 <= int(attempt) <= 20, 'Run identity invalid'
    )
    payload = read_json(os.environ['GITHUB_EVENT_PATH'])
    head = base = None
    if event == 'pull_request':
        pr = payload['pull_request']
        head, base = pr['head']['sha'], pr['base']['sha']
        require(sha(head) and sha(base) and pr['base']['ref'] == 'main', 'PR scope mismatch')
    if event == 'push':
        require(
            os.environ.get('GITHUB_REF') == 'refs/heads/main' and payload.get('after') == checkout,
            'Push scope mismatch',
        )
    return {
        'environment': 'github',
        'repository': REPOSITORY,
        'run_id': int(run),
        'attempt': int(attempt),
        'event': event,
        'checkout': checkout,
        'metadata_head': head or checkout,
        'pr_head': head,
        'pr_base': base,
        'workflow': WORKFLOW,
        'inventory': inventory_digest(),
        'recipe': recipe_digest(),
        'checkout_clean': clean,
    }


def verify_context(ctx):
    require(
        ctx['checkout'] == git('rev-parse', 'HEAD')
        and ctx['inventory'] == inventory_digest()
        and ctx['recipe'] == recipe_digest(),
        'Source changed during execution',
    )
    if ctx['environment'] == 'github' or ctx.get('checkout_clean') is True:
        require(not git('status', '--porcelain=v1'), 'Checkout became dirty during execution')


def same_source(a, b):
    keys = (
        'environment',
        'repository',
        'run_id',
        'event',
        'checkout',
        'metadata_head',
        'pr_head',
        'pr_base',
        'workflow',
        'inventory',
        'recipe',
        'checkout_clean',
    )
    require(all(a.get(k) == b.get(k) for k in keys), 'Evidence source/run mismatch')
    require(integer(a.get('attempt'), 1, 20) and integer(b.get('attempt'), 1, 20), 'Attempt identity invalid')
    require(set(a) == set(b) and all(type(a[k]) is type(b[k]) for k in a), 'Evidence context type/shape mismatch')


def artifact_name(ctx, kind, shard):
    require(kind in ('build', 'evidence') and re.fullmatch(r'[a-z][a-z-]*', shard), 'Artifact identity invalid')
    return f"enterprise-ui-{ctx['run_id']}-a{ctx['attempt']}-{shard}-{kind}"
