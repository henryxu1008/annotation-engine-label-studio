"""Conservative provenance for versions returned by a successful S3 PUT."""
import base64
import hashlib
import json
import re
import uuid
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from django.db import connection, transaction
from django.db.models import Q
from django.utils.crypto import constant_time_compare, salted_hmac


class S3ExportTargetBusy(ValueError):
    """An admitted PUT is blocked before any provider request has been sent."""


def s3_export_target_key(storage, annotation):
    from io_storages.s3.models import S3ExportStorageLink

    if annotation.pk is None or annotation.project_id != storage.project_id:
        raise ValueError('The annotation does not match the S3 storage project.')
    key = S3ExportStorageLink.get_key(annotation)
    return str(storage.prefix) + '/' + key if storage.prefix else key


def lock_s3_export_key(key):
    """Take the same transaction lock when creating or binding a key claim."""
    if not connection.in_atomic_block:
        raise RuntimeError('S3 key admission requires a transaction.')
    lock_id = int.from_bytes(
        hashlib.sha256(('issue86.s3.key.v1\0' + key).encode('utf-8')).digest()[:8], 'big', signed=True
    )
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(%s)', [lock_id])


def require_s3_deletion_admission(key):
    """Serialize a deletion intent with known and unknown S3 writers."""
    from io_storages.admission import TERMINAL_STATES
    from io_storages.admission_models import StorageOperation

    if not isinstance(key, str) or not key:
        raise ValueError('An S3 deletion key is required.')
    lock_s3_export_key(key)
    unknown_key = Q(target_key__isnull=True) | Q(target_key='')
    unresolved = (
        StorageOperation.objects.filter(
            storage_model='io_storages.s3exportstorage',
        )
        .exclude(state__in=TERMINAL_STATES)
        .filter(Q(target_key=key) | unknown_key)
    )
    blockers = unresolved.filter(operation='annotation_export')
    if blockers.exists():
        raise S3ExportTargetBusy('S3 deletion admission is temporarily blocked.')


def storage_binding(storage):
    # Ambient credential chains can change account or role without changing a
    # stored field. Do not claim a stable binding in that case.
    if not storage.bucket or not storage.aws_access_key_id or not storage.aws_secret_access_key:
        return None
    values = [
        storage.project_id,
        storage.pk,
        storage.bucket,
        storage.prefix,
        storage.s3_endpoint,
        storage.region_name,
        storage.aws_access_key_id,
        storage.aws_secret_access_key,
        storage.aws_session_token,
    ]
    owner = getattr(storage, 'expected_bucket_owner', None)
    if owner:
        if not isinstance(owner, str) or not re.fullmatch(r'[0-9]{12}', owner):
            return None
        values.append(owner)
    return salted_hmac(
        'issue86.s3-storage-binding.v2' if owner else 'issue86.s3-storage-binding.v1',
        json.dumps(values, ensure_ascii=True, separators=(',', ':')),
        algorithm='sha256',
    ).hexdigest()


def put_version(response):
    version = response.get('VersionId') if isinstance(response, dict) else None
    # S3's literal null version is replaceable and is not an immutable pin.
    if not isinstance(version, str) or not version or version.lower() == 'null' or len(version) > 1024:
        return None
    return version


