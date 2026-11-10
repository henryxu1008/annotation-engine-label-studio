import base64
import json
from datetime import timedelta
from io import BytesIO, StringIO
from uuid import uuid4

import pytest
from access_control.project_operations import local_principal
from classification_disagreements import (
    bbox,
    bbox_arbitration,
    bbox_batches,
    bbox_compare,
    bbox_media,
    bbox_prepare,
    bbox_quality,
    bbox_storage,
)
from classification_disagreements.models import BboxArbitrationDecision, BboxArbitrationTask, BboxSubmissionProof
from data_import.models import FileUpload
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import IntegrityError, transaction
from io_storages.gcs.models import GCSImportStorage, GCSImportStorageLink
from io_storages.localfiles.models import LocalFilesImportStorage
from io_storages.s3.models import S3ImportStorage, S3ImportStorageLink
from organizations.tests.factories import OrganizationFactory
from PIL import Image
from projects.models import ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.test import APIClient
from tasks.models import Submission, TaskAssignment
from tasks.tests.factories import TaskFactory
from users.tests.factories import UserFactory

XML = '<View><Image name="img" value="$image"/><RectangleLabels name="box" toName="img"><Label value="Car"/></RectangleLabels></View>'


@pytest.mark.django_db
def test_managed_upload_editor_and_submission_are_bound(settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = str(tmp_path / 'public')
    settings.STATIC_ROOT = str(tmp_path / 'static')
    settings.APPROVED_EXPORT_ROOT = str(tmp_path / 'private')
    audit_root = tmp_path / 'private' / 'audit-packages'
    audit_root.mkdir(parents=True)
    monkeypatch.setattr(bbox_storage.audit_files, 'private_root', lambda: audit_root)
    org = OrganizationFactory()
    project = ProjectFactory(organization=org, label_config=XML)
    settings.ANNOTATION_BBOX_MEDIA_ENABLED = True
    settings.ANNOTATION_BBOX_MEDIA_PROJECTS = {f'{org.pk}:{project.pk}': 'managed'}
    output = BytesIO()
    Image.new('RGB', (10, 20), 'red').save(output, format='PNG')
    original = output.getvalue()
    upload = FileUpload.objects.create(project=project, user=org.created_by,
                                       file=ContentFile(original, name='image.png'))
    unrelated = FileUpload.objects.create(project=project, user=org.created_by,
                                          file=ContentFile(original, name='unrelated.png'))
    settings.LOCAL_FILES_DOCUMENT_ROOT = settings.MEDIA_ROOT
    settings.LOCAL_FILES_SERVING_ENABLED = True
    LocalFilesImportStorage.objects.create(project=project, path=settings.MEDIA_ROOT)
    task = TaskFactory(project=project, data={'image': upload.url})
    annotator = UserFactory(active_organization=org)
    member = ProjectMember.objects.create(project=project, user=annotator, role=ProjectMember.Role.ANNOTATOR)
    client = APIClient()
    client.force_authenticate(user=annotator)
    prepare_path = f'/api/projects/{project.pk}/bbox-media/tasks/{task.pk}/prepare/'
    assert client.post(prepare_path, {}, format='json').status_code in (403, 404)
    client.force_authenticate(user=org.created_by)
    local_url = '/data/local-files/'
    assert client.get(local_url, {'d': upload.file.name}).status_code == 200
    assert client.post(prepare_path, {'actor_id': annotator.pk}, format='json').status_code == 400
    assert client.post(prepare_path, {'exclusive_attestation': '0' * 64}, format='json').status_code == 400
    assert client.post(prepare_path, {}, format='json').status_code == 400
    description = StringIO()
    call_command('describe_bbox_source', task=task.pk, stdout=description)
    described = json.loads(description.getvalue())
    assert described['exclusive_source_claim'] == bbox_prepare.exclusive_source_claim(task, upload.url)
    settings.ANNOTATION_BBOX_EXCLUSIVE_SOURCE_ATTESTATIONS = {f'{org.pk}:{project.pk}': {str(task.pk): '0' * 64}}
    assert client.post(prepare_path, {}, format='json').status_code == 400
    settings.ANNOTATION_BBOX_EXCLUSIVE_SOURCE_ATTESTATIONS = {
        f'{org.pk}:{project.pk}': {str(task.pk): bbox_prepare.exclusive_source_claim(task, upload.url)}}
    original_claims = settings.ANNOTATION_BBOX_EXCLUSIVE_SOURCE_ATTESTATIONS
    capture_upload = bbox_media.capture_managed_upload

    def revoke_during_capture(*args):
        captured = capture_upload(*args)
        settings.ANNOTATION_BBOX_EXCLUSIVE_SOURCE_ATTESTATIONS = {}
        return captured

    monkeypatch.setattr(bbox_media, 'capture_managed_upload', revoke_during_capture)
    assert client.post(prepare_path, {}, format='json').status_code == 400
    assert not hasattr(task, 'bbox_media_pin')
    settings.ANNOTATION_BBOX_EXCLUSIVE_SOURCE_ATTESTATIONS = original_claims
    monkeypatch.setattr(bbox_media, 'capture_managed_upload', capture_upload)
    response = client.post(prepare_path, {}, format='json')
    assert response.status_code == 201, response.content
    replay = client.post(prepare_path, {}, format='json')
    assert replay.status_code == 201 and replay.json()['media_token'] == response.json()['media_token']
    shared = TaskFactory(project=project, data={'image': upload.url})
    settings.ANNOTATION_BBOX_EXCLUSIVE_SOURCE_ATTESTATIONS[f'{org.pk}:{project.pk}'][str(shared.pk)] = (
        bbox_prepare.exclusive_source_claim(shared, upload.url))
    refused = client.post(f'/api/projects/{project.pk}/bbox-media/tasks/{shared.pk}/prepare/', {}, format='json')
    assert refused.status_code == 400 and not hasattr(shared, 'bbox_media_pin')
    task.refresh_from_db()
    url = task.data['image']
    assert task.bbox_media_pin.source['exclusive_attestation'] == settings.ANNOTATION_BBOX_EXCLUSIVE_SOURCE_ATTESTATIONS[
        f'{org.pk}:{project.pk}'][str(task.pk)]
    assert '/bbox-media/' in url and upload.url != url
    assert client.get(upload.url).status_code == 404
    assert client.get(unrelated.url).status_code == 200
    assert client.get(upload.url, HTTP_RANGE='bytes=0-1').status_code == 404
    assert client.get('/storage-data/uploaded/', {'filepath': upload.file.name}).status_code == 404
    assert client.get(local_url, {'d': upload.file.name}).status_code == 404
    old_upload_name = upload.file.name
    FileUpload.objects.filter(pk=upload.pk).update(file='upload/rebound-away.png')
    assert client.get(local_url, {'d': old_upload_name}).status_code == 404
    FileUpload.objects.create(project=project, user=org.created_by, file=old_upload_name)
    assert client.get(upload.url).status_code == 404
    assert client.get('/storage-data/uploaded/', {'filepath': old_upload_name}).status_code == 404
    FileUpload.objects.filter(pk=upload.pk).update(file=old_upload_name)
    assignment = TaskAssignment.objects.create(task=task, project=project, assignee=annotator)
    client.force_authenticate(user=annotator)
    task_response = client.get(f'/api/tasks/{task.pk}/?resolve_uri=false')
    assert task_response.status_code == 200, task_response.content
    assert task_response.json()['data']['image'] == url
    resolved = client.get(f'/api/tasks/{task.pk}/?resolve_uri=true')
    assert resolved.status_code == 200 and resolved.json()['data']['image'] == url
    image_response = client.get(url)
    assert image_response.status_code == 200 and image_response.content == original
    assert client.get(url + '?untrusted=1').status_code == 404
    member.enabled = False
    member.save(update_fields=['enabled'])
    assert client.get(url).status_code in (403, 404)
    member.enabled = True
    member.save(update_fields=['enabled'])
    region = {'type': 'rectanglelabels', 'from_name': 'box', 'to_name': 'img', 'original_width': 10,
              'original_height': 20, 'image_rotation': 0,
              'value': {'x': 0, 'y': 0, 'width': 20, 'height': 20, 'rotation': 0, 'rectanglelabels': ['Car']}}
    submission_response = client.post(f'/api/tasks/{task.pk}/annotations/', {
        'result': [region], 'assignment_id': assignment.pk, 'assignment_version': assignment.version,
        'submit_for_review': True,
    }, format='json')
    assert submission_response.status_code == 201, submission_response.content
    submission = Submission.objects.get(assignment=assignment)
    assert BboxSubmissionProof.objects.filter(submission=submission).exists()
    assert bbox_quality.build_plan(project, [task.pk])['items'][0]['result']['reason'] == 'insufficient_inputs'
    second = UserFactory(active_organization=org)
    ProjectMember.objects.create(project=project, user=second, role=ProjectMember.Role.ANNOTATOR)
    other_assignment = TaskAssignment.objects.create(task=task, project=project, assignee=second)
    client.force_authenticate(user=second)
    second_response = client.post(f'/api/tasks/{task.pk}/annotations/', {
        'result': [region], 'assignment_id': other_assignment.pk, 'assignment_version': other_assignment.version,
        'submit_for_review': True,
    }, format='json')
    assert second_response.status_code == 201, second_response.content
    rows = list(Submission.objects.filter(assignment__task=task).select_related('assignment__task', 'review').order_by('pk'))
    compared = bbox_compare.prepare(rows, project=project, task=task)
    assert compared['pairs'][0]['f1'] == {'numerator': '1', 'denominator': '1'}
    plan = bbox_quality.build_plan(project, [task.pk])
    assert plan['items'][0]['result']['pairs'][0]['f1'] == {'numerator': '1', 'denominator': '1'}
    assert bbox_quality.visible_items(plan, annotator, 'reviewer') == []
    assert bbox_quality.visible_items(plan, org.created_by, 'manager') == plan['items']
    assert bbox_quality.visible_items(plan, org.created_by, 'annotator') == []
    settings.ANNOTATION_BBOX_QUALITY_ENABLED = True
    settings.ANNOTATION_BBOX_QUALITY_MODES = {f'{org.pk}:{project.pk}': 'fork_review'}
    principal = local_principal(org.created_by)
    preview = bbox_batches.preview(project, principal, name='Quality', task_ids=[task.pk])
    request_key = uuid4()
    batch, replayed = bbox_batches.create(project, principal, token=preview['token'], idempotency_key=request_key)
    assert not replayed and batch.manifest['plan'] == plan
    repeated, replayed = bbox_batches.create(project, principal, token=preview['token'], idempotency_key=request_key)
    assert replayed and repeated.pk == batch.pk
    with pytest.raises(IntegrityError), transaction.atomic():
        type(batch).objects.filter(pk=batch.pk).update(manifest_hash='f' * 64)
    assert bbox_quality.replay(project, plan) == plan
    detail = f'/api/projects/{project.pk}/bbox-quality/batches/{batch.pk}/'
    batch_image = detail + f'items/{task.pk}/image/'
    client.force_authenticate(user=org.created_by)
    assert client.get(detail).status_code == 200
    assert client.get(batch_image).content == original
    reviewer = UserFactory(active_organization=org)
    reviewer_member = ProjectMember.objects.create(project=project, user=reviewer, role=ProjectMember.Role.REVIEWER)
    client.force_authenticate(user=reviewer)
    reviewer_detail = client.get(detail)
    assert reviewer_detail.status_code == 200
    assert 'manifest_hash' not in reviewer_detail.json() and 'projection_hash' in reviewer_detail.json()
    reviewer_queue = client.get(detail + 'items/', {'state': 'all'})
    reviewer_item = client.get(detail + f'items/{task.pk}/')
    assert reviewer_queue.status_code == 200 and reviewer_item.status_code == 200
    assert reviewer_detail.json()['projection_hash'] == reviewer_queue.json()['projection_hash'] == reviewer_item.json()['projection_hash']
    assert client.get(batch_image).content == original
    batches = client.get(f'/api/projects/{project.pk}/bbox-quality/batches/')
    assert batches.status_code == 200 and any(item['id'] == batch.pk for item in batches.json()['results'])
    reviewer_member.enabled = False
    reviewer_member.save(update_fields=['enabled'])
    assert client.get(detail).status_code in (403, 404)
    assert client.get(batch_image).status_code in (403, 404)
    assert client.get(detail + 'items/', {'state': 'all'}).status_code in (403, 404)
    member.role = ProjectMember.Role.REVIEWER
    member.save(update_fields=['role'])
    client.force_authenticate(user=annotator)
    assert client.get(detail).status_code in (403, 404)
    assert client.get(batch_image).status_code in (403, 404)
    assert client.get(detail + f'items/{task.pk}/').status_code in (403, 404)
    hidden = client.get(f'/api/projects/{project.pk}/bbox-quality/batches/')
    assert hidden.status_code == 200 and all(item['id'] != batch.pk for item in hidden.json()['results'])
    member.role = ProjectMember.Role.ANNOTATOR
    member.save(update_fields=['role'])
    client.force_authenticate(user=second)
    other_assignment.refresh_from_db()
    changed_region = {**region, 'value': {**region['value'], 'x': 10}}
    update = client.patch(f'/api/annotations/{second_response.json()["id"]}/', {
        'result': [changed_region], 'assignment_id': other_assignment.pk,
        'assignment_version': other_assignment.version, 'submit_for_review': True,
    }, format='json')
    assert update.status_code == 200, update.content
    assert bbox_quality.replay(project, plan) == plan
    settings.ANNOTATION_BBOX_ARBITRATION_ENABLED = True
    settings.ANNOTATION_BBOX_ARBITRATION_MODES = {f'{org.pk}:{project.pk}': 'fork_review'}
    reviewer_member.enabled = True
    reviewer_member.save(update_fields=['enabled'])
    conflict_preview = bbox_batches.preview(project, principal, name='Disagreement', task_ids=[task.pk])
    conflict_batch, _ = bbox_batches.create(project, principal, token=conflict_preview['token'], idempotency_key=uuid4())
    member.role = ProjectMember.Role.REVIEWER
    member.save(update_fields=['role'])
    with pytest.raises(bbox.Error, match='BBOX_ARBITRATION_ASSIGNEE_INVALID'):
        bbox_arbitration.create(project, principal, batch_id=conflict_batch.pk, task_id=task.pk,
                                expected_hash=conflict_batch.manifest_hash, assignee_id=annotator.pk,
                                idempotency_key=uuid4())
    member.role = ProjectMember.Role.ANNOTATOR
    member.save(update_fields=['role'])
    arbitration_base = f'/api/projects/{project.pk}/bbox-arbitrations/'
    create_key = uuid4()
    create_body = {'batch_id': conflict_batch.pk, 'task_id': task.pk,
                   'expected_hash': conflict_batch.manifest_hash, 'assignee_id': reviewer.pk}
    client.force_authenticate(user=org.created_by)
    roster_query = {key: create_body[key] for key in ('batch_id', 'task_id', 'expected_hash')}
    roster = client.get(arbitration_base + 'assignees/', roster_query)
    assert roster.status_code == 200, roster.content
    assert reviewer.pk in [user['id'] for user in roster.json()['results']]
    assert annotator.pk not in [user['id'] for user in roster.json()['results']]
    client.force_authenticate(user=reviewer)
    assert client.get(arbitration_base + 'assignees/', roster_query).status_code in (403, 404)
    client.force_authenticate(user=org.created_by)
    assert client.get(arbitration_base + 'assignees/', {**roster_query, 'expected_hash': '0' * 64}).status_code == 409
    forged = client.post(arbitration_base, {**create_body, 'actor_id': reviewer.pk},
                         format='json', HTTP_IDEMPOTENCY_KEY=str(create_key))
    assert forged.status_code == 400
    created = client.post(arbitration_base, create_body, format='json', HTTP_IDEMPOTENCY_KEY=str(create_key))
    assert created.status_code == 201, created.content
    assert created.json()['receipt']['key'] == str(create_key)
    assert created.json()['receipt']['actor_id'] == org.created_by_id
    arbitration = BboxArbitrationTask.objects.get(pk=created.json()['id'])
    repeated = client.post(arbitration_base, create_body, format='json', HTTP_IDEMPOTENCY_KEY=str(create_key))
    assert repeated.status_code == 200 and repeated.json()['replayed']
    client.force_authenticate(user=reviewer)
    arbitration_detail = client.get(arbitration_base + f'{arbitration.pk}/')
    assert arbitration_detail.status_code == 200
    assert 'batch_hash' not in arbitration_detail.json()['origin']
    assert 'origin_manifest_hash' not in arbitration_detail.json()
    assert arbitration_detail.json()['viewer_hash'] == client.get(arbitration_base + 'capability/').json()['viewer_hash']
    pair = arbitration_detail.json()['selected_pair']
    assert client.get(arbitration_base + f'{arbitration.pk}/', {'left': pair[0], 'right': pair[1]}).status_code == 200
    assert client.get(arbitration_base + f'{arbitration.pk}/', {'left': pair[0], 'right': 999999}).status_code == 400
    assert client.get(arbitration_base + f'{arbitration.pk}/', {'left': pair[0]}).status_code == 400
    listing = client.get(arbitration_base)
    assert listing.status_code == 200 and any(item['id'] == arbitration.pk for item in listing.json()['results'])
    arbitration_image = arbitration_base + f'{arbitration.pk}/image/'
    assert client.get(arbitration_image).content == original
    reviewer_member.enabled = False
    reviewer_member.save(update_fields=['enabled'])
    denied = client.post(arbitration_base + f'{arbitration.pk}/decide/',
                         {'winner_submission_id': submission.pk, 'reason': 'Adopt',
                          'expected_version': 1, 'expected_input_set_hash': arbitration.input_hash},
                         format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
    assert denied.status_code in (403, 404)
    reviewer_member.enabled = True
    reviewer_member.save(update_fields=['enabled'])
    member.role = ProjectMember.Role.REVIEWER
    member.save(update_fields=['role'])
    client.force_authenticate(user=annotator)
    assert client.get(arbitration_base + f'{arbitration.pk}/').status_code in (403, 404)
    assert client.get(arbitration_image).status_code in (403, 404)
    hidden = client.get(arbitration_base)
    assert hidden.status_code == 200 and all(item['id'] != arbitration.pk for item in hidden.json()['results'])
    member.role = ProjectMember.Role.ANNOTATOR
    member.save(update_fields=['role'])
    cancel_key = uuid4()
    client.force_authenticate(user=org.created_by)
    stale_cancel = client.post(arbitration_base + f'{arbitration.pk}/cancel/',
        {'reason': 'Reassign', 'expected_version': 9, 'expected_input_set_hash': arbitration.input_hash},
        format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
    assert stale_cancel.status_code == 409
    cancel_body = {'reason': 'Reassign', 'expected_version': 1, 'expected_input_set_hash': arbitration.input_hash}
    cancelled = client.post(arbitration_base + f'{arbitration.pk}/cancel/', cancel_body,
                            format='json', HTTP_IDEMPOTENCY_KEY=str(cancel_key))
    assert cancelled.status_code == 200 and cancelled.json()['status'] == 'cancelled'
    assert cancelled.json()['receipt']['key'] == str(cancel_key)
    _, replayed = bbox_arbitration.cancel(project, principal, arbitration_id=arbitration.pk,
                                          reason='Reassign', idempotency_key=cancel_key, expected_version=1,
                                          expected_input_set_hash=arbitration.input_hash)
    assert replayed
    arbitration, _ = bbox_arbitration.create(project, principal, batch_id=conflict_batch.pk, task_id=task.pk,
                                             expected_hash=conflict_batch.manifest_hash, assignee_id=reviewer.pk,
                                             idempotency_key=uuid4())
    decision_key = uuid4()
    client.force_authenticate(user=reviewer)
    decision_path = arbitration_base + f'{arbitration.pk}/decide/'
    body = {'winner_submission_id': submission.pk, 'reason': 'Adopt source', 'expected_version': 1,
            'expected_input_set_hash': arbitration.input_hash}
    missing_preconditions = client.post(decision_path, {'winner_submission_id': submission.pk, 'reason': 'Adopt source'},
                                        format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
    assert missing_preconditions.status_code == 400
    stale_decision = client.post(decision_path, {**body, 'expected_version': 1, 'expected_input_set_hash': '0' * 64},
                                 format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
    assert stale_decision.status_code == 409
    forged = client.post(decision_path, {**body, 'actor_id': reviewer.pk}, format='json', HTTP_IDEMPOTENCY_KEY=str(decision_key))
    assert forged.status_code == 400
    decided = client.post(decision_path, body, format='json', HTTP_IDEMPOTENCY_KEY=str(decision_key))
    assert decided.status_code == 201, decided.content
    assert decided.json()['receipt']['actor_id'] == reviewer.pk and decided.json()['winner_submission_id'] == submission.pk
    decision = BboxArbitrationDecision.objects.get(pk=decided.json()['decision_id'])
    bbox_arbitration.checked_decision(decision, arbitration)
    decision.created_at += timedelta(seconds=1)
    with pytest.raises(bbox.Error, match='BBOX_ARBITRATION_DECISION_INVALID'):
        bbox_arbitration.checked_decision(decision, arbitration)
    decision.refresh_from_db()
    assert decision.winner['submission_id'] == submission.pk
    _, replayed = bbox_arbitration.decide(project, local_principal(reviewer), arbitration_id=arbitration.pk,
                                          winner_submission_id=submission.pk, reason='Adopt source',
                                          idempotency_key=decision_key, expected_version=1,
                                          expected_input_set_hash=arbitration.input_hash)
    assert replayed and not hasattr(submission, 'review')
    with pytest.raises(IntegrityError), transaction.atomic():
        type(decision).objects.filter(pk=decision.pk).update(reason='Changed')
    with pytest.raises(bbox.Error, match='BBOX_BATCH_TOO_LARGE'):
        bbox_quality.build_plan(project, list(range(1, bbox_quality.MAX_TASKS + 2)))
    client.force_authenticate(user=annotator)
    settings.ANNOTATION_BBOX_MEDIA_ENABLED = False
    assert client.get(url).status_code == 404
    settings.ANNOTATION_BBOX_MEDIA_ENABLED = True
    task.data = {'image': '/data/upload/other.png'}
    task.save(update_fields=['data'])
    assert client.get(url).status_code == 404
    task.data = {'image': url}
    task.save(update_fields=['data'])
    assignment.cancel()
    assert client.get(url).status_code in (403, 404)


@pytest.mark.django_db
def test_task_linked_gcs_capture_pins_generation(settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = str(tmp_path / 'public')
    settings.STATIC_ROOT = str(tmp_path / 'static')
    settings.APPROVED_EXPORT_ROOT = str(tmp_path / 'private')
    audit_root = tmp_path / 'private' / 'audit-packages'
    audit_root.mkdir(parents=True)
    monkeypatch.setattr(bbox_storage.audit_files, 'private_root', lambda: audit_root)
    org = OrganizationFactory()
    project = ProjectFactory(organization=org, label_config=XML)
    settings.ANNOTATION_BBOX_MEDIA_ENABLED = True
    settings.ANNOTATION_BBOX_MEDIA_PROJECTS = {f'{org.pk}:{project.pk}': 'managed'}
    output = BytesIO()
    Image.new('RGB', (10, 20), 'red').save(output, format='PNG')
    content = output.getvalue()

    class Blob:
        generation = 99
        size = len(content)

        def reload(self, **kwargs):
            pass

        def open(self, mode, **kwargs):
            assert kwargs['if_generation_match'] == 99
            return BytesIO(content)

    class Client:
        def bucket(self, name):
            assert name == 'test-bucket'
            return self

        def blob(self, name):
            assert name == 'images/example.png'
            return Blob()

    monkeypatch.setattr(GCSImportStorage, 'get_client', lambda _: Client())
    storage = GCSImportStorage.objects.create(project=project, bucket='test-bucket', prefix='images')
    task = TaskFactory(project=project, data={settings.DATA_UNDEFINED_NAME: 'gs://test-bucket/images/example.png'})
    client = APIClient()
    client.force_authenticate(user=org.created_by)
    path = f'/api/projects/{project.pk}/bbox-media/tasks/{task.pk}/prepare/'
    assert client.post(path, {}, format='json').status_code == 400
    GCSImportStorageLink.objects.create(storage=storage, task=task, key='images/example.png')
    settings.ANNOTATION_BBOX_EXCLUSIVE_SOURCE_ATTESTATIONS = {
        f'{org.pk}:{project.pk}': {str(task.pk): bbox_prepare.exclusive_source_claim(task, task.data[settings.DATA_UNDEFINED_NAME])}}
    GCSImportStorage.objects.filter(pk=storage.pk).update(prefix='')
    assert client.post(path, {}, format='json').status_code == 400
    GCSImportStorage.objects.filter(pk=storage.pk).update(prefix='images')
    capture = bbox_media.capture_gcs

    def changed_storage(blob):
        result = capture(blob)
        GCSImportStorage.objects.filter(pk=storage.pk).update(prefix='changed')
        return result

    monkeypatch.setattr(bbox_media, 'capture_gcs', changed_storage)
    assert client.post(path, {}, format='json').status_code == 400
    GCSImportStorage.objects.filter(pk=storage.pk).update(prefix='images')
    monkeypatch.setattr(bbox_media, 'capture_gcs', capture)
    response = client.post(path, {}, format='json')
    assert response.status_code == 201, response.content
    task.refresh_from_db()
    assert task.bbox_media_pin.source['generation'] == '99'
    encoded = base64.urlsafe_b64encode(b'gs://test-bucket/images/example.png').decode()
    legacy = client.get(f'/tasks/{task.pk}/resolve/', {'fileuri': encoded})
    assert legacy.status_code == 303 and legacy['Location'] == task.data['image']
    assert client.get(legacy['Location']).content == content
    outsider = UserFactory(active_organization=org)
    ProjectMember.objects.create(project=project, user=outsider, role=ProjectMember.Role.ANNOTATOR)
    client.force_authenticate(user=outsider)
    unassigned = client.get(f'/tasks/{task.pk}/resolve/', {'fileuri': encoded})
    if unassigned.status_code == 303:
        assert client.get(unassigned['Location']).status_code in (403, 404)
    else:
        assert unassigned.status_code in (403, 404)
    client.force_authenticate(user=org.created_by)
    assert client.get(f'/projects/{project.pk}/resolve/', {'fileuri': encoded}).status_code == 404
    foreign = ProjectFactory(organization=org, created_by=org.created_by)
    GCSImportStorage.objects.create(project=foreign, bucket='test-bucket', prefix='images')
    foreign_task = TaskFactory(project=foreign, data={'image': 'gs://test-bucket/images/example.png'})
    monkeypatch.setattr(type(task), 'resolve_storage_uri', lambda *_: {'url': 'https://example.invalid/original'})
    assert client.get(f'/tasks/{foreign_task.pk}/resolve/', {'fileuri': encoded}).status_code == 404
    resolved = client.get(f'/api/tasks/{task.pk}/?resolve_uri=true')
    assert resolved.status_code == 200 and resolved.json()['data']['image'] == task.data['image']
    assert client.get(task.data['image']).content == content


@pytest.mark.django_db
def test_task_linked_s3_capture_uses_single_bounded_response(settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = str(tmp_path / 'public')
    settings.STATIC_ROOT = str(tmp_path / 'static')
    settings.APPROVED_EXPORT_ROOT = str(tmp_path / 'private')
    audit_root = tmp_path / 'private' / 'audit-packages'
    audit_root.mkdir(parents=True)
    monkeypatch.setattr(bbox_storage.audit_files, 'private_root', lambda: audit_root)
    org = OrganizationFactory()
    project = ProjectFactory(organization=org, label_config=XML)
    settings.ANNOTATION_BBOX_MEDIA_ENABLED = True
    settings.ANNOTATION_BBOX_MEDIA_PROJECTS = {f'{org.pk}:{project.pk}': 'managed'}
    output = BytesIO()
    Image.new('RGB', (10, 20), 'red').save(output, format='PNG')
    content = output.getvalue()

    class Stream(BytesIO):
        def iter_chunks(self, chunk_size):
            while chunk := self.read(chunk_size):
                yield chunk

    def stream(storage, reference):
        assert reference == 's3://test-bucket/images/example.png'
        return Stream(content), 'image/png', {'StatusCode': 200, 'ContentLength': len(content)}

    monkeypatch.setattr(S3ImportStorage, 'get_bytes_stream', stream)
    storage = S3ImportStorage.objects.create(project=project, bucket='test-bucket', prefix='images')
    task = TaskFactory(project=project, data={'image': 's3://test-bucket/images/example.png'})
    S3ImportStorageLink.objects.create(storage=storage, task=task, key='images/example.png')
    settings.ANNOTATION_BBOX_EXCLUSIVE_SOURCE_ATTESTATIONS = {
        f'{org.pk}:{project.pk}': {str(task.pk): bbox_prepare.exclusive_source_claim(task, task.data['image'])}}
    client = APIClient()
    client.force_authenticate(user=org.created_by)
    response = client.post(f'/api/projects/{project.pk}/bbox-media/tasks/{task.pk}/prepare/', {}, format='json')
    assert response.status_code == 201, response.content
    task.refresh_from_db()
    assert task.bbox_media_pin.source['kind'] == 's3'
    encoded = base64.urlsafe_b64encode(b's3://test-bucket/images/example.png?ignored=1').decode()
    legacy = client.get(f'/tasks/{task.pk}/resolve/', {'fileuri': encoded})
    assert legacy.status_code == 303 and legacy['Location'] == task.data['image']
    assert client.get(legacy['Location']).content == content
    assert client.get(f'/projects/{project.pk}/resolve/', {'fileuri': encoded}).status_code == 404
    monkeypatch.setattr(type(task), 'resolve_storage_uri', lambda *_: {'url': 'https://example.invalid/other', 'presign_ttl': 1})
    other = base64.urlsafe_b64encode(b's3://test-bucket/images/unrelated.png').decode()
    assert client.get(f'/tasks/{task.pk}/resolve/', {'fileuri': other}).status_code == 303
    resolved = client.get(f'/api/tasks/{task.pk}/?resolve_uri=true')
    assert resolved.status_code == 200 and resolved.json()['data']['image'] == task.data['image']
    assert client.get(task.data['image']).content == content
