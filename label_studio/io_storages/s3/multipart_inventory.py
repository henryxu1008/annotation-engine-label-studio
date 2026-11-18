"""Bounded observation of multipart uploads; never a request-drain proof."""
import json
from uuid import UUID

from cryptography.fernet import InvalidToken
from django.utils.crypto import constant_time_compare, salted_hmac
from io_storages.s3.version_provenance import (
    _export_cursor_cipher,
    _require_aws_current_observer,
    _require_aws_storage_identity,
    _require_versioned_aws_bucket,
)


def inspect_aws_multipart_page(
    storage, record, expected_owner, client, *, cursor=None, page_size=25, legacy_current_configuration=False
):
    """Observe one page for an exact key, without disclosing upload IDs or mutating S3."""
    if type(legacy_current_configuration) is not bool:
        raise ValueError('Invalid multipart identity mode.')
    if legacy_current_configuration:
        if record.target_storage_binding is not None:
            raise ValueError('Current-configuration mode is only available for unbound legacy records.')
        current_binding = _require_aws_current_observer(storage, record, expected_owner, client)
        owner = getattr(storage, 'expected_bucket_owner', None)
        if owner and owner != expected_owner:
            raise ValueError('The current expected AWS owner does not match.')
    else:
        _require_aws_storage_identity(storage, record, expected_owner, client)
        current_binding = None
    if (
        (record.operation, record.state)
        not in (
            ('annotation_export', 'running'),
            ('annotation_export', 'uncertain'),
            ('annotation_delete_pending', 'queued'),
        )
        or not isinstance(record.target_key, str)
        or not record.target_key
        or len(record.target_key.encode('utf-8')) > 1024
    ):
        raise ValueError('A blocking, key-bound S3 export or deletion is required.')
    if type(page_size) is not int or not 1 <= page_size <= 50:
        raise ValueError('Invalid multipart page size.')
    try:
        if str(UUID(str(record.pk))) != str(record.pk):
            raise ValueError('Invalid record identity')
    except (AttributeError, TypeError, ValueError):
        raise ValueError('A valid operation identity is required.') from None
    scope_values = [
        str(record.pk),
        record.operation,
        record.project_id,
        storage.pk,
        record.target_key,
        record.target_storage_binding,
        expected_owner,
        page_size,
    ]
    scope_salt = 'issue86.s3-multipart-cursor.scope.v1'
    if legacy_current_configuration:
        scope_salt = 'issue86.s3-multipart-current-cursor.scope.v1'
        scope_values.append(current_binding)
    scope = salted_hmac(scope_salt, json.dumps(scope_values, separators=(',', ':')), algorithm='sha256').hexdigest()
    marker, page_number = None, 1
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or not cursor or len(cursor) > 8192:
                raise ValueError('Invalid cursor')
            payload = json.loads(_export_cursor_cipher().decrypt(cursor.encode('ascii'), ttl=3600))
            if (
                not isinstance(payload, dict)
                or set(payload) != {'kind', 'scope', 'key', 'upload', 'page'}
                or payload['kind'] != 'multipart-v1'
                or not isinstance(payload['scope'], str)
                or not constant_time_compare(payload['scope'], scope)
                or type(payload['page']) is not int
                or not 2 <= payload['page'] <= 100
                or not all(
                    isinstance(payload[field], str) and 0 < len(payload[field].encode('utf-8')) <= 1024
                    for field in ('key', 'upload')
                )
            ):
                raise ValueError('Invalid cursor')
            marker, page_number = (payload['key'], payload['upload']), payload['page']
        except (InvalidToken, UnicodeError, ValueError, TypeError, KeyError):
            raise ValueError('Invalid or expired multipart cursor.') from None
    legacy_versioning = None
    if legacy_current_configuration:
        # Multipart uploads can exist before versioning is enabled or after it
        # is suspended. This observation must not imply deletion readiness.
        client.head_bucket(Bucket=storage.bucket, ExpectedBucketOwner=expected_owner)
        versioning = client.get_bucket_versioning(Bucket=storage.bucket, ExpectedBucketOwner=expected_owner)
        if not isinstance(versioning, dict) or (
            'Status' in versioning and versioning['Status'] not in ('Enabled', 'Suspended')
        ):
            raise ValueError('Invalid current bucket versioning response.')
        legacy_versioning = versioning.get('Status', 'UNVERSIONED')
    else:
        _require_versioned_aws_bucket(storage, expected_owner, client)
    params = {
        'Bucket': storage.bucket,
        'Prefix': record.target_key,
        'MaxUploads': page_size,
        'ExpectedBucketOwner': expected_owner,
    }
    if marker:
        params.update(KeyMarker=marker[0], UploadIdMarker=marker[1])
    response = client.list_multipart_uploads(**params)
    if not isinstance(response, dict) or type(response.get('IsTruncated')) is not bool:
        raise ValueError('Incomplete multipart inventory.')
    uploads = response.get('Uploads', [])
    if not isinstance(uploads, list) or len(uploads) > page_size:
        raise ValueError('Invalid bounded multipart inventory.')
    seen = set()
    matches = 0
    for item in uploads:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(field), str) and 0 < len(item[field].encode('utf-8')) <= 1024
            for field in ('Key', 'UploadId')
        ):
            raise ValueError('Invalid multipart inventory entry.')
        identity = (item['Key'], item['UploadId'])
        if identity in seen or not item['Key'].startswith(record.target_key):
            raise ValueError('Ambiguous multipart inventory entry.')
        seen.add(identity)
        matches += item['Key'] == record.target_key
    next_cursor = None
    if response['IsTruncated']:
        next_marker = (response.get('NextKeyMarker'), response.get('NextUploadIdMarker'))
        if (
            page_number >= 100
            or next_marker == marker
            or not all(isinstance(value, str) and 0 < len(value.encode('utf-8')) <= 1024 for value in next_marker)
        ):
            raise ValueError('Invalid or excessive multipart continuation.')
        payload = {
            'kind': 'multipart-v1',
            'scope': scope,
            'key': next_marker[0],
            'upload': next_marker[1],
            'page': page_number + 1,
        }
        next_cursor = (
            _export_cursor_cipher().encrypt(json.dumps(payload, separators=(',', ':')).encode()).decode('ascii')
        )
    result = {
        'matching_uploads': matches,
        'inspected_uploads': len(uploads),
        'page': page_number,
        'next_cursor': next_cursor,
        'outcome': 'UNKNOWN',
    }
    if legacy_current_configuration:
        result['storage_identity'] = 'CURRENT_CONFIGURATION_ONLY'
        result['bucket_versioning'] = legacy_versioning
    return result