def prepare_s3_export_request(storage, annotation, record):
    """Commit the exact PUT identity before any provider request is issued."""
    from io_storages.admission_models import StorageOperation

    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError('S3 request identity must commit before the external effect.')
    if (
        record.operation != 'annotation_export'
        or storage._meta.label_lower != 'io_storages.s3exportstorage'
        or annotation.pk is None
        or annotation.project_id != storage.project_id
    ):
        raise ValueError('The S3 request identity does not match the annotation and storage.')
    # A worker may have loaded storage before a configuration update acquired
    # the project lock. Recheck the persisted destination after admission; the
    # storage mutation fence blocks further supported changes while it runs.
    fields = (
        'project_id',
        'bucket',
        'prefix',
        's3_endpoint',
        'region_name',
        'aws_access_key_id',
        'aws_secret_access_key',
        'aws_session_token',
        'aws_sse_kms_key_id',
        'expected_bucket_owner',
    )
    persisted = type(storage).objects.filter(pk=storage.pk, project_id=storage.project_id).values(*fields).first()
    if persisted is None or any(persisted[field] != getattr(storage, field) for field in fields):
        raise S3ExportTargetBusy('The S3 storage configuration changed before admission.')
    key = s3_export_target_key(storage, annotation)
    if key != record.target_key or annotation.pk != record.subject_id:
        raise S3ExportTargetBusy('The S3 export target changed before submission.')
    token = uuid.uuid4().hex
    binding = storage_binding(storage)
    # Serialize admission across projects and S3 storage configurations that
    # address the same key. We intentionally over-serialize equal keys in
    # different buckets: storage configurations and credentials can change,
    # while a single database cannot fence external or old-version writers.
    with transaction.atomic():
        lock_s3_export_key(key)
        unknown_key = Q(target_key__isnull=True) | Q(target_key='')
        from io_storages.admission import TERMINAL_STATES

        blockers = (
            StorageOperation.objects.filter(
                storage_model='io_storages.s3exportstorage',
                operation='annotation_export',
            )
            .exclude(state__in=TERMINAL_STATES)
            .exclude(pk=record.pk)
            .filter(Q(target_key=key) | unknown_key)
        )

        deletions = (
            StorageOperation.objects.filter(
                storage_model='io_storages.s3exportstorage',
                operation='annotation_delete_pending',
            )
            .exclude(state__in=TERMINAL_STATES)
            .filter(Q(target_key=key) | unknown_key)
        )
        if blockers.exists() or deletions.exists():
            raise S3ExportTargetBusy('S3 export admission is temporarily blocked.')
        updated = StorageOperation.objects.filter(
            pk=record.pk,
            state=StorageOperation.State.RUNNING,
            project_id=storage.project_id,
            storage_model=storage._meta.label_lower,
            storage_id=storage.pk,
            operation='annotation_export',
            target_key=key,
            subject_id=annotation.pk,
            effect_payload={'issue86_s3_preflight': 'v2'},
            target_object_token__isnull=True,
        ).update(subject_id=annotation.pk, target_key=key, target_object_token=token, target_storage_binding=binding)
        if updated != 1:
            if StorageOperation.objects.filter(
                pk=record.pk,
                state=StorageOperation.State.CANCELLED,
                project_id=storage.project_id,
                storage_model=storage._meta.label_lower,
                storage_id=storage.pk,
                operation='annotation_export',
                target_key=key,
                subject_id=annotation.pk,
                effect_payload={'issue86_s3_preflight': 'v2'},
                target_object_token__isnull=True,
                target_version_id__isnull=True,
            ).exists():
                raise S3ExportTargetBusy('The S3 preflight was cancelled before submission.')
            raise ValueError('The durable S3 export request could not be recorded.')
    return {
        'record_id': record.pk,
        'storage_id': storage.pk,
        'project_id': storage.project_id,
        'annotation_id': annotation.pk,
        'key': key,
        'token': token,
        'binding': binding,
        'observed_version': None,
    }


def record_s3_export_response(record, identity):
    """Record a successful response without converting unknown outcomes into success."""
    from io_storages.admission_models import StorageOperation

    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError('S3 response identity must commit outside an outer transaction.')
    if identity['record_id'] != record.pk:
        raise ValueError('The S3 export response does not match its durable request.')
    updated = StorageOperation.objects.filter(
        pk=record.pk,
        state=StorageOperation.State.RUNNING,
        operation='annotation_export',
        project_id=identity['project_id'],
        storage_model='io_storages.s3exportstorage',
        storage_id=identity['storage_id'],
        subject_id=identity['annotation_id'],
        target_key=identity['key'],
        target_object_token=identity['token'],
        target_storage_binding=identity['binding'],
    ).update(target_version_id=put_version({'VersionId': identity['observed_version']}))
    if updated != 1:
        raise ValueError('The S3 export response does not match its durable request.')


def _require_aws_current_observer(storage, record, expected_owner, client):
    """Validate current S3 observer scope without claiming historical identity."""
    endpoint = urlsplit(getattr(getattr(client, 'meta', None), 'endpoint_url', '') or '')
    host = endpoint.hostname or ''
    if (
        storage.s3_endpoint
        or endpoint.scheme != 'https'
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.port not in (None, 443)
        or endpoint.path not in ('', '/')
        or endpoint.query
        or endpoint.fragment
        or not re.fullmatch(r's3(?:[.-][a-z0-9-]+)*\.amazonaws\.com(?:\.cn)?', host)
    ):
        raise ValueError('A standard AWS S3 endpoint is required.')
    if not isinstance(expected_owner, str) or not re.fullmatch(r'[0-9]{12}', expected_owner):
        raise ValueError('A trusted AWS bucket owner account is required.')
    if (
        storage._meta.label_lower != 'io_storages.s3exportstorage'
        or record.project_id != storage.project_id
        or record.storage_id != storage.pk
        or record.storage_model != storage._meta.label_lower
    ):
        raise ValueError('The storage identity does not match the recorded project and storage.')
    binding = storage_binding(storage)
    if not binding:
        raise ValueError('Explicit storage credentials are required.')
    return binding


