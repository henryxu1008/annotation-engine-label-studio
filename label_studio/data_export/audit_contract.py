"""Strict transport, independent policy and immutable audit format contract."""
import hashlib
import hmac
import importlib.metadata
import json
import platform
import re
import secrets
import zipfile
from pathlib import Path
from uuid import UUID

import rfc8785
from django.conf import settings
from django.utils.crypto import salted_hmac

from .approved import fail as base_fail

PROFILE = 'review-audit-redacted/v1'
SCHEMA = 'review-audit-source/v1'
FORMAT = 'audit-zip-format/v1'
EFFECT = 'supervisory_only'
ERROR_CODES = frozenset(
    'AUDIT_' + name
    for name in (
        'SOURCE_INTEGRITY_INVALID',
        'SQL_BUDGET',
        'TOO_LARGE',
        'SOURCE_CHANGED',
        'ROOT_UNAVAILABLE',
        'UNSUPPORTED_INPUT',
        'INPUT_SCOPE_UNPROVABLE',
        'PROJECTION_INVALID',
        'RUNTIME_CHANGED',
        'INPUT_INVALID',
        'SCRATCH_LIMIT',
        'FILE_INVALID',
        'PRIVATE_STORAGE_REQUIRED',
        'INVALID',
        'POLICY_INVALID',
        'MODE_FORBIDDEN',
        'VIEWER_CHANGED',
        'INVALID_REQUEST',
        'NOT_FOUND',
        'NOT_READY',
        'INVALID_CURSOR',
        'FORBIDDEN',
        'BACKGROUND_UNAVAILABLE',
        'COLLECTION_UNAVAILABLE',
        'AUTHORITY_CHANGED',
        'POLICY_CHANGED',
        'CREATOR_REVOKED',
        'INVALID_KEY',
        'QUEUE_UNAVAILABLE',
        'IDEMPOTENCY_CONFLICT',
        'ACTIVE_LIMIT',
        'PREVIEW_CHANGED',
        'NOT_RETRYABLE',
        'IO_FAILED',
        'GENERATION_FAILED',
        'REQUEST_REJECTED',
    )
)
MIB = 1024**2
DEFAULT_LIMITS = dict(
    roots=1000,
    assignments=1000,
    revisions=2000,
    per_assignment=20,
    entities=5000,
    edges=10000,
    cohort=2000,
    canonical=8 * MIB,
    stored=32 * MIB,
    input=8 * MIB,
    projection=8 * MIB,
    artifact=16 * MIB,
    scratch=32 * MIB,
    sql_per_pass=128,
    chunk=250,
    page=50,
    active_project=2,
    active_owner=1,
    timeout=1800,
)
README = (
    'Review audit package / 审核审计包\n'
    'Static text classification; supervisory_only; fixed redaction.\n'
    '静态文本分类；仅监督意见；固定脱敏，不含原文、答案、自由理由或实名。\n'
    'Complete means the declared bounded single-batch graph, including explicit evidence gaps.\n'
    '完整仅指声明的单批次有界图与明确缺口，不认证永久历史、原文或法律真实性。\n'
    'First reviews are observed subjects; batch execution provenance is not recorded.\n'
    'No approval, concern resolution, release, Host acceptance or current-source verification.\n'
    '不改变审核、concern、发布或Host权威；不验证当前源。\n'
).encode('utf-8')


def fail(code='SOURCE_INTEGRITY_INVALID', status=409):
    base_fail('AUDIT_' + code, status)


def digest(blob):
    return hashlib.sha256(blob).hexdigest()


def canonical(value):
    try:
        return rfc8785.dumps(value)
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        fail('INVALID', 422)


def exact(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode(
            'utf-8'
        )
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        fail('INVALID', 422)


def strict(blob, maximum=8 * MIB):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate')
            result[key] = value
        return result

    try:
        if not isinstance(blob, (bytes, str)) or len(blob.encode() if isinstance(blob, str) else blob) > maximum:
            fail('TOO_LARGE', 422)
        return json.loads(blob, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        fail('INVALID', 400)


def positive(value):
    return type(value) is int and 0 < value <= 2**63 - 1


def uuid(value):
    try:
        return str(UUID(value)) if isinstance(value, str) and str(UUID(value)) == value else None
    except (ValueError, AttributeError):
        return None


def hexadecimal(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def limits():
    supplied = getattr(settings, 'ANNOTATION_REVIEW_AUDIT_LIMITS', {})
    if not isinstance(supplied, dict) or any(
        k not in DEFAULT_LIMITS or type(v) is not int or v < 1 for k, v in supplied.items()
    ):
        fail('POLICY_INVALID', 503)
    return {k: min(v, supplied.get(k, v)) for k, v in DEFAULT_LIMITS.items()}


def policy(project):
    try:
        config = settings.ANNOTATION_REVIEW_AUDIT_POLICY
        entry = config['projects'][f'{project.organization_id}:{project.pk}']
        instance = uuid(config['instance_id'])
        if (
            not settings.ANNOTATION_REVIEW_AUDIT_ENABLED
            or not instance
            or entry['mode'] != 'fork_review'
            or set(entry) != {'mode', 'version'}
            or not isinstance(entry['version'], str)
            or not 1 <= len(entry['version'].encode()) <= 128
        ):
            raise ValueError()
        return dict(
            instance_id=instance,
            mode='fork_review',
            version=entry['version'],
            profile=PROFILE,
            schema=SCHEMA,
            limits=limits(),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        fail('MODE_FORBIDDEN', 403)


def preview_hash(domain, value):
    return salted_hmac(SCHEMA + '/preview/' + domain, exact(value), algorithm='sha256').hexdigest()


def aliases():
    key = secrets.token_bytes(32)

    def ref(kind, value):
        return hmac.new(key, kind.encode() + b'\0' + exact(value), hashlib.sha256).hexdigest()

    return ref


def formatter():
    return dict(
        format=FORMAT,
        python=platform.python_implementation() + '/' + platform.python_version(),
        zipfile=digest(Path(zipfile.__file__).read_bytes()),
        formatter=digest(Path(__file__).with_name('audit_format.py').read_bytes()),
        rfc8785=importlib.metadata.version('rfc8785'),
        jsonschema=importlib.metadata.version('jsonschema'),
        readme=README.decode('utf-8'),
        readme_sha256=digest(README),
        canonicalization='RFC8785+LF',
        compression='ZIP_STORED',
        timestamp=[1980, 1, 1, 0, 0, 0],
        permissions=0o100400,
        entries=['manifest.json', 'records.jsonl', 'README.txt', 'SHA256SUMS'],
    )
