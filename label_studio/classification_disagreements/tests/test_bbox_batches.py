from uuid import uuid4

import pytest
from access_control.project_operations import local_principal
from classification_disagreements import bbox, bbox_batches
from classification_disagreements.models import BboxQualityBatch
from organizations.tests.factories import OrganizationFactory
from projects.models import ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.exceptions import NotFound
from rest_framework.test import APIClient
from tasks.models import TaskAssignment
from tasks.tests.factories import TaskFactory
from users.tests.factories import UserFactory


@pytest.fixture
def context(settings):
    organization = OrganizationFactory()
    project = ProjectFactory(organization=organization)
    settings.ANNOTATION_BBOX_MEDIA_ENABLED = True
    settings.ANNOTATION_BBOX_QUALITY_ENABLED = True
    settings.ANNOTATION_BBOX_MEDIA_PROJECTS = {f'{organization.pk}:{project.pk}': 'managed'}
    settings.ANNOTATION_BBOX_QUALITY_MODES = {f'{organization.pk}:{project.pk}': 'fork_review'}
    return organization, project, local_principal(organization.created_by)


@pytest.mark.django_db
def test_idempotent_replay_survives_expired_preview(context, monkeypatch):
    _, project, principal = context
    task = TaskFactory(project=project)
    preview = bbox_batches.preview(project, principal, name='Test', task_ids=[task.pk])
    key = uuid4()
    batch, replayed = bbox_batches.create(project, principal, token=preview['token'], idempotency_key=key)
    assert not replayed and batch.manifest['plan']['items'][0]['result']['reason'] == 'insufficient_inputs'
    monkeypatch.setattr(bbox_batches.signing, 'loads', lambda *args, **kwargs: pytest.fail('must not reload preview'))
    again, replayed = bbox_batches.create(project, principal, token=preview['token'], idempotency_key=key)
    assert replayed and again.pk == batch.pk
    with pytest.raises(bbox.Error, match='BBOX_BATCH_KEY_CONFLICT'):
        bbox_batches.create(project, principal, token='different', idempotency_key=key)


@pytest.mark.django_db
def test_changed_cohort_is_rejected_after_preview(context):
    organization, project, principal = context
    task = TaskFactory(project=project)
    preview = bbox_batches.preview(project, principal, name='Test', task_ids=[task.pk])
    annotator = UserFactory(active_organization=organization)
    ProjectMember.objects.create(project=project, user=annotator, role=ProjectMember.Role.ANNOTATOR)
    TaskAssignment.objects.create(task=task, project=project, assignee=annotator)
    with pytest.raises(bbox.Error, match='BBOX_BATCH_PREVIEW_CHANGED'):
        bbox_batches.create(project, principal, token=preview['token'], idempotency_key=uuid4())
    assert not BboxQualityBatch.objects.filter(project=project).exists()


@pytest.mark.django_db
def test_reviewer_or_core_only_mode_cannot_create(context, settings):
    organization, project, principal = context
    reviewer = UserFactory(active_organization=organization)
    ProjectMember.objects.create(project=project, user=reviewer, role=ProjectMember.Role.REVIEWER)
    task = TaskFactory(project=project)
    with pytest.raises(NotFound):
        bbox_batches.preview(project, local_principal(reviewer), name='Test', task_ids=[task.pk])
    settings.ANNOTATION_BBOX_QUALITY_MODES = {f'{organization.pk}:{project.pk}': 'core_only_internal'}
    with pytest.raises(NotFound):
        bbox_batches.preview(project, principal, name='Test', task_ids=[task.pk])


@pytest.mark.django_db
def test_manager_api_previews_and_idempotently_creates(context):
    organization, project, _ = context
    task = TaskFactory(project=project)
    client = APIClient()
    client.force_authenticate(user=organization.created_by)
    base = f'/api/projects/{project.pk}/bbox-quality/'
    preview = client.post(base + 'preview/', {'name': 'Test', 'task_ids': [task.pk]}, format='json')
    assert preview.status_code == 200, preview.content
    key = str(uuid4())
    body = {'preview_token': preview.json()['token']}
    first = client.post(base + 'batches/', body, format='json', HTTP_IDEMPOTENCY_KEY=key)
    assert first.status_code == 201, first.content
    second = client.post(base + 'batches/', body, format='json', HTTP_IDEMPOTENCY_KEY=key)
    assert second.status_code == 200 and second.json()['id'] == first.json()['id']
    detail = client.get(base + f'batches/{first.json()["id"]}/')
    assert detail.status_code == 200, detail.content


@pytest.mark.django_db
def test_workspace_preview_binds_scope_hash_and_recovery(context):
    organization, project, _ = context
    task = TaskFactory(project=project)
    client = APIClient()
    client.force_authenticate(user=organization.created_by)
    base = f'/api/projects/{project.pk}/bbox-quality/'
    preview = client.post(base + 'preview/', {'name': 'Test', 'scope': {'kind': 'tasks', 'task_ids': [task.pk]}},
                          format='json')
    assert preview.status_code == 200, preview.content
    value = preview.json()
    assert value['schema'] == bbox_batches.SCHEMA and value['task_ids'] == [task.pk]
    assert value['display_counts'] == {'agreement': 0, 'disagreement': 0, 'unavailable': 1}
    assert value['scope_enumerated_at'] and len(value['viewer']) == 64
    body = {'preview_token': value['preview_token'], 'expected_plan_hash': value['plan_hash']}
    key = str(uuid4())
    invalid = client.post(base + 'batches/', {**body, 'expected_plan_hash': '0' * 64}, format='json',
                          HTTP_IDEMPOTENCY_KEY=key)
    assert invalid.status_code == 400
    first = client.post(base + 'batches/', body, format='json', HTTP_IDEMPOTENCY_KEY=key)
    assert first.status_code == 201, first.content
    second = client.post(base + 'batches/', body, format='json', HTTP_IDEMPOTENCY_KEY=key)
    assert second.status_code == 200 and second.json()['id'] == first.json()['id']
    assert second.json()['idempotency_key'] == key and second.json()['plan_hash'] == value['plan_hash']
    batch_url = base + f'batches/{first.json()["id"]}/'
    listing = client.get(base + 'batches/')
    detail = client.get(batch_url)
    queue = client.get(batch_url + 'items/', {'state': 'all'})
    item = client.get(batch_url + f'items/{task.pk}/')
    assert all(response.status_code == 200 for response in (listing, detail, queue, item))
    assert listing.json()['results'][0]['manifest_hash'] == detail.json()['manifest_hash']
    assert queue.json()['results'] == [{'task_id': task.pk, 'outcome': 'unavailable', 'reason': 'insufficient_inputs'}]
    assert item.json()['item']['task_id'] == task.pk and item.json()['item']['comparison']['pairs'] is None
    assert client.get(batch_url + 'items/999999/').status_code == 404
    changed = client.post(base + 'batches/', {**body, 'expected_plan_hash': '0' * 64}, format='json',
                          HTTP_IDEMPOTENCY_KEY=key)
    assert changed.status_code == 409