def _require_aws_storage_identity(storage, record, expected_owner, client):
    """Validate locally before any provider request; do not infer quiescence."""
    binding = _require_aws_current_observer(storage, record, expected_owner, client)
    if getattr(storage, 'expected_bucket_owner', None) != expected_owner:
        raise ValueError('The expected AWS owner was not bound to the original export.')
    if not record.target_storage_binding or not constant_time_compare(binding, record.target_storage_binding):
        raise ValueError('The storage configuration binding changed.')


def _require_versioned_aws_bucket(storage, expected_owner, client):
    client.head_bucket(Bucket=storage.bucket, ExpectedBucketOwner=expected_owner)
    versioning = client.get_bucket_versioning(Bucket=storage.bucket, ExpectedBucketOwner=expected_owner)
    if versioning.get('Status') != 'Enabled':
        raise ValueError('Bucket versioning must be enabled.')


def verify_aws_deletion_target(storage, record, expected_owner, client):
    """Read-only preflight; this is not authorization to delete an object.

    Compatible providers may silently ignore ExpectedBucketOwner, so only an
    SDK-selected AWS S3 endpoint is admitted. The caller must still establish
    old-process isolation and define safe behavior for older object versions.
    """
    _require_aws_storage_identity(storage, record, expected_owner, client)
    if (
        record.operation != 'annotation_delete_pending'
        or not record.target_key
        or put_version({'VersionId': record.target_version_id}) is None
        or not isinstance(record.target_object_token, str)
        or not re.fullmatch(r'[0-9a-f]{32}', record.target_object_token)
    ):
        raise ValueError('The deletion intent lacks a valid version-bound target.')
    _require_versioned_aws_bucket(storage, expected_owner, client)
    result = client.head_object(
        Bucket=storage.bucket,
        Key=record.target_key,
        VersionId=record.target_version_id,
        ExpectedBucketOwner=expected_owner,
    )
    marker = result.get('Metadata', {}).get('ls-export-token', '')
    if result.get('VersionId') != record.target_version_id or not constant_time_compare(
        marker, record.target_object_token
    ):
        raise ValueError('The object version does not match the captured export identity.')


def _aws_version_pages(storage, key, expected_owner, client, max_pages):
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages < 1:
        raise ValueError('Invalid inventory page limit.')
    marker = None
    seen_markers = set()
    for _ in range(max_pages):
        kwargs = {'Bucket': storage.bucket, 'Prefix': key, 'ExpectedBucketOwner': expected_owner}
        if marker:
            kwargs.update(KeyMarker=marker[0], VersionIdMarker=marker[1])
        page = client.list_object_versions(**kwargs)
        if not isinstance(page, dict) or not isinstance(page.get('IsTruncated'), bool):
            raise ValueError('Incomplete version inventory.')
        yield page
        if not page['IsTruncated']:
            return
        marker = (page.get('NextKeyMarker'), page.get('NextVersionIdMarker'))
        if not all(isinstance(value, str) and value for value in marker) or marker in seen_markers:
            raise ValueError('Invalid version inventory continuation.')
        seen_markers.add(marker)
    raise ValueError('Version inventory exceeds the page limit.')


