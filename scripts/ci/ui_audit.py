"""Strict job-private audit fixture validation, reusing supervision bootstrap."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

from ui_common import require
from ui_second_review import validate_database as supervision_database
from ui_second_review import validate_fixture as supervision_fixture
from ui_server import uuid_string

PROVIDER = 'core.management.commands.seed_review_audit_e2e.ReviewAuditE2EPolicy'


def validate_fixture(value):
    require(
        isinstance(value, dict) and set(value) == {'schema', 'supervision', 'settings', 'ready'},
        'Audit fixture shape invalid',
    )
    require(value['schema'] == 'review-audit-e2e/v1', 'Audit fixture version invalid')
    base = supervision_fixture(value['supervision'])
    configured = value['settings']
    extras = {'ANNOTATION_REVIEW_AUDIT_ENABLED', 'ANNOTATION_REVIEW_AUDIT_POLICY', 'APPROVED_EXPORT_ROOT'}
    require(isinstance(configured, dict) and set(configured) == set(base) | extras, 'Audit settings allowlist invalid')
    require(configured['ANNOTATION_PROJECT_OPERATION_PROVIDER'] == PROVIDER, 'Audit policy import forbidden')
    require(
        all(configured[key] == entry for key, entry in base.items() if key != 'ANNOTATION_PROJECT_OPERATION_PROVIDER'),
        'Supervision settings changed',
    )
    policy = configured['ANNOTATION_REVIEW_AUDIT_POLICY']
    require(
        isinstance(policy, dict) and set(policy) == {'instance_id', 'projects'} and uuid_string(policy['instance_id']),
        'Audit namespace invalid',
    )
    require(
        policy['projects']
        == {
            key: {'mode': 'fork_review', 'version': 'synthetic-audit/v1'}
            for key in base['ANNOTATION_REVIEW_BATCH_MODES']
        },
        'Audit scope invalid',
    )
    require(configured['ANNOTATION_REVIEW_AUDIT_ENABLED'] is True, 'Audit flag invalid')
    require(
        isinstance(configured['APPROVED_EXPORT_ROOT'], str) and Path(configured['APPROVED_EXPORT_ROOT']).is_absolute(),
        'Private root invalid',
    )
    require(
        isinstance(value['ready'], dict)
        and set(value['ready']) == {'en-US', 'zh-CN'}
        and all(uuid_string(v) for v in value['ready'].values()),
        'Audit READY identities invalid',
    )
    return deepcopy(configured)


def validate_database(value):
    from django.conf import settings
    from rest_framework.test import APIClient
    from users.models import User

    configured = validate_fixture(value)
    require(
        Path(configured['APPROVED_EXPORT_ROOT']).resolve()
        == Path(settings.BASE_DATA_DIR).resolve().parent / 'audit-private-exports',
        'Audit storage not owned by job',
    )
    supervision_database(value['supervision'])
    client = APIClient()
    client.force_authenticate(User.objects.get(pk=value['supervision']['users']['manager']['id']))
    for locale, project in value['supervision']['projects'].items():
        response = client.get(f'/api/projects/{project["id"]}/review-audit-packages/{value["ready"][locale]}/')
        require(response.status_code == 200 and response.data['state'] == 'ready', 'Synthetic audit READY row invalid')


@contextmanager
def synthetic_queue():
    """Only this fresh synthetic server; no product settings/provider fallback."""
    from data_export import approved
    from data_export import review_audit as audit

    with patch.object(audit, 'queue_available', return_value=True), patch.object(
        approved, 'api_queue', return_value=MagicMock()
    ):
        yield
