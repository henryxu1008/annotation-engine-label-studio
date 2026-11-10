from datetime import timedelta
from io import BytesIO
from uuid import uuid4

import pytest
from classification_disagreements import bbox, bbox_storage
from classification_disagreements.models import BboxMediaAttempt, BboxMediaPin, BboxSubmissionProof
from django.utils import timezone
from organizations.tests.factories import OrganizationFactory
from PIL import Image
from projects.models import ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.test import APIClient
from tasks.models import Annotation, Submission, TaskAssignment
from tasks.tests.factories import TaskFactory
from users.tests.factories import UserFactory


@pytest.mark.django_db
def test_submission_binds_image_and_corruption_rolls_back(settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = str(tmp_path / 'public')
    settings.STATIC_ROOT = str(tmp_path / 'static')
    settings.APPROVED_EXPORT_ROOT = str(tmp_path / 'private')
    audit_root = tmp_path / 'private' / 'audit-packages'
    audit_root.mkdir(parents=True)
    monkeypatch.setattr(bbox_storage.audit_files, 'private_root', lambda: audit_root)
    org = OrganizationFactory()
    project = ProjectFactory(organization=org)
    settings.ANNOTATION_BBOX_MEDIA_ENABLED = True
    settings.ANNOTATION_BBOX_MEDIA_PROJECTS = {f'{org.pk}:{project.pk}': 'managed'}
    annotator = UserFactory(active_organization=org)
    ProjectMember.objects.create(project=project, user=annotator, role=ProjectMember.Role.ANNOTATOR)
    task = TaskFactory(project=project, data={'image': '/pinned-image/'})
    assignment = TaskAssignment.objects.create(task=task, project=project, assignee=annotator)
    output = BytesIO()
    Image.new('RGB', (10, 20), 'red').save(output, format='PNG')
    content = output.getvalue()
    image = bbox.image_identity(content)
    token = uuid4()
    key, storage_id = bbox_storage.publish(token, content)
    BboxMediaAttempt.objects.create(token=token, task=task, project=project, task_id_frozen=task.pk,
                                    project_id_frozen=project.pk, actor_id_frozen=org.created_by_id,
                                    storage_binding=storage_id, expires_at=timezone.now() + timedelta(minutes=5))
    pin = BboxMediaPin.objects.create(
        task=task, project=project, token=token, actor_id_frozen=org.created_by_id, field='image',
        source={'kind': 'managed-upload', 'upload_id': 1}, task_data_hash=bbox.digest(task.data),
        config_hash=bbox.digest(project.label_config), artifact_key=key, storage_binding=storage_id,
        sha256=image['sha256'], size_bytes=len(content), width=image['width'], height=image['height'],
    )
    client = APIClient()
    client.force_authenticate(user=annotator)
    response = client.post(f'/api/tasks/{task.pk}/annotations/', {
        'result': [], 'assignment_id': assignment.pk, 'assignment_version': assignment.version,
        'submit_for_review': True,
    }, format='json')
    assert response.status_code == 201, response.content
    submission = Submission.objects.get(assignment=assignment, revision=1)
    proof = BboxSubmissionProof.objects.get(submission=submission)
    assert proof.media_id == pin.pk and proof.result_hash == submission.result_hash

    annotation_id = response.json()['id']
    assignment.refresh_from_db()
    settings.ANNOTATION_BBOX_MEDIA_ENABLED = False
    denied = client.patch(f'/api/annotations/{annotation_id}/', {
        'result': [], 'assignment_id': assignment.pk, 'assignment_version': assignment.version,
        'submit_for_review': True,
    }, format='json')
    assert denied.status_code == 400 and Submission.objects.filter(assignment=assignment).count() == 1
    settings.ANNOTATION_BBOX_MEDIA_ENABLED = True
    (bbox_storage.root() / key).write_bytes(b'corrupt')
    assignment.refresh_from_db()
    response = client.patch(f'/api/annotations/{annotation_id}/', {
        'result': [{'id': 'changed', 'from_name': 'box', 'to_name': 'img', 'type': 'rectanglelabels',
                    'value': {'rectanglelabels': ['Car']}}],
        'assignment_id': assignment.pk, 'assignment_version': assignment.version,
        'submit_for_review': True,
    }, format='json')
    assert response.status_code == 400, response.content
    assert Submission.objects.filter(assignment=assignment).count() == 1
    assert BboxSubmissionProof.objects.filter(media=pin).count() == 1
    assert Annotation.objects.get(task=task).result == []