def inspect_aws_deletion_history(storage, record, expected_owner, client, *, max_pages=100):
    """Classify a point-in-time version inventory, without authorizing deletion.

    This must follow verify_aws_deletion_target. In particular, a concurrent
    writer can change the inventory immediately after this function returns.
    """
    versions = {}
    for page in _aws_version_pages(storage, record.target_key, expected_owner, client, max_pages):
        for field, is_delete_marker in (('Versions', False), ('DeleteMarkers', True)):
            entries = page.get(field, [])
            if not isinstance(entries, list):
                raise ValueError('Invalid version inventory.')
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get('Key'), str):
                    raise ValueError('Invalid version inventory entry.')
                if entry['Key'] != record.target_key:
                    continue
                version_id = entry.get('VersionId')
                if (
                    not isinstance(version_id, str)
                    or not version_id
                    or not isinstance(entry.get('IsLatest'), bool)
                    or version_id in versions
                ):
                    raise ValueError('Ambiguous version inventory entry.')
                versions[version_id] = (entry['IsLatest'], is_delete_marker)
    target = versions.get(record.target_version_id)
    if target is None or target[1] or sum(latest for latest, _ in versions.values()) != 1:
        raise ValueError('The version inventory does not establish a unique target and current version.')
    if not target[0]:
        return 'noncurrent'
    if any(
        not is_marker and version_id != record.target_version_id for version_id, (_, is_marker) in versions.items()
    ):
        return 'current_with_other_object_versions'
    return 'current_without_other_object_versions'


def _require_export_request_identity(storage, record, expected_owner, client):
    _require_aws_storage_identity(storage, record, expected_owner, client)
    if (
        record.operation != 'annotation_export'
        or record.state not in ('running', 'uncertain')
        or type(record.subject_id) is not int
        or record.subject_id < 1
        or not isinstance(record.target_key, str)
        or not record.target_key
        or not isinstance(record.target_object_token, str)
        or not re.fullmatch(r'[0-9a-f]{32}', record.target_object_token)
    ):
        raise ValueError('A blocking S3 export with a durable request identity is required.')


def inspect_aws_export_request(storage, record, expected_owner, client, *, max_pages=100, max_versions=100):
    """Inspect versions for a durable PUT token; never resolve its outcome.

    A match or absence is an observation only: requests, retries and other
    writers may still be in flight, and listing is not a locked snapshot.
    """
    _require_export_request_identity(storage, record, expected_owner, client)
    if not isinstance(max_versions, int) or isinstance(max_versions, bool) or max_versions < 1:
        raise ValueError('Invalid version inspection limit.')
    _require_versioned_aws_bucket(storage, expected_owner, client)
    seen = set()
    matches = 0
    for page in _aws_version_pages(storage, record.target_key, expected_owner, client, max_pages):
        entries = page.get('Versions', [])
        if not isinstance(entries, list):
            raise ValueError('Invalid version inventory.')
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get('Key'), str):
                raise ValueError('Invalid version inventory entry.')
            if entry['Key'] != record.target_key:
                continue
            version_id = put_version({'VersionId': entry.get('VersionId')})
            if version_id is None or version_id in seen:
                raise ValueError('Ambiguous version inventory entry.')
            seen.add(version_id)
            if len(seen) > max_versions:
                raise ValueError('Version inventory exceeds the inspection limit.')
            response = client.head_object(
                Bucket=storage.bucket,
                Key=record.target_key,
                VersionId=version_id,
                ExpectedBucketOwner=expected_owner,
            )
            if not isinstance(response, dict) or response.get('VersionId') != version_id:
                raise ValueError('The provider returned an inconsistent object version.')
            metadata = response.get('Metadata', {})
            if not isinstance(metadata, dict) or not isinstance(metadata.get('ls-export-token', ''), str):
                raise ValueError('The object metadata is invalid.')
            if constant_time_compare(metadata.get('ls-export-token', ''), record.target_object_token):
                matches += 1
    return {'inspected_versions': len(seen), 'matching_versions': matches, 'outcome': 'UNKNOWN'}


def _export_cursor_cipher():
    key = salted_hmac('issue86.s3-export-cursor.encryption.v1', 'key', algorithm='sha256').digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _export_cursor_scope(storage, record, expected_owner, page_size):
    # The cursor is bound to the immutable request identity, owner and page
    # size. The request token is never exposed as cursor plaintext.
    value = [
        str(record.pk),
        record.project_id,
        storage.pk,
        record.subject_id,
        record.target_key,
        record.target_object_token,
        record.target_storage_binding,
        expected_owner,
        page_size,
    ]
    return salted_hmac(
        'issue86.s3-export-cursor.scope.v1', json.dumps(value, separators=(',', ':')), algorithm='sha256'
    ).hexdigest()


