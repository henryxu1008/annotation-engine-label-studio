"""Read-only IAM user inventory; it cannot establish isolation or request drain."""
import re
from urllib.parse import urlsplit


def _require_aws_endpoint(client, service):
    endpoint = urlsplit(getattr(getattr(client, 'meta', None), 'endpoint_url', '') or '')
    host = endpoint.hostname or ''
    if (
        endpoint.scheme != 'https'
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.port not in (None, 443)
        or endpoint.path not in ('', '/')
        or endpoint.query
        or endpoint.fragment
        or not re.fullmatch(rf'{service}(?:[.-][a-z0-9-]+)*\.amazonaws\.com(?:\.cn)?', host)
    ):
        raise ValueError('A standard AWS service endpoint is required.')


def inspect_legacy_iam_user(iam, sts, *, account_id, user_name, user_id, max_pages=10):
    """Count one immutable IAM user's long-term keys; always leave isolation UNKNOWN.

    An inactive key inventory does not cover temporary sessions, other users,
    IAM roles, presigned requests, network paths, or already accepted requests.
    The user ID must come from trusted historical deployment evidence, not a
    fresh lookup by name, because a deleted user name can be reused.
    """
    if not isinstance(account_id, str) or not re.fullmatch(r'[0-9]{12}', account_id):
        raise ValueError('A 12-digit AWS account is required.')
    if not isinstance(user_name, str) or not re.fullmatch(r'[A-Za-z0-9_+=,.@-]{1,64}', user_name):
        raise ValueError('A valid IAM user name is required.')
    if not isinstance(user_id, str) or not re.fullmatch(r'AIDA[A-Z0-9]{12,124}', user_id):
        raise ValueError('The historical immutable IAM user ID is required.')
    if type(max_pages) is not int or not 1 <= max_pages <= 100:
        raise ValueError('Invalid page limit.')
    _require_aws_endpoint(iam, 'iam')
    _require_aws_endpoint(sts, 'sts')
    caller = sts.get_caller_identity()
    if not isinstance(caller, dict) or caller.get('Account') != account_id:
        raise ValueError('The observer account does not match.')
    response = iam.get_user(UserName=user_name)
    user = response.get('User') if isinstance(response, dict) else None
    arn_pattern = (
        rf'arn:(?:aws|aws-us-gov|aws-cn):iam::{account_id}:user/(?:[A-Za-z0-9_+=,.@/-]*/)?{re.escape(user_name)}'
    )
    if (
        not isinstance(user, dict)
        or user.get('UserName') != user_name
        or user.get('UserId') != user_id
        or not isinstance(user.get('Arn'), str)
        or not re.fullmatch(arn_pattern, user['Arn'])
    ):
        raise ValueError('The historical IAM user identity does not match.')
    marker = None
    seen_markers, seen_keys = set(), set()
    counts = {'Active': 0, 'Inactive': 0}
    for _ in range(max_pages):
        params = {'UserName': user_name, 'MaxItems': 100}
        if marker is not None:
            params['Marker'] = marker
        page = iam.list_access_keys(**params)
        if not isinstance(page, dict) or type(page.get('IsTruncated')) is not bool:
            raise ValueError('Incomplete IAM access-key inventory.')
        keys = page.get('AccessKeyMetadata')
        if not isinstance(keys, list) or len(keys) > 100:
            raise ValueError('Invalid IAM access-key inventory.')
        for key in keys:
            if (
                not isinstance(key, dict)
                or key.get('UserName') != user_name
                or not isinstance(key.get('AccessKeyId'), str)
                or not re.fullmatch(r'[A-Za-z0-9]{16,128}', key['AccessKeyId'])
                or key['AccessKeyId'] in seen_keys
                or key.get('Status') not in counts
            ):
                raise ValueError('Ambiguous IAM access-key inventory.')
            seen_keys.add(key['AccessKeyId'])
            counts[key['Status']] += 1
        if not page['IsTruncated']:
            return {
                'active_long_term_keys': counts['Active'],
                'inactive_long_term_keys': counts['Inactive'],
                'isolation': 'UNKNOWN',
                'request_drain': 'UNKNOWN',
            }
        marker = page.get('Marker')
        if not isinstance(marker, str) or not marker or len(marker) > 1024 or marker in seen_markers:
            raise ValueError('Invalid IAM inventory continuation.')
        seen_markers.add(marker)
    raise ValueError('IAM inventory exceeds the page limit.')
