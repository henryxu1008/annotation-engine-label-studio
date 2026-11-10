import json

import pytest
from classification_disagreements import bbox_storage
from classification_disagreements.models import BboxMediaPin, BboxSubmissionProof
from django.core.management import call_command
from projects.models import ProjectMember


@pytest.mark.django_db
def test_synthetic_browser_fixture_captures_and_binds_evidence(settings, tmp_path, monkeypatch):
    settings.TEST_ENVIRONMENT = True
    settings.MEDIA_ROOT = str(tmp_path / 'public')
    settings.STATIC_ROOT = str(tmp_path / 'static')
    settings.APPROVED_EXPORT_ROOT = str(tmp_path / 'private')
    root = tmp_path / 'private' / 'audit-packages'
    root.mkdir(parents=True)
    monkeypatch.setattr(bbox_storage.audit_files, 'private_root', lambda: root)
    output = tmp_path / 'fixture.json'
    call_command('seed_bbox_e2e', output=str(output))
    fixture = json.loads(output.read_text(encoding='utf-8'))
    assert set(fixture['projects']) == {'en-US', 'zh-CN'}
    assert BboxMediaPin.objects.filter(project__organization_id=fixture['organization']).count() == 2
    assert BboxSubmissionProof.objects.filter(media__project__organization_id=fixture['organization']).count() == 4
    call_command('seed_bbox_e2e', output=str(output), operation='revoke_reviewer', locale='en-US')
    assert not ProjectMember.objects.get(project_id=fixture['projects']['en-US']['id'],
                                         user_id=fixture['users']['reviewer']['id']).enabled
    assert ProjectMember.objects.get(project_id=fixture['projects']['zh-CN']['id'],
                                     user_id=fixture['users']['reviewer']['id']).enabled