def inspect_aws_export_request_page(storage, record, expected_owner, client, *, cursor=None, page_size=25):
    """Return one bounded, non-snapshot observation for the original PUT token.

    Neither a match nor a completed page sequence proves provider request drain
    or permits a ledger transition. The cursor contains encrypted provider
    markers and expires after one hour.
    """
    _require_export_request_identity(storage, record, expected_owner, client)
    if type(page_size) is not int or not 1 <= page_size <= 50:
        raise ValueError('Invalid inspection page size.')
    try:
        if str(uuid.UUID(str(record.pk))) != str(record.pk):
            raise ValueError('Invalid record identity')
    except (AttributeError, TypeError, ValueError):
        raise ValueError('A valid export record identity is required.') from None
    scope = _export_cursor_scope(storage, record, expected_owner, page_size)
    marker = None
    page_number = 1
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or not cursor or len(cursor) > 8192:
                raise ValueError('Invalid cursor')
            payload = json.loads(_export_cursor_cipher().decrypt(cursor.encode('ascii'), ttl=3600))
            if (
                not isinstance(payload, dict)
                or set(payload) != {'version', 'scope', 'key', 'version_id', 'page'}
                or type(payload['version']) is not int
                or payload['version'] != 1
                or not isinstance(payload['scope'], str)
                or not constant_time_compare(payload['scope'], scope)
                or type(payload['page']) is not int
                or not 2 <= payload['page'] <= 100
                or not all(
                    isinstance(payload[field], str) and 0 < len(payload[field].encode('utf-8')) <= 1024
                    for field in ('key', 'version_id')
                )
            ):
                raise ValueError('Invalid cursor')
            marker = (payload['key'], payload['version_id'])
            page_number = payload['page']
        except (InvalidToken, UnicodeError, ValueError, TypeError, KeyError):
            raise ValueError('Invalid or expired S3 export cursor.') from None
    _require_versioned_aws_bucket(storage, expected_owner, client)
    kwargs = {
        'Bucket': storage.bucket,
        'Prefix': record.target_key,
        'ExpectedBucketOwner': expected_owner,
        'MaxKeys': page_size,
    }
    if marker:
        kwargs.update(KeyMarker=marker[0], VersionIdMarker=marker[1])
    page = client.list_object_versions(**kwargs)
    if not isinstance(page, dict) or not isinstance(page.get('IsTruncated'), bool):
        raise ValueError('Incomplete version inventory.')
    versions, delete_markers = page.get('Versions', []), page.get('DeleteMarkers', [])
    if (
        not isinstance(versions, list)
        or not isinstance(delete_markers, list)
        or len(versions) + len(delete_markers) > page_size
    ):
        raise ValueError('Invalid bounded version inventory.')
    matches, seen = [], set()
    for entry in versions + delete_markers:
        if not isinstance(entry, dict) or not isinstance(entry.get('Key'), str):
            raise ValueError('Invalid version inventory entry.')
    for entry in versions:
        if entry['Key'] != record.target_key:
            continue
        version_id = put_version({'VersionId': entry.get('VersionId')})
        if version_id is None or version_id in seen:
            raise ValueError('Ambiguous version inventory entry.')
        seen.add(version_id)
        response = client.head_object(
            Bucket=storage.bucket, Key=record.target_key, VersionId=version_id, ExpectedBucketOwner=expected_owner
        )
        if not isinstance(response, dict) or response.get('VersionId') != version_id:
            raise ValueError('The provider returned an inconsistent object version.')
        metadata = response.get('Metadata', {})
        if not isinstance(metadata, dict) or not isinstance(metadata.get('ls-export-token', ''), str):
            raise ValueError('The object metadata is invalid.')
        if constant_time_compare(metadata.get('ls-export-token', ''), record.target_object_token):
            matches.append(version_id)
    next_cursor = None
    if page['IsTruncated']:
        next_marker = (page.get('NextKeyMarker'), page.get('NextVersionIdMarker'))
        if (
            page_number >= 100
            or next_marker == marker
            or not all(isinstance(value, str) and 0 < len(value.encode('utf-8')) <= 1024 for value in next_marker)
        ):
            raise ValueError('Invalid or excessive version inventory continuation.')
        payload = {
            'version': 1,
            'scope': scope,
            'key': next_marker[0],
            'version_id': next_marker[1],
            'page': page_number + 1,
        }
        next_cursor = (
            _export_cursor_cipher().encrypt(json.dumps(payload, separators=(',', ':')).encode()).decode('ascii')
        )
    return {
        'inspected_versions': len(seen),
        'matching_version_ids': matches,
        'page': page_number,
        'next_cursor': next_cursor,
        'outcome': 'UNKNOWN',
    }
