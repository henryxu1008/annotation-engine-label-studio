from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import timedelta
import json
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from classification_disagreements.blind_quality_boundary import is_quality_only_submission, with_quality_snapshot
from classification_disagreements.blind_reference import checked_blind_reference
from classification_disagreements.models import BlindGoldScore, BlindQualityAssignment, ClassificationGoldEvidence
from data_export.approved import source_record
from data_export.boundary import has_immutable_release_boundary
from data_export.models import Export
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.utils import timezone
from io_storages.localfiles.models import LocalFilesExportStorage
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.test import APITestCase
from review_batches.service import valid_source
from tasks.models import Annotation, ReviewDecision, Submission, Task, TaskAssignment
from tasks.submissions import review_submission
from tasks.tests import test_submission_review_workflow as fixtures
from classification_disagreements.tests import test_blind_provenance as provenance_fixtures
from projects.tests.factories import ProjectFactory
from projects.models import Project
from tasks.tests.factories import TaskFactory
from users.tests.factories import UserFactory


def bind_marker(assignment, reference=None):
    return BlindQualityAssignment.objects.create(
        assignment_id=assignment.pk, task_id=assignment.task_id, project_id=assignment.project_id,
        assignee_id=assignment.assignee_id,
        reference_id=reference.pk if reference else None,
        reference_hash=reference.manifest_hash if reference else None,
    )


def synthetic_reference(project):
    return ClassificationGoldEvidence.objects.create(
        project=project, created_by=project.created_by, actor_id_frozen=project.created_by_id,
        principal='local:test', principal_source='local', kind='reference', idempotency_key=uuid4(),
        request_hash='0' * 64, manifest={}, manifest_hash='1' * 64,
    )


class TestBlindQualityBoundary(APITestCase):
    setUpTestData = classmethod(fixtures.TestSubmissionReviewWorkflow.setUpTestData.__func__)
    setUp = fixtures.TestSubmissionReviewWorkflow.setUp
    _review = fixtures.TestSubmissionReviewWorkflow._review

    def test_opaque_handles_are_stable_owner_bound_and_never_grant_stale_access(self):
        from dataclasses import replace
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import HANDLE_BASE, HANDLE_LIMIT, issue_handle, resolve_handle
        from classification_disagreements.models import BlindTaskHandle
        from rest_framework.exceptions import NotFound

        principal = local_principal(self.annotator)
        handle = issue_handle(principal=principal, assignment_id=self.assignment.pk)
        assert issue_handle(principal=principal, assignment_id=self.assignment.pk).pk == handle.pk
        assert HANDLE_BASE <= handle.task_handle < HANDLE_LIMIT
        assert HANDLE_BASE <= handle.assignment_handle < HANDLE_LIMIT
        assert resolve_handle(principal=principal, task_handle=handle.task_handle,
                              assignment_handle=handle.assignment_handle).pk == self.assignment.pk
        for actor, token, assignment_token in (
            (local_principal(self.other_user), handle.task_handle, handle.assignment_handle),
            (local_principal(self.manager), handle.task_handle, handle.assignment_handle),
            (principal, self.task.pk, handle.assignment_handle),
            (principal, handle.task_handle, HANDLE_BASE),
        ):
            with self.assertRaises(NotFound):
                resolve_handle(principal=actor, task_handle=token, assignment_handle=assignment_token)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindTaskHandle.objects.filter(pk=handle.pk).update(actor_id_frozen=self.other_user.pk)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindTaskHandle.objects.filter(pk=handle.pk).delete()
        with self.assertRaises(NotFound):
            resolve_handle(principal=replace(principal, source='external'), task_handle=handle.task_handle)
        with self.assertRaises(NotFound):
            issue_handle(principal=replace(principal, principal_id='different'), assignment_id=self.assignment.pk)
        self.assignment.cancel()
        with self.assertRaises(NotFound):
            resolve_handle(principal=principal, task_handle=handle.task_handle)
        replacement = TaskAssignment.objects.create(task=self.task, project=self.project,
                                                    assignee=self.annotator, assigned_by=self.manager)
        replacement_handle = issue_handle(principal=principal, assignment_id=replacement.pk)
        assert replacement_handle.task_handle != handle.task_handle
        assert replacement_handle.assignment_handle != handle.assignment_handle
        with self.assertRaises(NotFound):
            resolve_handle(principal=principal, task_handle=handle.task_handle)

    def test_opaque_handle_collision_retries_and_keeps_existing_binding(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import HANDLE_BASE, issue_handle

        principal = local_principal(self.annotator)
        first = issue_handle(principal=principal, assignment_id=self.assignment.pk)
        task = TaskFactory(project=self.project)
        second = TaskAssignment.objects.create(task=task, project=self.project,
                                               assignee=self.annotator, assigned_by=self.manager)
        with patch('classification_disagreements.blind_task_handles._token', side_effect=[
            first.task_handle, HANDLE_BASE + 12, HANDLE_BASE + 13, HANDLE_BASE + 14,
        ]) as random:
            handle = issue_handle(principal=principal, assignment_id=second.pk)
        assert random.call_count == 4
        assert handle.task_handle == HANDLE_BASE + 13
        assert handle.assignment_handle == HANDLE_BASE + 14

    def test_opaque_write_token_is_owner_bound_and_fences_stale_versions(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import issue_handle, lock_handle_for_write
        from rest_framework.exceptions import NotFound
        from tasks.exceptions import AssignmentConflictError

        principal = local_principal(self.annotator)
        handle = issue_handle(principal=principal, assignment_id=self.assignment.pk)
        args = {'principal': principal, 'task_handle': handle.task_handle,
                'assignment_handle': handle.assignment_handle, 'assignment_version': self.assignment.version}
        with transaction.atomic():
            assert lock_handle_for_write(**args).pk == self.assignment.pk
        with transaction.atomic(), self.assertRaises(NotFound):
            lock_handle_for_write(**{**args, 'principal': local_principal(self.other_user)})
        with transaction.atomic(), self.assertRaises(NotFound):
            lock_handle_for_write(**{**args, 'assignment_handle': handle.assignment_handle + 1})
        self.assignment.version += 1
        self.assignment.save(update_fields=['version'])
        with transaction.atomic(), self.assertRaises(AssignmentConflictError):
            lock_handle_for_write(**args)
        with transaction.atomic():
            assert lock_handle_for_write(**{**args, 'assignment_version': self.assignment.version}).pk == self.assignment.pk

    def test_annotation_write_adapter_binds_handle_to_task_and_version(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import issue_handle

        principal = local_principal(self.annotator)
        handle = issue_handle(principal=principal, assignment_id=self.assignment.pk)
        other_task = TaskFactory(project=self.project)
        other_assignment = TaskAssignment.objects.create(task=other_task, project=self.project,
                                                         assignee=self.annotator, assigned_by=self.manager)
        other_handle = issue_handle(principal=principal, assignment_id=other_assignment.pk)
        self.client.force_authenticate(self.annotator)
        url = f'/api/tasks/{self.task.pk}/annotations/'
        payload = {'result': [], 'task_handle': handle.task_handle,
                   'assignment_id': handle.assignment_handle, 'assignment_version': self.assignment.version,
                   'submit_for_review': True}
        wrong = {**payload, 'task_handle': other_handle.task_handle, 'assignment_id': other_handle.assignment_handle}
        assert self.client.post(url, wrong, format='json').status_code == 409
        assert self.client.post(url, {**payload, 'assignment_id': self.assignment.pk}, format='json').status_code == 404
        assert self.client.post(url, {**payload, 'assignment_version': True}, format='json').status_code == 400
        response = self.client.post(url, payload, format='json')
        assert response.status_code == 201, response.json()
        annotation_id = response.json()['id']
        assert Submission.objects.filter(assignment=self.assignment, revision=1).exists()
        stale = self.client.patch(f'/api/annotations/{annotation_id}/', payload, format='json')
        assert stale.status_code == 409
        self.assignment.refresh_from_db()
        fresh = self.client.patch(f'/api/annotations/{annotation_id}/',
                                  {**payload, 'assignment_version': self.assignment.version}, format='json')
        assert fresh.status_code == 200, fresh.json()

    def test_draft_write_adapter_rechecks_current_assignment(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import issue_handle
        from tasks.models import AnnotationDraft

        handle = issue_handle(principal=local_principal(self.annotator), assignment_id=self.assignment.pk)
        token = {'task_handle': handle.task_handle, 'assignment_id': handle.assignment_handle,
                 'assignment_version': self.assignment.version}
        self.client.force_authenticate(self.annotator)
        created = self.client.post(f'/api/tasks/{self.task.pk}/drafts', {**token, 'result': []}, format='json')
        assert created.status_code == 201, created.json()
        draft = AnnotationDraft.objects.get(pk=created.json()['id'])
        url = f'/api/drafts/{draft.pk}/'
        changed = self.client.patch(url, {**token, 'result': [], 'draft_revision': draft.revision}, format='json')
        assert changed.status_code == 200, changed.json()
        draft.refresh_from_db()
        self.assignment.version += 1
        self.assignment.save(update_fields=['version'])
        stale = self.client.patch(url, {**token, 'result': [], 'draft_revision': draft.revision}, format='json')
        assert stale.status_code == 409
        self.assignment.cancel()
        revoked = self.client.patch(url, {**token, 'assignment_version': self.assignment.version,
                                          'result': [], 'draft_revision': draft.revision}, format='json')
        assert revoked.status_code in (403, 404, 409)

    def test_resource_handles_bind_exact_draft_and_annotation_and_revoke(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_resource_handles import issue_resource_handle, resolve_resource_handle
        from classification_disagreements.models import BlindResourceHandle
        from rest_framework.exceptions import NotFound
        from tasks.models import AnnotationDraft

        principal = local_principal(self.annotator)
        draft = AnnotationDraft.objects.create(task=self.task, assignment=self.assignment, user=self.annotator, result=[])
        first = issue_resource_handle(principal=principal, assignment_id=self.assignment.pk, kind='draft', object_id=draft.pk)
        assert issue_resource_handle(principal=principal, assignment_id=self.assignment.pk,
                                     kind='draft', object_id=draft.pk).pk == first.pk
        assert resolve_resource_handle(principal=principal, kind='draft', handle=first.handle).pk == draft.pk
        for actor, kind, token in ((local_principal(self.other_user), 'draft', first.handle),
                                   (principal, 'annotation', first.handle), (principal, 'draft', draft.pk)):
            with self.assertRaises(NotFound):
                resolve_resource_handle(principal=actor, kind=kind, handle=token)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindResourceHandle.objects.filter(pk=first.pk).update(object_id_frozen=draft.pk + 1)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindResourceHandle.objects.filter(pk=first.pk).delete()
        draft.delete()
        replacement = AnnotationDraft.objects.create(task=self.task, assignment=self.assignment,
                                                      user=self.annotator, result=[])
        second = issue_resource_handle(principal=principal, assignment_id=self.assignment.pk,
                                       kind='draft', object_id=replacement.pk)
        assert second.handle != first.handle
        with self.assertRaises(NotFound):
            resolve_resource_handle(principal=principal, kind='draft', handle=first.handle)
        replacement.delete()
        annotation, _ = self._submit_new_annotation()
        token = issue_resource_handle(principal=principal, assignment_id=self.assignment.pk,
                                      kind='annotation', object_id=annotation.pk)
        assert resolve_resource_handle(principal=principal, kind='annotation', handle=token.handle).pk == annotation.pk
        self.assignment.cancel()
        with self.assertRaises(NotFound):
            resolve_resource_handle(principal=principal, kind='annotation', handle=token.handle)

    def test_quality_resource_handle_is_revoked_after_reference_exposure(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_provenance import record_project_file_access
        from classification_disagreements.blind_resource_handles import issue_resource_handle, resolve_resource_handle
        from classification_disagreements.tests.test_gold_scoring import answer
        from rest_framework.exceptions import NotFound

        self.qualified_submission(answer('Positive'))
        principal = local_principal(self.annotator)
        handle = issue_resource_handle(principal=principal, assignment_id=self.assignment.pk,
                                       kind='annotation', object_id=self.assignment.annotation_id)
        assert resolve_resource_handle(principal=principal, kind='annotation', handle=handle.handle)
        record_project_file_access(account_id=self.annotator.pk, project_id=self.project.pk)
        with self.assertRaises(NotFound):
            resolve_resource_handle(principal=principal, kind='annotation', handle=handle.handle)

    def test_blind_editor_read_returns_only_owned_opaque_identifiers(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import issue_handle
        from classification_disagreements.tests.test_gold_scoring import CONFIG
        from tasks.models import AnnotationDraft

        Project.objects.filter(pk=self.project.pk).update(label_config=CONFIG)
        Task.objects.filter(pk=self.task.pk).update(data={'text': 'example', 'answer_key': 'private'})
        self.project.refresh_from_db()
        self.task.refresh_from_db()
        annotation, _ = self._submit_new_annotation()
        draft = AnnotationDraft.objects.create(task=self.task, assignment=self.assignment, user=self.annotator,
                                               annotation=annotation, result=[])
        handle = issue_handle(principal=local_principal(self.annotator), assignment_id=self.assignment.pk)
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        url = f'/api/tasks/blind/{handle.task_handle}/'
        with self.settings(**config):
            self.client.force_authenticate(self.annotator)
            response = self.client.get(url)
            assert response.status_code == 200, response.json()
            assert self.client.get(url.rstrip('/')).status_code == 200
            body = response.json()
            assert body['id'] == handle.task_handle and body['assignment_id'] == handle.assignment_handle
            assert body['data'] == {'text': 'example'} and body['project'] == self.project.pk
            assert body['annotations'][0]['id'] != annotation.pk
            assert body['annotations'][0]['task'] == handle.task_handle
            assert body['annotations'][0]['completed_by'] == self.annotator.pk
            assert body['default_selected_annotation'] == body['annotations'][0]['id']
            assert body['can_label'] is True and body['predictions'] == []
            assert body['drafts'][0]['id'] != draft.pk
            assert body['drafts'][0]['annotation'] == body['annotations'][0]['id']
            assert response['Cache-Control'] == 'private, no-store'
            with self.settings(ANNOTATION_PROJECT_OPERATION_PROVIDER=
                               'tasks.tests.test_project_operation_policy.PausedProvider'):
                paused = self.client.get(url)
                assert paused.status_code == 200 and paused.json()['can_label'] is False
                blocked = self.client.patch(
                    f'/api/tasks/blind/{handle.task_handle}/drafts/{body["drafts"][0]["id"]}/',
                    {'assignment_id': handle.assignment_handle, 'assignment_version': self.assignment.version,
                     'draft_revision': draft.revision, 'result': []}, format='json')
                assert blocked.status_code == 403
            assert self.client.get(f'/api/tasks/blind/{self.task.pk}/').status_code == 404
            self.client.force_authenticate(self.other_user)
            assert self.client.get(url).status_code == 404
            self.client.force_authenticate(self.annotator)
            from projects.models import ProjectMember
            ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=False)
            assert self.client.get(url).status_code == 404
            ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=True)
            self.assignment.cancel()
            assert self.client.get(url).status_code == 404
        self.client.force_authenticate(self.annotator)
        assert self.client.get(url).status_code == 404

    def test_legacy_resource_routes_deny_participants_but_keep_manager_and_handle_access(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import issue_handle
        from classification_disagreements.tests.test_gold_scoring import CONFIG
        from data_manager.models import View
        from tasks.models import AnnotationDraft

        Project.objects.filter(pk=self.project.pk).update(label_config=CONFIG)
        Task.objects.filter(pk=self.task.pk).update(data={'text': 'example'})
        self.project.refresh_from_db()
        annotation, _ = self._submit_new_annotation()
        draft = AnnotationDraft.objects.create(task=self.task, assignment=self.assignment, user=self.annotator,
                                               annotation=annotation, result=[])
        handle = issue_handle(principal=local_principal(self.annotator), assignment_id=self.assignment.pk)
        view = View.objects.create(project=self.project, user=self.manager,
                                   selected_items={'all': False, 'included': [self.task.pk]})
        routes = [f'/api/tasks/{self.task.pk}/', f'/api/tasks/{self.task.pk}/annotations/',
                  f'/api/tasks/{self.task.pk}/drafts',
                  f'/api/tasks/{self.task.pk}/annotations/{annotation.pk}/drafts',
                  f'/api/tasks/{self.task.pk}/agreement/', f'/api/annotations/{annotation.pk}/',
                  f'/api/annotations/{annotation.pk}/convert-to-draft', f'/api/drafts/{draft.pk}/']
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        with self.settings(**config):
            self.client.force_authenticate(self.annotator)
            assert self.client.get('/api/tasks/', {'project': self.project.pk}).status_code == 404
            assert self.client.get('/api/tasks/', {'view': view.pk}).status_code == 404
            views = self.client.get('/api/dm/views/', {'project': self.project.pk})
            assert views.status_code == 200 and not any(row['id'] == view.pk for row in views.data)
            assert self.client.get(f'/api/dm/views/{view.pk}/').status_code == 404
            assert self.client.post('/api/dm/views/', {'project': self.project.pk, 'data': {}},
                                    format='json').status_code == 404
            assert self.client.get(f'/api/projects/{self.project.pk}/my-work/', {'kind': 'annotation'}).status_code == 404
            assert self.client.get(f'/api/projects/{self.project.pk}/tasks/').status_code == 404
            assert self.client.get(f'/api/projects/{self.project.pk}/label-stream-history/').status_code == 404
            assert self.client.get(f'/api/projects/{self.project.pk}/task-packages/').status_code == 404
            for url in routes:
                response = self.client.get(url)
                assert response.status_code == 404, (url, response.status_code)
            for url in (routes[1], routes[2], routes[5], routes[6], routes[7]):
                response = self.client.post(url, {}, format='json')
                assert response.status_code == 404, (url, response.status_code)
            for url in (routes[0], routes[5], routes[7]):
                assert self.client.patch(url, {}, format='json').status_code == 404
                assert self.client.delete(url).status_code == 404
            assert self.client.get(f'/api/tasks/blind/{handle.task_handle}/').status_code == 200
            self.client.force_authenticate(self.other_user)
            assert self.client.get(routes[0]).status_code in (403, 404)
            self.client.force_authenticate(self.annotator)
            self.client.force_authenticate(self.manager)
            assert self.client.get(routes[0]).status_code == 200
            assert self.client.get('/api/tasks/', {'project': self.project.pk}).status_code == 200
            assert self.client.get(f'/api/dm/views/{view.pk}/').status_code == 200
        self.client.force_authenticate(self.annotator)
        assert self.client.get(routes[0]).status_code == 200

    def test_sensitive_submissions_keep_ordinary_review_and_deny_participant_raw_snapshots(self):
        from classification_disagreements.blind_provenance import account_has_untainted_privilege_history
        from classification_disagreements.models import BlindPrivilegeHistory
        from classification_disagreements.tests.test_gold_scoring import answer
        from data_manager.models import View
        from projects.models import ProjectMember

        original_annotator = self.annotator
        _, ordinary = self._submit_new_annotation()
        self.task = TaskFactory(project=self.project, data={'text': 'quality candidate'})
        self.assignment = TaskAssignment.objects.create(task=self.task, project=self.project,
                                                        assignee=original_annotator, assigned_by=self.manager)
        _, quality = self.qualified_submission(answer('Positive'))
        view = View.objects.create(project=self.project, user=self.manager,
                                   selected_items={'all': False, 'included': [self.task.pk]})
        self.client.force_authenticate(original_annotator)
        assert self.client.get(f'/api/submissions/{ordinary.pk}/').status_code == 404
        assert ordinary.pk not in [row['id'] for row in self.client.get('/api/submissions/').data]
        self.client.force_authenticate(self.annotator)
        assert self.client.get(f'/api/submissions/{quality.pk}/').status_code == 404
        assert quality.pk not in [row['id'] for row in self.client.get('/api/submissions/').data]
        self.client.force_authenticate(self.reviewer)
        assert self.client.get(f'/api/dm/views/{view.pk}/').status_code == 200
        assert self.client.patch(f'/api/dm/views/{view.pk}/', {'data': {'title': 'review'}},
                                 format='json').status_code == 200
        assert all(row['id'] != view.pk for row in self.client.get('/api/dm/views/').data)
        shell = self.client.get('/api/tasks/', {'project': self.project.pk})
        assert shell.status_code == 200, shell.data
        assert shell.data['tasks'] == []
        queue = self.client.get('/api/submissions/', {'project': self.project.pk, 'reviewable': 'true'})
        assert queue.status_code == 200
        assert ordinary.pk in [row['id'] for row in queue.data['results']]
        assert quality.pk not in [row['id'] for row in queue.data['results']]
        assert ordinary.pk not in [row['id'] for row in self.client.get('/api/submissions/').data]
        assert self.client.get(f'/api/submissions/{ordinary.pk}/').status_code == 200
        assert self.client.get(f'/api/submissions/{quality.pk}/').status_code == 404
        response = self.client.post(f'/api/submissions/{ordinary.pk}/review/',
                                    {'decision': 'approved'}, format='json')
        assert response.status_code == 200, response.data
        assert self.client.post(f'/api/submissions/{quality.pk}/review/',
                                {'decision': 'approved'}, format='json').status_code in (403, 404)
        ProjectMember.objects.filter(project=self.project, user=self.reviewer).update(role='annotator')
        assert BlindPrivilegeHistory.objects.get(account_id=self.reviewer.pk).elevated
        assert not account_has_untainted_privilege_history(account_id=self.reviewer.pk)
        assert self.client.get(f'/api/submissions/{ordinary.pk}/').status_code == 404
        assert self.client.patch(f'/api/dm/views/{view.pk}/', {'data': {}}, format='json').status_code == 404
        assert self.client.get('/api/tasks/', {'project': self.project.pk}).status_code == 404

    def test_blind_editor_read_revokes_quality_after_exposure(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_provenance import record_project_file_access
        from classification_disagreements.blind_task_handles import issue_handle
        from classification_disagreements.tests.test_gold_scoring import answer

        self.qualified_submission(answer('Positive'))
        handle = issue_handle(principal=local_principal(self.annotator), assignment_id=self.assignment.pk)
        self.client.force_authenticate(self.annotator)
        url = f'/api/tasks/blind/{handle.task_handle}/'
        response = self.client.get(url)
        assert response.status_code == 200, response.json()
        assert response.json()['annotations'][0]['id'] != self.assignment.annotation_id
        record_project_file_access(account_id=self.annotator.pk, project_id=self.project.pk)
        assert self.client.get(url).status_code == 404

    def test_blind_text_projection_fails_closed_for_unusable_input(self):
        from types import SimpleNamespace
        from classification_disagreements.blind_editor_api import participant_text_data
        from classification_disagreements.tests.test_gold_scoring import CONFIG
        from rest_framework.exceptions import NotFound

        project = SimpleNamespace(label_config=CONFIG)
        assert participant_text_data(project, SimpleNamespace(data={'text': 'shown', 'answer': 'hidden'})) == {
            'text': 'shown',
        }
        project_with_context = SimpleNamespace(label_config=CONFIG.replace(
            '</View>', '<Text name="context" value="$context"/></View>',
        ))
        assert participant_text_data(project_with_context, SimpleNamespace(
            data={'text': 'shown', 'context': 'also shown', 'answer': 'hidden'},
        )) == {'text': 'shown', 'context': 'also shown'}
        for data in ({'answer': 'hidden'}, {'text': {'nested': 'not text'}}, []):
            with self.assertRaises(NotFound):
                participant_text_data(project, SimpleNamespace(data=data))
        with self.assertRaises(NotFound):
            participant_text_data(SimpleNamespace(label_config='<View><Image name="image" value="$text"/></View>'),
                                  SimpleNamespace(data={'text': 'shown'}))

    def test_blind_work_list_uses_stable_opaque_ids_and_live_access(self):
        from classification_disagreements.tests.test_gold_scoring import CONFIG

        Project.objects.filter(pk=self.project.pk).update(label_config=CONFIG)
        self.project.refresh_from_db()
        other_task = TaskFactory(project=self.project, data={'text': 'ordinary', 'private_answer': 'hidden'})
        other = TaskAssignment.objects.create(task=other_task, project=self.project,
                                              assignee=self.annotator, assigned_by=self.manager)
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        url = f'/api/projects/{self.project.pk}/blind-work/'
        self.client.force_authenticate(self.annotator)
        with self.settings(**config):
            response = self.client.get(url)
            assert response.status_code == 200, response.json()
            rows = response.json()['results']
            assert len(rows) == 2 and not response.json()['has_next']
            assert response.json()['total'] == 2 and response.json()['tasks'] == rows
            assert all('private_answer' not in row['data'] and row['can_label'] for row in rows)
            assert all(row['task_id'] not in (self.task.pk, other_task.pk)
                       and row['assignment_id'] not in (self.assignment.pk, other.pk) for row in rows)
            assert self.client.get(url).json()['results'] == rows
            assert all(self.client.get(row['read_url']).status_code == 200 for row in rows)
            with self.settings(ANNOTATION_PROJECT_OPERATION_PROVIDER=
                               'tasks.tests.test_project_operation_policy.PausedProvider'):
                paused = self.client.get(url)
                assert paused.status_code == 200 and all(not row['can_label'] for row in paused.json()['results'])
            assert self.client.get(url + '?page=1&page=2').status_code == 404
            self.client.force_authenticate(self.other_user)
            assert self.client.get(url).status_code in (403, 404)
            self.client.force_authenticate(self.annotator)
            other.cancel()
            assert len(self.client.get(url).json()['results']) == 1
        assert self.client.get(url).status_code == 404

    def test_blind_work_excludes_oversized_task_without_blocking_other_work(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import issue_handle
        from classification_disagreements.gold_api import MAX_BYTES
        from classification_disagreements.tests.test_gold_scoring import CONFIG

        Project.objects.filter(pk=self.project.pk).update(label_config=CONFIG)
        Task.objects.filter(pk=self.task.pk).update(data={'text': 'before'})
        handle = issue_handle(principal=local_principal(self.annotator), assignment_id=self.assignment.pk)
        Task.objects.filter(pk=self.task.pk).update(data={'text': 'x' * (MAX_BYTES + 1)})
        other = TaskFactory(project=self.project, data={'text': 'bounded ordinary work'})
        TaskAssignment.objects.create(task=other, project=self.project, assignee=self.annotator,
                                      assigned_by=self.manager)
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        self.client.force_authenticate(self.annotator)
        with self.settings(**config):
            response = self.client.get(f'/api/projects/{self.project.pk}/blind-work/')
            assert response.status_code == 200 and response.data['total'] == 1
            assert [row['data']['text'] for row in response.data['results']] == ['bounded ordinary work']
            assert self.client.get(f'/api/tasks/blind/{handle.task_handle}/').status_code == 404
            response = self.client.post(f'/api/tasks/blind/{handle.task_handle}/annotations/', {
                'result': [], 'assignment_id': handle.assignment_handle,
                'assignment_version': self.assignment.version,
            }, format='json')
            assert response.status_code == 404
            assert not Annotation.objects.filter(task=self.task).exists()

    def test_blind_editor_rejects_oversized_annotation_draft_and_write(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_resource_handles import issue_resource_handle
        from classification_disagreements.blind_task_handles import issue_handle
        from classification_disagreements.gold_api import MAX_BYTES
        from classification_disagreements.tests.test_gold_scoring import CONFIG
        from tasks.models import AnnotationDraft

        Project.objects.filter(pk=self.project.pk).update(label_config=CONFIG)
        Task.objects.filter(pk=self.task.pk).update(data={'text': 'bounded task'})
        handle = issue_handle(principal=local_principal(self.annotator), assignment_id=self.assignment.pk)
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        oversized = [{'value': {'text': 'x' * (MAX_BYTES + 1)}}]
        self.client.force_authenticate(self.annotator)
        with self.settings(**config):
            payload = {'result': oversized, 'assignment_id': handle.assignment_handle,
                       'assignment_version': self.assignment.version, 'submit_for_review': True}
            response = self.client.post(f'/api/tasks/blind/{handle.task_handle}/annotations/', payload, format='json')
            assert response.status_code == 400
            assert not Annotation.objects.filter(task=self.task).exists()
            annotation = Annotation.objects.create(task=self.task, project=self.project,
                                                   completed_by=self.annotator, result=oversized)
            self.assignment.annotation = annotation
            self.assignment.save(update_fields=['annotation'])
            url = f'/api/tasks/blind/{handle.task_handle}/'
            assert self.client.get(url).status_code == 404
            Annotation.objects.filter(pk=annotation.pk).update(result=[])
            resource = issue_resource_handle(principal=local_principal(self.annotator),
                                             assignment_id=self.assignment.pk, kind='annotation',
                                             object_id=annotation.pk)
            Annotation.objects.filter(pk=annotation.pk).update(result=oversized)
            response = self.client.patch(f'{url}annotations/{resource.handle}/', {
                'result': [], 'assignment_id': handle.assignment_handle,
                'assignment_version': self.assignment.version,
            }, format='json')
            assert response.status_code == 404
            Annotation.objects.filter(pk=annotation.pk).update(result=[])
            AnnotationDraft.objects.create(task=self.task, assignment=self.assignment, user=self.annotator,
                                           annotation=annotation, result=oversized)
            assert self.client.get(url).status_code == 404

    def test_data_manager_project_state_hides_other_work_for_participants(self):
        from tasks.tests.factories import PredictionFactory

        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        url = f'/api/dm/project/?project={self.project.pk}'
        columns_url = f'/api/dm/columns/?project={self.project.pk}'
        fields = ('task_count', 'annotation_count', 'task_number', 'total_annotations_number')
        self.client.force_authenticate(self.annotator)
        with self.settings(**config):
            before = self.client.get(url)
            assert before.status_code == 200, before.json()
            before_columns = self.client.get(columns_url)
            assert before_columns.status_code == 200
            other_task = TaskFactory(project=self.project, data={'text': 'other', 'private_answer': 'hidden'})
            Annotation.objects.create(task=other_task, project=self.project, completed_by=self.manager, result=[])
            PredictionFactory(task=other_task, project=self.project, model_version='private-model', result=[])
            after = self.client.get(url)
            assert after.status_code == 200, after.json()
            after_columns = self.client.get(columns_url)
            assert after_columns.status_code == 200
            assert before_columns.json() == after_columns.json()
            assert not any(column.get('id') == 'private_answer' for column in after_columns.json()['columns'])
            model_columns = [column for column in after_columns.json()['columns']
                             if column.get('id') == 'predictions_model_versions']
            assert model_columns and model_columns[0]['schema']['items'] == []
            assert {key: before.json()[key] for key in fields} == {key: after.json()[key] for key in fields}
            assert after.json()['task_count'] == 1 and after.json()['annotation_count'] == 0
            assert not after.json()['can_manage_tasks'] and not after.json()['can_manage_annotations']
            sparse = self.client.get(url + '&fields=id')
            assert sparse.status_code == 200, sparse.json()
            assert sparse.json()['task_count'] == 1 and sparse.json()['annotation_count'] == 0
            self.client.force_authenticate(self.manager)
            manager = self.client.get(url)
            assert manager.status_code == 200, manager.json()
            assert manager.json()['task_count'] == 2 and manager.json()['annotation_count'] == 1
            assert manager.json()['can_manage_tasks']
            manager_columns = self.client.get(columns_url).json()['columns']
            assert any(column.get('id') == 'private_answer' for column in manager_columns)
            assert any('private-model' in column.get('schema', {}).get('items', [])
                       for column in manager_columns if column.get('id') == 'predictions_model_versions')
        self.client.force_authenticate(self.annotator)
        ordinary_columns = self.client.get(columns_url).json()['columns']
        assert any(column.get('id') == 'private_answer' for column in ordinary_columns)
        assert any('private-model' in column.get('schema', {}).get('items', [])
                   for column in ordinary_columns if column.get('id') == 'predictions_model_versions')

    def test_participant_project_queue_never_uses_global_queue_query(self):
        from types import SimpleNamespace
        from django.http import QueryDict
        from projects.serializers import ProjectSerializer

        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        with self.settings(**config), patch('projects.functions.quality_task_ids',
                                           side_effect=AssertionError('global queue query')):
            request = SimpleNamespace(user=self.annotator, query_params=QueryDict(''), method='GET')
            serializer = ProjectSerializer(self.project, context={'request': request})
            assert serializer.get_queue_total(self.project) == 1
            assert serializer.get_queue_done(self.project) == 0

    def test_blind_bootstrap_only_returns_current_participant_projection(self):
        from classification_disagreements.tests.test_gold_scoring import CONFIG

        Project.objects.filter(pk=self.project.pk).update(label_config=CONFIG)
        TaskFactory(project=self.project, data={'text': 'other', 'secret_column': 'hidden'})
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        project_url = f'/api/dm/blind-project/?project={self.project.pk}'
        columns_url = f'/api/dm/blind-columns/?project={self.project.pk}'
        self.client.force_authenticate(self.annotator)
        assert self.client.get(project_url).status_code == 404
        with self.settings(**config):
            project = self.client.get(project_url)
            columns = self.client.get(columns_url)
            assert project.status_code == 200, project.json()
            assert columns.status_code == 200, columns.json()
            assert self.client.get(project_url.replace('blind-project/', 'blind-project')).status_code == 200
            assert self.client.get(columns_url.replace('blind-columns/', 'blind-columns')).status_code == 200
            assert project['Cache-Control'] == 'private, no-store'
            assert columns['Cache-Control'] == 'private, no-store'
            assert project.json()['task_count'] == 1
            assert 'task_data_password' not in project.json()
            assert not any(column.get('id') == 'secret_column' for column in columns.json()['columns'])
            assert self.client.get(project_url + '&project=2').status_code == 404
            assert self.client.get(project_url + '&fields=id').status_code == 404
            self.client.force_authenticate(self.manager)
            assert self.client.get(project_url).status_code == 404
            self.client.force_authenticate(self.other_user)
            assert self.client.get(columns_url).status_code == 404

    def test_blind_write_routes_create_draft_submit_and_resume_by_handle(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import issue_handle

        handle = issue_handle(principal=local_principal(self.annotator), assignment_id=self.assignment.pk)
        token = {'assignment_id': handle.assignment_handle, 'assignment_version': self.assignment.version}
        root = f'/api/tasks/blind/{handle.task_handle}'
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        self.client.force_authenticate(self.annotator)
        with self.settings(**config):
            created = self.client.post(root + '/drafts', {**token, 'result': []}, format='json')
            assert created.status_code == 201, created.json()
            draft = created.json()
            assert draft['task'] == handle.task_handle and draft['id'] >= (1 << 51)
            draft_url = root + f'/drafts/{draft["id"]}'
            updated = self.client.patch(draft_url, {**token, 'result': [], 'draft_revision': draft['revision']},
                                        format='json')
            assert updated.status_code == 200, updated.json()
            draft = updated.json()
            submitted = self.client.post(root + '/annotations', {**token, 'result': [], 'draft_id': draft['id'],
                'draft_revision': draft['revision'], 'submit_for_review': True}, format='json')
            assert submitted.status_code == 201, submitted.json()
            annotation = submitted.json()
            assert annotation['id'] >= (1 << 51) and annotation['task'] == handle.task_handle
            assert Submission.objects.filter(assignment=self.assignment, revision=1).exists()
            assert self.client.patch(draft_url, {**token, 'result': [], 'draft_revision': draft['revision']},
                                     format='json').status_code == 404
            annotation_url = root + f'/annotations/{annotation["id"]}'
            assert self.client.patch(annotation_url, {**token, 'result': []}, format='json').status_code == 409
            token['assignment_version'] = annotation['assignment_version']
            resumed = self.client.patch(annotation_url, {**token, 'result': []}, format='json')
            assert resumed.status_code == 200, resumed.json()
            token['assignment_version'] = resumed.json()['assignment_version']
            attached = self.client.post(root + f'/annotations/{annotation["id"]}/drafts',
                                        {**token, 'result': []}, format='json')
            assert attached.status_code == 201, attached.json()
            attached_draft = attached.json()
            removed = self.client.delete(root + f'/drafts/{attached_draft["id"]}',
                                         {**token, 'draft_revision': attached_draft['revision']}, format='json')
            assert removed.status_code == 204
            self.assignment.refresh_from_db()
            assert self.client.patch(root + f'/annotations/{self.assignment.annotation_id}/',
                                     {**token, 'result': []}, format='json').status_code == 404
            self.client.force_authenticate(self.other_user)
            assert self.client.patch(annotation_url, {**token, 'result': []}, format='json').status_code == 404

    def test_blind_write_accepts_unsaved_draft_sentinel_but_not_an_existing_draft(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import issue_handle

        handle = issue_handle(principal=local_principal(self.annotator), assignment_id=self.assignment.pk)
        token = {'assignment_id': handle.assignment_handle, 'assignment_version': self.assignment.version}
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        self.client.force_authenticate(self.annotator)
        with self.settings(**config):
            root = f'/api/tasks/blind/{handle.task_handle}'
            draft = self.client.post(root + '/drafts', {**token, 'result': [], 'draft_id': 0, 'draft_revision': 0},
                                     format='json')
            assert draft.status_code == 201, draft.data
            stale = self.client.post(root + '/annotations/', {**token, 'result': [], 'draft_id': 0,
                'draft_revision': 0, 'submit_for_review': True}, format='json')
            assert stale.status_code == 409
            assert not Submission.objects.filter(assignment=self.assignment).exists()
            deleted = self.client.delete(root + f'/drafts/{draft.data["id"]}/',
                {**token, 'draft_revision': draft.data['revision']}, format='json')
            assert deleted.status_code == 204, deleted.data
            submitted = self.client.post(root + '/annotations/', {**token, 'result': [], 'draft_id': 0,
                'draft_revision': 0, 'submit_for_review': True}, format='json')
            assert submitted.status_code == 201, submitted.data
            assert Submission.objects.filter(assignment=self.assignment, revision=1).exists()

    def test_blind_write_routes_reject_spoofed_fields_and_rollback_failed_projection(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import issue_handle
        from rest_framework.exceptions import NotFound
        from tasks.models import AnnotationDraft

        handle = issue_handle(principal=local_principal(self.annotator), assignment_id=self.assignment.pk)
        token = {'assignment_id': handle.assignment_handle, 'assignment_version': self.assignment.version}
        url = f'/api/tasks/blind/{handle.task_handle}/drafts'
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        self.client.force_authenticate(self.annotator)
        with self.settings(**config):
            assert self.client.post(url, [1, 2], format='json').status_code == 400
            assert self.client.post(url, {**token, 'result': [], 'task': self.task.pk}, format='json').status_code == 400
            assert self.client.post(url, {**token, 'result': [], 'parent_annotation': 1}, format='json').status_code == 400
            with patch('classification_disagreements.blind_editor_writes.issue_resource_handle',
                       side_effect=NotFound('Work is unavailable.')):
                assert self.client.post(url, {**token, 'result': []}, format='json').status_code == 404
            assert not AnnotationDraft.objects.filter(assignment=self.assignment).exists()

    def test_blind_write_does_not_emit_ordinary_annotation_webhooks(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import issue_handle

        handle = issue_handle(principal=local_principal(self.annotator), assignment_id=self.assignment.pk)
        token = {'assignment_id': handle.assignment_handle, 'assignment_version': self.assignment.version}
        url = f'/api/tasks/blind/{handle.task_handle}/annotations/'
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        self.client.force_authenticate(self.annotator)
        with self.settings(**config), patch('webhooks.utils.emit_webhooks_for_instance') as emit:
            created = self.client.post(url, {**token, 'result': []}, format='json')
            assert created.status_code == 201, created.json()
            updated = self.client.patch(url + f'{created.json()["id"]}/', {**token, 'result': []}, format='json')
            assert updated.status_code == 200, updated.json()
            emit.assert_not_called()

    def test_opaque_handle_retained_without_blocking_project_deletion(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import issue_handle, resolve_handle
        from classification_disagreements.models import BlindTaskHandle
        from rest_framework.exceptions import NotFound

        principal = local_principal(self.annotator)
        handle = issue_handle(principal=principal, assignment_id=self.assignment.pk)
        self.project.delete()
        assert BlindTaskHandle.objects.filter(pk=handle.pk).exists()
        with self.assertRaises(NotFound):
            resolve_handle(principal=principal, task_handle=handle.task_handle)

    def test_opaque_quality_handle_is_revoked_with_eligibility(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_provenance import record_project_file_access
        from classification_disagreements.blind_task_handles import issue_handle, lock_handle_for_write, resolve_handle
        from classification_disagreements.tests.test_gold_scoring import answer
        from rest_framework.exceptions import NotFound

        self.qualified_submission(answer('Positive'))
        principal = local_principal(self.annotator)
        handle = issue_handle(principal=principal, assignment_id=self.assignment.pk)
        assert resolve_handle(principal=principal, task_handle=handle.task_handle).pk == self.assignment.pk
        with transaction.atomic():
            assert lock_handle_for_write(principal=principal, task_handle=handle.task_handle,
                assignment_handle=handle.assignment_handle, assignment_version=self.assignment.version).pk == self.assignment.pk
        record_project_file_access(account_id=self.annotator.pk, project_id=self.project.pk)
        with self.assertRaises(NotFound):
            resolve_handle(principal=principal, task_handle=handle.task_handle)
        with self.assertRaises(NotFound):
            issue_handle(principal=principal, assignment_id=self.assignment.pk)
        with transaction.atomic(), self.assertRaises(NotFound):
            lock_handle_for_write(principal=principal, task_handle=handle.task_handle,
                assignment_handle=handle.assignment_handle, assignment_version=self.assignment.version)

    def test_reissuing_existing_quality_handle_checks_the_live_proof_once(self):
        from access_control.project_operations import local_principal
        from classification_disagreements import blind_provenance
        from classification_disagreements.blind_task_handles import issue_handle
        from classification_disagreements.tests.test_gold_scoring import answer

        self.qualified_submission(answer('Positive'))
        principal = local_principal(self.annotator)
        first = issue_handle(principal=principal, assignment_id=self.assignment.pk)
        with patch.object(blind_provenance, 'checked_quality_proof', wraps=blind_provenance.checked_quality_proof) as check:
            second = issue_handle(principal=principal, assignment_id=self.assignment.pk)
        assert second.pk == first.pk
        assert check.call_count == 1

    def test_editor_checks_quality_once_before_issuing_annotation_and_draft_handles(self):
        from access_control.project_operations import local_principal
        from classification_disagreements import blind_provenance
        from classification_disagreements.blind_task_handles import issue_handle
        from classification_disagreements.tests.test_gold_scoring import answer
        from tasks.models import AnnotationDraft

        self.qualified_submission(answer('Positive'))
        self.assignment.refresh_from_db()
        draft = AnnotationDraft.objects.create(task=self.task, assignment=self.assignment, user=self.annotator,
                                               annotation_id=self.assignment.annotation_id, result=[])
        handle = issue_handle(principal=local_principal(self.annotator), assignment_id=self.assignment.pk)
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        self.client.force_authenticate(self.annotator)
        with self.settings(**config), patch.object(
            blind_provenance, 'checked_quality_proof', wraps=blind_provenance.checked_quality_proof,
        ) as check:
            response = self.client.get(f'/api/tasks/blind/{handle.task_handle}/')
        assert response.status_code == 200, response.data
        assert check.call_count == 1
        assert response.data['annotations'][0]['id'] != self.assignment.annotation_id
        assert response.data['drafts'][0]['id'] != draft.pk

    def test_quality_handle_is_revoked_when_pinned_config_or_task_data_changes(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import issue_handle, resolve_handle
        from classification_disagreements.tests.test_gold_scoring import answer
        from rest_framework.exceptions import NotFound

        self.qualified_submission(answer('Positive'))
        principal = local_principal(self.annotator)
        handle = issue_handle(principal=principal, assignment_id=self.assignment.pk)
        original_config = self.project.label_config
        original_data = self.task.data
        ordinary = TaskFactory(project=self.project, data={'text': 'independent ordinary sample 12345'})
        TaskAssignment.objects.create(task=ordinary, project=self.project, assignee=self.annotator,
                                      assigned_by=self.manager)
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        self.client.force_authenticate(self.annotator)
        url = f'/api/projects/{self.project.pk}/blind-work/'
        with self.settings(**config):
            assert len(self.client.get(url).data['results']) == 2
            Project.objects.filter(pk=self.project.pk).update(label_config=original_config + ' ')
            with self.assertRaises(NotFound):
                resolve_handle(principal=principal, task_handle=handle.task_handle)
            config_changed = self.client.get(url)
            assert config_changed.status_code == 200 and len(config_changed.data['results']) == 1
            Project.objects.filter(pk=self.project.pk).update(label_config=original_config)
            assert resolve_handle(principal=principal, task_handle=handle.task_handle).pk == self.assignment.pk
            changed_data = {**original_data, 'text': original_data['text'][:-1] + 'X'}
            assert len(changed_data['text'].encode()) == len(original_data['text'].encode())
            Task.objects.filter(pk=self.task.pk).update(data=changed_data)
            with self.assertRaises(NotFound):
                resolve_handle(principal=principal, task_handle=handle.task_handle)
            task_changed = self.client.get(url)
            assert task_changed.status_code == 200 and len(task_changed.data['results']) == 1

    def test_project_file_scope_checks_global_fences_in_one_snapshot(self):
        from classification_disagreements.blind_provenance import project_file_scope_supported
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            assert project_file_scope_supported(self.project.pk)
        assert len(queries) == 1
        assert not project_file_scope_supported(-1)

    def test_collection_prefilter_is_not_a_substitute_for_integrity_verification(self):
        from classification_disagreements.blind_provenance import checked_quality_proof, current_quality_assignment_ids
        from classification_disagreements.tests.test_gold_scoring import answer

        self.qualified_submission(answer('Positive'))
        marker = BlindQualityAssignment.objects.get(assignment_id=self.assignment.pk)
        assert self.assignment.pk in set(current_quality_assignment_ids(self.annotator.pk)
                                         .values_list('assignment_id', flat=True))
        # Simulate a failed integrity computation without modifying immutable
        # evidence; the SQL visibility prefilter deliberately cannot detect it.
        with patch('review_batches.service.digest', return_value='0' * 64):
            assert self.assignment.pk in set(current_quality_assignment_ids(self.annotator.pk)
                                             .values_list('assignment_id', flat=True))
            with self.assertRaises(ValidationError):
                checked_quality_proof(assignment=self.assignment, marker=marker)

    def test_quality_tasks_do_not_change_ordinary_project_counts(self):
        from projects.models import ProjectMember
        from tasks.tests.factories import AnnotationFactory, PredictionFactory

        self.enterContext(self.settings(
            ANNOTATION_CLASSIFICATION_BLIND_ENABLED=True,
            ANNOTATION_CLASSIFICATION_BLIND_PROJECTS={f'{self.organization.pk}:{self.project.pk}': True},
        ))
        member = UserFactory(active_organization=self.organization)
        self.organization.add_user(member)
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        AnnotationFactory(task=self.task, completed_by=self.annotator, result=[])
        PredictionFactory(task=self.task)
        self.task.is_labeled = True
        self.task.save(update_fields=['is_labeled'])
        fields = (*Project.objects.COUNTER_FIELDS, 'queue_total', 'queue_done')
        def counts(optimized, user):
            self.client.force_authenticate(user=user)
            with patch('projects.functions.flag_set', return_value=optimized):
                response = self.client.get(f'/api/projects/{self.project.pk}/')
                assert response.status_code == 200, response.data
                return {name: response.data[name] for name in fields}

        cases = [(optimized, user) for optimized in (False, True) for user in (self.annotator, self.manager)]
        before = {(optimized, user.pk): counts(optimized, user) for optimized, user in cases}
        self.client.force_authenticate(user=self.annotator)
        annotators_before = self.client.get(f'/api/projects/{self.project.pk}/annotators/').json()
        versions_before = self.client.get(f'/api/projects/{self.project.pk}/model-versions').json()
        quality = TaskFactory(project=self.project)
        assignment = TaskAssignment.objects.create(task=quality, project=self.project,
                                                   assignee=member, assigned_by=self.manager)
        bind_marker(assignment)
        AnnotationFactory(task=quality, completed_by=member, result=[])
        AnnotationFactory(task=quality, completed_by=member, was_cancelled=True)
        AnnotationFactory(task=quality, completed_by=member, ground_truth=True)
        PredictionFactory(task=quality, model_version='quality-only-version')
        Task.objects.filter(pk=quality.pk).update(is_labeled=True)
        for optimized, user in cases:
            assert counts(optimized, user) == before[optimized, user.pk]
        self.client.force_authenticate(user=self.annotator)
        assert self.client.get(f'/api/projects/{self.project.pk}/annotators/').json() == annotators_before
        assert self.client.get(f'/api/projects/{self.project.pk}/model-versions').json() == versions_before
        assignment.cancel()
        for optimized, user in cases:
            assert counts(optimized, user) == before[optimized, user.pk]

    def test_participant_annotator_directory_is_constant_for_ordinary_and_quality_work(self):
        from projects.models import ProjectMember
        from tasks.tests.factories import AnnotationFactory

        url = f'/api/projects/{self.project.pk}/annotators/'
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        with self.settings(**config):
            self.client.force_authenticate(self.annotator)
            first = self.client.get(url)
            assert first.status_code == 200 and first.json() == []
            assert first['Cache-Control'] == 'private, no-store'
            AnnotationFactory(task=self.task, completed_by=self.annotator, result=[])
            assert self.client.get(url).json() == []
            member = UserFactory(active_organization=self.organization)
            self.organization.add_user(member)
            ProjectMember.objects.create(project=self.project, user=member, role='annotator')
            quality = TaskFactory(project=self.project)
            assignment = TaskAssignment.objects.create(task=quality, project=self.project,
                                                       assignee=member, assigned_by=self.manager)
            bind_marker(assignment)
            AnnotationFactory(task=quality, completed_by=member, result=[])
            assert self.client.get(url).json() == []
            self.client.force_authenticate(self.manager)
            manager = self.client.get(url)
            assert manager.status_code == 200
            ids = {row['id'] for row in manager.json()}
            assert self.annotator.pk in ids and member.pk not in ids
            self.client.force_authenticate(self.reviewer)
            assert self.client.get(url).json() == []
            self.client.force_authenticate(self.other_user)
            assert self.client.get(url).status_code == 404
            ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=False)
            self.client.force_authenticate(self.annotator)
            assert self.client.get(url).status_code == 404

        ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=True)
        # A persisted quality marker keeps the project sensitive after disabling
        # the feature; unmarked projects retain the upstream directory behavior.
        self.client.force_authenticate(self.annotator)
        assert self.client.get(url).json() == []
        ordinary_project = ProjectFactory(organization=self.organization, created_by=self.manager)
        ProjectMember.objects.create(project=ordinary_project, user=self.annotator, role='annotator')
        AnnotationFactory(task=TaskFactory(project=ordinary_project), completed_by=self.annotator, result=[])
        response = self.client.get(f'/api/projects/{ordinary_project.pk}/annotators/')
        assert response.status_code == 200
        assert self.annotator.pk in {row['id'] for row in response.json()}

    def test_organization_contributions_exclude_all_sensitive_project_work(self):
        from tasks.tests.factories import AnnotationFactory

        regular = ProjectFactory(organization=self.organization)
        AnnotationFactory(task=TaskFactory(project=regular), completed_by=self.annotator, result=[])
        list_url = f'/api/organizations/{self.organization.pk}/memberships'
        detail_url = f'/api/organizations/{self.organization.pk}/memberships/{self.annotator.pk}/'
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        self.client.force_authenticate(self.manager)
        with self.settings(**config):
            def projection():
                detail = self.client.get(detail_url, {'contributed_to_projects': 'true'})
                listing = self.client.get(list_url, {'contributed_to_projects': 'true', 'page_size': '-1'})
                assert detail.status_code == 200, detail.data
                assert listing.status_code == 200, listing.data
                assert detail['Cache-Control'] == listing['Cache-Control'] == 'private, no-store'
                row = next(item for item in listing.data['results'] if item['user']['id'] == self.annotator.pk)
                return (detail.data['annotations_count'], detail.data['contributed_projects_count'],
                        detail.data['contributed_to_projects'], row['contributed_to_projects'],
                        row['user']['contributed_to_projects'])

            before = projection()
            assert before[0:2] == (1, 1)
            assert [item['id'] for item in before[2]] == [regular.pk]
            AnnotationFactory(task=self.task, completed_by=self.annotator, result=[])
            assert projection() == before
            quality = TaskFactory(project=self.project)
            assignment = TaskAssignment.objects.create(task=quality, project=self.project,
                                                       assignee=self.annotator, assigned_by=self.manager)
            bind_marker(assignment)
            AnnotationFactory(task=quality, completed_by=self.annotator, result=[])
            assert projection() == before
        # Quality markers preserve the suppression after the rollout flag is off.
        assert projection() == before

    def test_project_summary_is_scoped_and_hides_participant_activity(self):
        from projects.models import ProjectMember, ProjectSummary

        _ = self.project.summary
        ProjectSummary.objects.filter(project=self.project).update(
            created_annotations={'private': 3}, created_labels={'label': {'answer': 2}},
        )
        url = f'/api/projects/{self.project.pk}/summary/'
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        with self.settings(**config):
            self.client.force_authenticate(self.annotator)
            response = self.client.get(url)
            assert response.status_code == 200
            assert response.data['created_annotations'] == {} and response.data['created_labels'] == {}
            assert response['Cache-Control'] == 'private, no-store'
            self.client.force_authenticate(self.manager)
            assert self.client.get(url).data['created_annotations'] == {'private': 3}
            self.client.force_authenticate(self.other_user)
            assert self.client.get(url).status_code == 404
            ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=False)
            self.client.force_authenticate(self.annotator)
            assert self.client.get(url).status_code == 404

    def test_summary_rebuild_clears_stale_counts_with_only_quality_tasks(self):
        from projects.functions.utils import recalculate_created_annotations_and_labels_from_scratch
        from projects.models import ProjectSummary

        quality_project = ProjectFactory(organization=self.organization)
        task = TaskFactory(project=quality_project)
        assignment = TaskAssignment.objects.create(task=task, project=quality_project,
                                                   assignee=self.annotator, assigned_by=self.manager)
        bind_marker(assignment)
        ProjectSummary.objects.filter(project=quality_project).update(
            all_data_columns={'text': 1}, common_data_columns=['text'], created_annotations={'stale': 1},
            created_labels={'stale': {'label': 1}}, created_labels_drafts={'stale': {'label': 1}},
        )
        summary = ProjectSummary.objects.get(project=quality_project)
        recalculate_created_annotations_and_labels_from_scratch(quality_project, summary, self.organization.pk)
        summary.refresh_from_db()
        assert summary.all_data_columns == {}
        assert summary.common_data_columns == []
        assert summary.created_annotations == {}
        assert summary.created_labels == {}
        assert summary.created_labels_drafts == {}

    def test_prediction_access_is_revoked_with_quality_eligibility(self):
        from classification_disagreements.blind_provenance import record_project_file_access
        from classification_disagreements.tests.test_gold_scoring import answer
        from tasks.tests.factories import PredictionFactory

        self.qualified_submission(answer('Positive'))
        prediction = PredictionFactory(task=self.task, result=[])
        self.client.force_authenticate(self.annotator)
        assert self.client.get(f'/api/predictions/{prediction.pk}/').status_code == 404
        assert not prediction.has_permission(self.annotator)
        record_project_file_access(account_id=self.annotator.pk, project_id=self.project.pk)
        assert self.client.get(f'/api/predictions/{prediction.pk}/').status_code == 404
        assert not prediction.has_permission(self.annotator)

    def test_ml_backend_changes_invalidate_capture_and_revoke_quality(self):
        from classification_disagreements.blind_provenance import project_file_scope_supported
        from classification_disagreements.blind_scoring import score_quality_revision
        from classification_disagreements.models import BlindCaptureEpoch
        from classification_disagreements.tests.test_gold_scoring import answer
        from ml.models import MLBackend

        _, submission = self.qualified_submission(answer('Positive'))
        assert Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        with patch('ml.models.WebhookSerializer') as webhook:
            webhook.return_value.is_valid.return_value = False
            backend = MLBackend.objects.create(project=self.project, url='http://127.0.0.1:1')
        assert not project_file_scope_supported(self.project.pk)
        assert not BlindCaptureEpoch.objects.get(pk=1).enabled
        assert not Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        with self.assertRaises(ValidationError):
            score_quality_revision(submission.pk)
        # Re-enabling while a backend exists still denies quality access.
        provenance_fixtures.enable_test_capture()
        assert not Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        backend.title = 'changed'
        backend.save(update_fields=['title'])
        assert not BlindCaptureEpoch.objects.get(pk=1).enabled
        provenance_fixtures.enable_test_capture()
        backend.delete()
        assert not BlindCaptureEpoch.objects.get(pk=1).enabled
        assert project_file_scope_supported(self.project.pk)

    def test_project_credentials_are_manager_only_even_outside_blind_mode(self):
        Project.objects.filter(pk=self.project.pk).update(task_data_login='synthetic-user',
                                                          task_data_password='synthetic-password')
        urls = (f'/api/projects/{self.project.pk}/', f'/api/dm/project/?project={self.project.pk}')
        self.client.force_authenticate(self.annotator)
        for url in urls:
            response = self.client.get(url)
            assert response.status_code == 200, response.json()
            assert 'task_data_login' not in response.json() and 'task_data_password' not in response.json()
        self.client.force_authenticate(self.manager)
        for url in urls:
            response = self.client.get(url)
            assert response.status_code == 200, response.json()
            assert response.json()['task_data_login'] == 'synthetic-user'
            assert response.json()['task_data_password'] == 'synthetic-password'

    def test_external_project_credentials_invalidate_capture_and_quality_access(self):
        from classification_disagreements.blind_provenance import project_file_scope_supported
        from classification_disagreements.models import BlindCaptureEpoch
        from classification_disagreements.tests.test_gold_scoring import answer

        self.qualified_submission(answer('Positive'))
        assert Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        # A different project's credentials can reach shared external data.
        other = ProjectFactory(organization=self.organization, task_data_login='synthetic-user')
        assert not BlindCaptureEpoch.objects.get(pk=1).enabled
        assert not project_file_scope_supported(self.project.pk)
        assert not Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        provenance_fixtures.enable_test_capture()
        assert not Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        Project.all_objects.filter(pk=other.pk).update(task_data_login='')
        assert not BlindCaptureEpoch.objects.get(pk=1).enabled
        assert project_file_scope_supported(self.project.pk)
        provenance_fixtures.enable_test_capture()
        Project.all_objects.filter(pk=other.pk).update(task_data_login='')
        assert BlindCaptureEpoch.objects.get(pk=1).enabled  # Identical writes do not invalidate capture.
        Project.all_objects.filter(pk=other.pk).update(task_data_password='synthetic-password')
        assert not BlindCaptureEpoch.objects.get(pk=1).enabled
        provenance_fixtures.enable_test_capture()
        other.delete()
        assert not BlindCaptureEpoch.objects.get(pk=1).enabled

    def _submit_new_annotation(self, result=None):
        # Some isolation tests deliberately construct a bare marker to exercise
        # review, export and deletion independently of admission.  Real proof
        # paths and explicit rejection tests do not use this fixture bypass.
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_participant_projection import sensitive_project
        from classification_disagreements.blind_task_handles import issue_handle
        from classification_disagreements.models import BlindEligibilityProof

        def submit():
            if not sensitive_project(self.project):
                return fixtures.TestSubmissionReviewWorkflow._submit_new_annotation(self, result)
            handle = issue_handle(principal=local_principal(self.annotator), assignment_id=self.assignment.pk)
            self.client.force_authenticate(self.annotator)
            response = self.client.post(
                f'/api/tasks/blind/{handle.task_handle}/annotations/',
                {'result': result or [], 'assignment_id': handle.assignment_handle,
                 'assignment_version': self.assignment.version, 'submit_for_review': True}, format='json',
            )
            assert response.status_code == 201, response.json()
            self.assignment.refresh_from_db()
            return (Annotation.objects.get(pk=self.assignment.annotation_id),
                    Submission.objects.get(assignment=self.assignment, revision=1))

        bare_marker = BlindQualityAssignment.objects.filter(assignment_id=self.assignment.pk).exists()
        has_proof = BlindEligibilityProof.objects.filter(assignment_id=self.assignment.pk).exists()
        if bare_marker and not has_proof:
            fixture_ids = BlindQualityAssignment.objects.filter(assignment_id=self.assignment.pk).values('assignment_id')
            with patch('classification_disagreements.blind_provenance.checked_quality_proof'), patch(
                'classification_disagreements.blind_provenance.current_quality_assignment_ids', return_value=fixture_ids,
            ):
                return submit()
        return submit()

    def test_ordinary_export_remains_available_but_excludes_quality_task(self):
        assert not has_immutable_release_boundary(self.project.pk)
        bind_marker(self.assignment)
        quality_annotation, _ = self._submit_new_annotation()
        normal_task = TaskFactory(project=self.project, data={'text': 'ordinary-synthetic'})
        normal_annotation = Annotation.objects.create(
            task=normal_task, project=self.project, completed_by=self.annotator, result=[],
        )
        assert not has_immutable_release_boundary(self.project.pk)
        export = Export.objects.create(project=self.project, created_by=self.manager)
        exported = list(export.get_export_data())
        assert [item['id'] for item in exported] == [normal_task.pk]
        with TemporaryDirectory() as path:
            storage = LocalFilesExportStorage.objects.create(project=self.project, path=path)
            with self.assertRaises(ValidationError):
                storage.save_annotation(quality_annotation)
            storage.save_annotation(normal_annotation)
        self.client.force_authenticate(self.manager)
        response = self.client.get(f'/api/projects/{self.project.pk}/export', {'exportType': 'JSON'})
        assert response.status_code == 200
        payload = json.loads(b''.join(response.streaming_content))
        assert [item['id'] for item in payload] == [normal_task.pk]

    def test_moved_quality_task_remains_excluded_from_other_project_export(self):
        bind_marker(self.assignment)
        other = ProjectFactory()
        Task.objects.filter(pk=self.task.pk).update(project=other)
        export = Export.objects.create(project=other, created_by=other.created_by)
        assert list(export.get_export_data()) == []

    def test_quality_annotation_cannot_be_moved_or_created_by_another_actor(self):
        bind_marker(self.assignment)
        annotation, _ = self._submit_new_annotation()
        other_project = ProjectFactory()
        other_task = TaskFactory(project=other_project)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Annotation.objects.filter(pk=annotation.pk).update(task=other_task)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Annotation.objects.filter(pk=annotation.pk).update(project=other_project)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Annotation.objects.create(task=self.task, project=self.project, completed_by=self.other_user, result=[])
        normal_task = TaskFactory(project=self.project)
        ordinary = Annotation.objects.create(task=normal_task, project=self.project,
                                             completed_by=self.other_user, result=[])
        with self.assertRaises(DatabaseError), transaction.atomic():
            Annotation.objects.filter(pk=ordinary.pk).update(task=self.task)

    def test_quality_submission_cannot_enter_pending_review_queue_or_be_reviewed(self):
        bind_marker(self.assignment)
        _, submission = self._submit_new_annotation()
        assert not valid_source(submission, self.project.pk)
        from task_packages.access import review_work

        assert not review_work(self.project, self.reviewer, 'reviewer').filter(pk=submission.pk).exists()
        assert not submission.has_permission(self.reviewer)
        self.client.force_authenticate(self.reviewer)
        queue = self.client.get('/api/submissions/', {'project': self.project.pk, 'reviewable': 'true'})
        assert queue.status_code == 200
        assert submission.pk not in [item['id'] for item in queue.data['results']]
        assert self.client.get(f'/api/submissions/{submission.pk}/').status_code == 404
        work = self.client.get(f'/api/projects/{self.project.pk}/my-work/', {'kind': 'review'})
        assert work.status_code == 200
        assert submission.pk not in [item['submission_id'] for item in work.data['results']]
        response = self._review(submission)
        assert response.status_code in (403, 404)
        with self.assertRaises(PermissionDenied):
            review_submission(submission=submission, reviewer=self.reviewer, decision='approved')
        assert not ReviewDecision.objects.filter(submission=submission).exists()

    def test_cancelled_quality_submission_disappears_from_assignee_history(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import issue_handle
        from classification_disagreements.tests.test_gold_scoring import answer

        _, submission = self.qualified_submission(answer('Positive'))
        assert submission.has_permission(self.annotator)
        handle = issue_handle(principal=local_principal(self.annotator), assignment_id=self.assignment.pk)
        assert self.client.get(f'/api/tasks/blind/{handle.task_handle}/').status_code == 200
        assert self.client.get(f'/api/submissions/{submission.pk}/').status_code == 404
        self.assignment.cancel()
        # A previously loaded assignment must not keep the old access alive.
        assert not submission.has_permission(self.annotator)
        assert self.client.get(f'/api/tasks/blind/{handle.task_handle}/').status_code == 404
        assert self.client.get(f'/api/submissions/{submission.pk}/').status_code == 404
        response = self.client.get('/api/submissions/', {'project': self.project.pk, 'history': 'true'})
        assert response.status_code == 200
        assert submission.pk not in [row['id'] for row in response.data['results']]
        self.client.force_authenticate(self.manager)
        assert self.client.get(f'/api/submissions/{submission.pk}/').status_code == 200

    def test_cancelled_ordinary_submission_remains_in_assignee_history(self):
        _, submission = self._submit_new_annotation()
        self.assignment.cancel()
        submission.refresh_from_db()
        assert submission.has_permission(self.annotator)
        assert self.client.get(f'/api/submissions/{submission.pk}/').status_code == 200

    def test_quality_write_requires_current_admission_proof(self):
        from access_control.authorization import authorization
        from access_control.project_operations import local_principal
        from classification_disagreements.models import BlindCaptureEpoch
        from classification_disagreements.tests.test_gold_scoring import answer
        from tasks.models import AnnotationDraft
        from tasks.my_work import annotation_work

        _, submission = self.qualified_submission(answer('Positive'))
        annotation_id = self.assignment.annotation_id
        annotation = Annotation.objects.get(pk=annotation_id)
        draft = AnnotationDraft.objects.create(task=self.task, assignment=self.assignment,
                                               user=self.annotator, result=[])
        principal = local_principal(self.annotator)
        assert authorization.can_view_task(principal, self.task)
        assert annotation.has_permission(self.annotator) and draft.has_permission(self.annotator)
        assert Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        assert annotation_work(self.project, self.annotator).filter(pk=self.assignment.pk).exists()
        # Random fixture text can happen to have the same encoded length as
        # the reference, which intentionally revokes blind eligibility.
        ordinary = TaskFactory(project=self.project, data={'text': 'ordinary'})
        ordinary_assignment = TaskAssignment.objects.create(
            task=ordinary, project=self.project, assignee=self.annotator, assigned_by=self.manager,
        )
        BlindCaptureEpoch.objects.filter(pk=1).update(enabled=False)
        assert not authorization.can_view_task(principal, self.task)
        assert not annotation.has_permission(self.annotator) and not draft.has_permission(self.annotator)
        assert not Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        assert not annotation_work(self.project, self.annotator).filter(pk=self.assignment.pk).exists()
        assert Task.objects.for_user(self.annotator).filter(pk=ordinary.pk).exists()
        assert annotation_work(self.project, self.annotator).filter(pk=ordinary_assignment.pk).exists()
        response = self.client.patch(
            f'/api/annotations/{annotation_id}/',
            {'result': answer('Negative'), 'assignment_id': self.assignment.pk,
             'assignment_version': self.assignment.version, 'submit_for_review': True},
            format='json',
        )
        assert response.status_code in (403, 404, 409)
        assert Submission.objects.filter(assignment=self.assignment).count() == 1
        assert Submission.objects.get(pk=submission.pk).result_hash == submission.result_hash

    def test_unqualified_quality_write_is_rejected(self):
        bind_marker(self.assignment)
        self.client.force_authenticate(self.annotator)
        response = self.client.post(
            f'/api/tasks/{self.task.pk}/annotations/',
            {'result': [], 'assignment_id': self.assignment.pk,
             'assignment_version': self.assignment.version, 'submit_for_review': True},
            format='json',
        )
        assert response.status_code in (403, 404, 409)
        assert not Submission.objects.filter(assignment=self.assignment).exists()

    def test_deleted_certificate_revokes_object_level_quality_access(self):
        from access_control.authorization import authorization
        from access_control.project_operations import local_principal
        from classification_disagreements.models import BlindCaptureCertificate
        from classification_disagreements.tests.test_gold_scoring import answer
        from tasks.my_work import annotation_work

        _, submission = self.qualified_submission(answer('Positive'))
        principal = local_principal(self.annotator)
        assert authorization.can_view_task(principal, self.task)
        BlindCaptureCertificate.objects.filter(account_id=self.annotator.pk).delete()
        assert not authorization.can_view_task(principal, self.task)
        assert not submission.has_permission(self.annotator)
        assert self.client.get(f'/api/submissions/{submission.pk}/').status_code == 404
        assert not Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        assert not annotation_work(self.project, self.annotator).filter(pk=self.assignment.pk).exists()
        assert not authorization.can_update_annotation(
            principal, Annotation.objects.get(pk=self.assignment.annotation_id),
        )

    def test_missing_provenance_event_hides_quality_from_task_and_work_lists(self):
        from classification_disagreements.models import BlindProvenanceEvent
        from classification_disagreements.tests.test_gold_scoring import answer
        from tasks.my_work import annotation_work

        self.qualified_submission(answer('Positive'))
        assert Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        BlindProvenanceEvent.objects.filter(kind='account', subject_id=self.annotator.pk).delete()
        assert not Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        assert not annotation_work(self.project, self.annotator).filter(pk=self.assignment.pk).exists()

    def test_deleted_reference_revokes_quality_list_and_object_access(self):
        from classification_disagreements.tests.test_gold_scoring import answer
        from tasks.my_work import annotation_work

        reference, submission = self.qualified_submission(answer('Positive'))
        assert Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        reference.delete()
        assert not Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        assert not annotation_work(self.project, self.annotator).filter(pk=self.assignment.pk).exists()
        assert not submission.has_permission(self.annotator)

    def test_epoch_revocation_hides_quality_history_and_package(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import issue_handle
        from classification_disagreements.models import BlindCaptureEpoch
        from classification_disagreements.tests.test_gold_scoring import answer
        from task_packages.access import visible_packages
        from task_packages.models import TaskPackage, TaskPackageItem

        _, submission = self.qualified_submission(answer('Positive'))
        package = TaskPackage.objects.create(
            project=self.project, name='synthetic', creator_principal='local:test', idempotency_key=uuid4(),
            request_hash='a' * 64, membership_hash='b' * 64,
        )
        TaskPackageItem.objects.create(
            package=package, task=self.task, original_task_id=self.task.pk, original_created_at=self.task.created_at,
        )
        assert submission.has_permission(self.annotator)
        handle = issue_handle(principal=local_principal(self.annotator), assignment_id=self.assignment.pk)
        assert self.client.get(f'/api/tasks/blind/{handle.task_handle}/').status_code == 200
        assert self.client.get(f'/api/submissions/{submission.pk}/').status_code == 404
        assert visible_packages(self.project, self.annotator, 'annotator').filter(pk=package.pk).exists()
        BlindCaptureEpoch.objects.filter(pk=1).update(enabled=False)
        assert not submission.has_permission(self.annotator)
        assert self.client.get(f'/api/tasks/blind/{handle.task_handle}/').status_code == 404
        assert self.client.get(f'/api/submissions/{submission.pk}/').status_code == 404
        response = self.client.get('/api/submissions/', {'project': self.project.pk, 'history': 'true'})
        assert response.status_code == 200
        assert submission.pk not in [row['id'] for row in response.data['results']]
        assert not visible_packages(self.project, self.annotator, 'annotator').filter(pk=package.pk).exists()
        self.client.force_authenticate(self.manager)
        assert self.client.get(f'/api/submissions/{submission.pk}/').status_code == 200

    def test_revoked_quality_is_not_counted_in_mixed_package_progress(self):
        from classification_disagreements.models import BlindCaptureEpoch
        from classification_disagreements.tests.test_gold_scoring import answer
        from task_packages.models import TaskPackage, TaskPackageItem
        from task_packages.progress import PackageWork

        self.qualified_submission(answer('Positive'))
        # A random same-length string would intentionally revoke the sample.
        ordinary = TaskFactory(project=self.project, data={'text': 'ordinary'})
        TaskAssignment.objects.create(task=ordinary, project=self.project, assignee=self.annotator,
                                      assigned_by=self.manager)
        package = TaskPackage.objects.create(
            project=self.project, name='mixed', creator_principal='local:test', idempotency_key=uuid4(),
            request_hash='a' * 64, membership_hash='b' * 64,
        )
        for task in (self.task, ordinary):
            TaskPackageItem.objects.create(package=package, task=task, original_task_id=task.pk,
                                           original_created_at=task.created_at)
        work = PackageWork(self.project, self.annotator, 'annotator', [package.pk])
        assert work.summaries([package.pk])[package.pk]['assignments'] == 2
        BlindCaptureEpoch.objects.filter(pk=1).update(enabled=False)
        assert work.summaries([package.pk])[package.pk]['assignments'] == 1

    def test_same_length_ordinary_assignment_revokes_quality_in_package_progress(self):
        from classification_disagreements.tests.test_gold_scoring import answer
        from task_packages.models import TaskPackage, TaskPackageItem
        from task_packages.progress import PackageWork

        reference, submission = self.qualified_submission(answer('Positive'))
        reference_text = reference.manifest['contents']['task_data']['text']
        different_text = 'x' * len(reference_text.encode('utf-8'))
        assert different_text != reference_text
        ordinary = TaskFactory(project=self.project, data={'text': different_text})
        TaskAssignment.objects.create(task=ordinary, project=self.project, assignee=self.annotator,
                                      assigned_by=self.manager)
        package = TaskPackage.objects.create(
            project=self.project, name='same length', creator_principal='local:test', idempotency_key=uuid4(),
            request_hash='a' * 64, membership_hash='b' * 64,
        )
        for task in (self.task, ordinary):
            TaskPackageItem.objects.create(package=package, task=task, original_task_id=task.pk,
                                           original_created_at=task.created_at)
        work = PackageWork(self.project, self.annotator, 'annotator', [package.pk])
        assert work.summaries([package.pk])[package.pk]['assignments'] == 1
        assert not submission.has_permission(self.annotator)

    def test_legacy_first_assignment_proof_also_revokes_after_same_length_exposure(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import issue_handle
        from classification_disagreements.models import BlindEligibilityProof
        from classification_disagreements.tests.test_gold_scoring import answer
        from rest_framework.exceptions import NotFound
        from review_batches.service import digest

        create_proof = BlindEligibilityProof.objects.create

        def issue_legacy(**kwargs):
            manifest = dict(kwargs['manifest'])
            manifest['rule'] = 'local-first-assignment/v1'
            manifest.pop('source_task_id')
            return create_proof(**{**kwargs, 'manifest': manifest, 'manifest_hash': digest(manifest)})

        with patch.object(BlindEligibilityProof.objects, 'create', side_effect=issue_legacy):
            reference, submission = self.qualified_submission(answer('Positive'))
        assert BlindEligibilityProof.objects.get(assignment_id=self.assignment.pk).manifest['rule'] == 'local-first-assignment/v1'
        principal = local_principal(self.annotator)
        assert submission.has_permission(self.annotator)
        issue_handle(principal=principal, assignment_id=self.assignment.pk)
        text = reference.manifest['contents']['task_data']['text']
        ordinary = TaskFactory(project=self.project, data={'text': 'x' * len(text.encode('utf-8'))})
        TaskAssignment.objects.create(task=ordinary, project=self.project, assignee=self.annotator,
                                      assigned_by=self.manager)
        assert not submission.has_permission(self.annotator)
        with self.assertRaises(NotFound):
            issue_handle(principal=principal, assignment_id=self.assignment.pk)

    def test_reused_account_id_cannot_resolve_previous_blind_handle(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import issue_handle, resolve_handle
        from classification_disagreements.tests.test_gold_scoring import answer
        from projects.models import ProjectMember
        from rest_framework.exceptions import NotFound
        from users.models import User

        self.qualified_submission(answer('Positive'))
        old_id = self.annotator.pk
        handle = issue_handle(principal=local_principal(self.annotator), assignment_id=self.assignment.pk)
        User.objects.filter(pk=old_id).delete()
        replacement = UserFactory(id=old_id, active_organization=self.organization)
        self.organization.add_user(replacement)
        ProjectMember.objects.create(project=self.project, user=replacement, role='annotator')
        with self.assertRaises(NotFound):
            resolve_handle(principal=local_principal(replacement), task_handle=handle.task_handle)

    def test_batched_quality_snapshot_needs_no_per_submission_query(self):
        bind_marker(self.assignment)
        _, submission = self._submit_new_annotation()
        fetched = with_quality_snapshot(Submission.objects.filter(pk=submission.pk)).get()
        with self.assertNumQueries(0):
            assert is_quality_only_submission(fetched)

    def test_stale_reverse_relation_cache_does_not_hide_marker(self):
        bind_marker(self.assignment)
        _, submission = self._submit_new_annotation()
        self.assignment._state.fields_cache['blind_quality_marker'] = None
        submission.assignment = self.assignment
        assert is_quality_only_submission(submission)

    def test_forged_approval_cannot_use_direct_release(self):
        bind_marker(self.assignment)
        _, submission = self._submit_new_annotation()
        with self.assertRaises(DatabaseError), transaction.atomic():
            Submission.objects.filter(pk=submission.pk).update(status=Submission.Status.APPROVED)
        with self.assertRaises(DatabaseError), transaction.atomic():
            ReviewDecision.objects.create(submission=submission, reviewer=self.reviewer, decision='approved')
        submission.status = Submission.Status.APPROVED  # even an inconsistent in-memory row is denied
        assert not submission.is_releasable
        with self.assertRaises(APIException):
            source_record(submission, 'synthetic')
        self.client.force_authenticate(self.manager)
        response = self.client.get(f'/api/submissions/{submission.pk}/release/')
        assert response.status_code == 400

    def test_quality_annotation_create_and_update_do_not_emit_ordinary_webhooks(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_resource_handles import issue_resource_handle
        from classification_disagreements.blind_task_handles import issue_handle

        with patch('webhooks.utils.emit_webhooks_for_instance') as emit:
            self.qualified_submission([])
            annotation = Annotation.objects.get(pk=self.assignment.annotation_id)
            principal = local_principal(self.annotator)
            handle = issue_handle(principal=principal, assignment_id=self.assignment.pk)
            resource = issue_resource_handle(principal=principal, assignment_id=self.assignment.pk,
                                             kind='annotation', object_id=annotation.pk)
            emit.assert_not_called()
            response = self.client.patch(
                f'/api/tasks/blind/{handle.task_handle}/annotations/{resource.handle}/',
                {'result': [], 'assignment_id': handle.assignment_handle,
                 'assignment_version': self.assignment.version, 'submit_for_review': False},
                format='json',
            )
            assert response.status_code == 200, response.data
            emit.assert_not_called()
            response = self.client.delete(f'/api/annotations/{annotation.pk}/')
            assert response.status_code == 404, response.data
            emit.assert_not_called()

    def test_ordinary_annotation_still_emits_webhook(self):
        with patch('webhooks.utils.emit_webhooks_for_instance') as emit:
            self._submit_new_annotation()
            emit.assert_called_once()

    def test_quality_purpose_is_immutable_and_cannot_be_added_after_submission(self):
        marker = bind_marker(self.assignment)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindQualityAssignment.objects.filter(pk=marker.pk).delete()
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindQualityAssignment.objects.filter(pk=marker.pk).update(assignment_id=self.assignment.pk)

    def test_existing_submission_cannot_be_reclassified_as_quality_only(self):
        self._submit_new_annotation()
        with self.assertRaises(DatabaseError), transaction.atomic():
            bind_marker(self.assignment)

    def test_certified_assignment_exposure_survives_assignment_deletion(self):
        from classification_disagreements.models import BlindAssignmentExposure, BlindCaptureEpoch

        provenance_fixtures.enable_test_capture()
        member = UserFactory(active_organization=self.organization)
        task = TaskFactory(project=self.project)
        assignment = TaskAssignment.objects.create(task=task, project=self.project, assignee=member)
        generation = BlindCaptureEpoch.objects.get(pk=1).generation
        exposure = BlindAssignmentExposure.objects.get(account_id=member.pk, task_id=task.pk,
                                                        generation=generation)
        assignment.delete()
        assert BlindAssignmentExposure.objects.filter(pk=exposure.pk).exists()
        with self.assertRaises(DatabaseError), transaction.atomic():
            exposure.delete()

    def test_existing_draft_cannot_be_reclassified_as_quality_only(self):
        from tasks.models import AnnotationDraft

        AnnotationDraft.objects.create(task=self.task, assignment=self.assignment, user=self.annotator, result=[])
        with self.assertRaises(DatabaseError), transaction.atomic():
            bind_marker(self.assignment)

    def test_existing_peer_annotation_or_mismatched_project_denies_binding(self):
        annotation = Annotation.objects.create(task=self.task, project=self.project,
                                               completed_by=self.other_user, result=[])
        with self.assertRaises(DatabaseError), transaction.atomic():
            bind_marker(self.assignment)
        annotation.delete()
        other_project = ProjectFactory()
        TaskAssignment.objects.filter(pk=self.assignment.pk).update(project=other_project)
        with self.assertRaises(DatabaseError), transaction.atomic():
            bind_marker(self.assignment)

    def test_reassignment_retains_quality_purpose_and_rejects_shared_work(self):
        other = UserFactory(active_organization=self.organization)
        self.organization.add_user(other)
        from projects.models import ProjectMember

        ProjectMember.objects.create(project=self.project, user=other, role='annotator')
        extra = TaskAssignment.objects.create(task=self.task, project=self.project, assignee=other)
        with self.assertRaises(DatabaseError), transaction.atomic():
            bind_marker(self.assignment)
        extra.delete()
        reference = synthetic_reference(self.project)
        bind_marker(self.assignment, reference)
        self.assignment.cancel()
        replacement = TaskAssignment.objects.create(task=self.task, project=self.project, assignee=other)
        assert BlindQualityAssignment.objects.filter(
            assignment_id=replacement.pk, task_id=self.task.pk, reference_id=reference.pk,
            reference_hash=reference.manifest_hash,
        ).exists()
        with self.assertRaises(DatabaseError), transaction.atomic():
            TaskAssignment.objects.filter(pk=self.assignment.pk).update(assignee=other)
        self.client.force_authenticate(self.manager)
        response = self.client.post('/api/task-assignments/',
                                    {'task': self.task.pk, 'assignee': other.pk}, format='json')
        assert response.status_code == 400

    def test_reference_binding_rejects_wrong_project_or_hash(self):
        foreign = synthetic_reference(ProjectFactory())
        with self.assertRaises(DatabaseError), transaction.atomic():
            bind_marker(self.assignment, foreign)
        reference = synthetic_reference(self.project)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindQualityAssignment.objects.create(
                assignment_id=self.assignment.pk, task_id=self.task.pk, project_id=self.project.pk,
                assignee_id=self.annotator.pk, reference_id=reference.pk, reference_hash='0' * 64,
            )

    def valid_reference(self):
        from classification_disagreements.gold_api import SCHEMA
        from classification_disagreements.tests.test_gold_scoring import CONFIG, answer
        from review_batches.service import digest

        data = {'text': 'synthetic classification text'}
        Task.objects.filter(pk=self.task.pk).update(data=data)
        Project.objects.filter(pk=self.project.pk).update(label_config=CONFIG)
        self.task.refresh_from_db()
        self.project.refresh_from_db()
        principal = 'local:test'
        manifest = {
            'schema': SCHEMA, 'project_id': self.project.pk, 'kind': 'reference',
            'actor_id': self.manager.pk, 'principal': principal, 'principal_source': 'local',
            'contents': {'source': {'task_id': self.task.pk + 1000000, 'submitter_id': self.manager.pk},
                         'config': CONFIG, 'task_data': data, 'answer': answer('Positive')},
        }
        reference = ClassificationGoldEvidence.objects.create(
            project=self.project, created_by=self.manager, actor_id_frozen=self.manager.pk,
            principal=principal, principal_source='local', kind='reference', idempotency_key=uuid4(),
            request_hash='0' * 64, manifest=manifest, manifest_hash=digest(manifest),
        )
        return reference

    def qualified_submission(self, result, *, prior_ordinary=False):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_reassignment import reassign_quality_task
        from projects.models import ProjectMember

        reference = self.valid_reference()
        bind_marker(self.assignment, reference)
        self.assignment.cancel()
        provenance_fixtures.enable_test_capture()
        member = UserFactory(active_organization=self.organization)
        self.organization.add_user(member)
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        if prior_ordinary:
            ordinary = TaskFactory(project=self.project, data={'text': 'ordinary'})
            TaskAssignment.objects.create(task=ordinary, project=self.project, assignee=member,
                                          assigned_by=self.manager)
        settings = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        with self.settings(**settings), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider',
            return_value=LocalIdentityProvider(),
        ):
            self.assignment = reassign_quality_task(
                task=self.task, assignee=member, principal=local_principal(self.manager),
            )
        self.annotator = member
        _, submission = self._submit_new_annotation(result)
        return reference, submission

    def test_checked_reference_reuses_scorer_and_rejects_changed_task_or_config(self):
        data = {'text': 'synthetic classification text'}
        reference = self.valid_reference()
        marker = bind_marker(self.assignment, reference)
        assert checked_blind_reference(marker=marker, project=self.project, task=self.task).pk == reference.pk
        Task.objects.filter(pk=self.task.pk).update(data={'text': 'changed'})
        self.task.refresh_from_db()
        with self.assertRaises(ValidationError):
            checked_blind_reference(marker=marker, project=self.project, task=self.task)
        Task.objects.filter(pk=self.task.pk).update(data=data)
        self.task.refresh_from_db()
        Project.objects.filter(pk=self.project.pk).update(label_config='<View/>')
        self.project.refresh_from_db()
        with self.assertRaises(ValidationError):
            checked_blind_reference(marker=marker, project=self.project, task=self.task)

    def test_internal_reassignment_is_default_off_and_requires_new_unexposed_account(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_reassignment import reassign_quality_task
        from projects.models import ProjectMember

        reference = self.valid_reference()
        bind_marker(self.assignment, reference)
        self.assignment.cancel()
        provenance_fixtures.enable_test_capture()
        member = UserFactory(active_organization=self.organization)
        self.organization.add_user(member)
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        principal = local_principal(self.manager)
        with self.assertRaises(ValidationError):
            reassign_quality_task(task=self.task, assignee=member, principal=principal)

        settings = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        with self.settings(**settings), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider',
            return_value=LocalIdentityProvider(),
        ):
            with self.assertRaises(PermissionDenied):
                reassign_quality_task(task=self.task, assignee=member, principal=local_principal(self.annotator))
            self.organization.add_user(self.other_user)
            ProjectMember.objects.create(project=self.project, user=self.other_user, role='annotator')
            with self.assertRaises(ValidationError):
                reassign_quality_task(task=self.task, assignee=self.other_user, principal=principal)
            prior = UserFactory(active_organization=self.organization)
            self.organization.add_user(prior)
            ProjectMember.objects.create(project=self.project, user=prior, role='annotator')
            prior_task = TaskFactory(project=self.project, id=reference.manifest['contents']['source']['task_id'])
            TaskAssignment.objects.create(task=prior_task, project=self.project, assignee=prior).delete()
            with self.assertRaises(ValidationError):
                reassign_quality_task(task=self.task, assignee=prior, principal=principal)
            with patch('classification_disagreements.blind_reassignment.get_identity_provider', return_value=object()):
                with self.assertRaises(ValidationError):
                    reassign_quality_task(task=self.task, assignee=member, principal=principal)
            assignment = reassign_quality_task(task=self.task, assignee=member, principal=principal)
        assert assignment.assignee_id == member.pk
        assert BlindQualityAssignment.objects.filter(
            assignment_id=assignment.pk, reference_id=reference.pk, reference_hash=reference.manifest_hash,
        ).exists()
        from classification_disagreements.models import BlindEligibilityProof

        proof = BlindEligibilityProof.objects.get(assignment_id=assignment.pk)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindEligibilityProof.objects.filter(pk=proof.pk).update(manifest_hash='0' * 64)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindEligibilityProof.objects.filter(pk=proof.pk).delete()

    def test_certified_member_with_unrelated_prior_assignment_can_participate(self):
        from classification_disagreements.blind_scoring import score_quality_revision
        from classification_disagreements.tests.test_gold_scoring import answer

        _, submission = self.qualified_submission(answer('Positive'), prior_ordinary=True)
        assert Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        assert score_quality_revision(submission.pk).manifest['result']['status'] == 'scored'

    def test_initial_quality_assignment_is_atomic_and_idempotent(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_reassignment import create_quality_task
        from classification_disagreements.blind_scoring import score_quality_revision
        from classification_disagreements.models import BlindDispatchReceipt
        from classification_disagreements.tests.test_gold_scoring import answer
        from projects.models import ProjectMember

        reference = self.valid_reference()
        provenance_fixtures.enable_test_capture()
        member = UserFactory(active_organization=self.organization)
        self.organization.add_user(member)
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        settings = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        key = uuid4()
        args = {'project_id': self.project.pk, 'reference_id': reference.pk,
                'expected_reference_hash': reference.manifest_hash, 'assignee': member,
                'principal': local_principal(self.manager), 'key': key}
        with self.settings(**settings), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ):
            assignment = create_quality_task(**args)
            assert create_quality_task(**args).pk == assignment.pk
            with self.assertRaises(ValidationError):
                create_quality_task(**{**args, 'expected_reference_hash': '0' * 64})
            count = Task.objects.filter(project=self.project).count()
            with self.assertRaises(ValidationError):
                create_quality_task(**{**args, 'key': uuid4(), 'assignee': self.other_user})
            with self.assertRaises(PermissionDenied):
                create_quality_task(**{**args, 'key': uuid4(), 'principal': local_principal(self.annotator)})
            assert Task.objects.filter(project=self.project).count() == count
        assert BlindDispatchReceipt.objects.filter(project=self.project, key=key).count() == 1
        assert BlindQualityAssignment.objects.filter(assignment_id=assignment.pk, reference_id=reference.pk).exists()
        self.assignment, self.task, self.annotator = assignment, assignment.task, member
        _, submission = self._submit_new_annotation(answer('Positive'))
        assert score_quality_revision(submission.pk).manifest['result']['status'] == 'scored'
        project_id, assignment_id = self.project.pk, assignment.pk
        self.task.delete()
        receipt = BlindDispatchReceipt.objects.get(project_id=project_id, key=key)
        assert receipt.assignment_id is None and receipt.assignment_id_frozen == assignment_id
        with self.settings(**settings), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ):
            with self.assertRaises(ValidationError):
                create_quality_task(**args)
        self.project.delete()
        assert not BlindDispatchReceipt.objects.filter(project_id=project_id).exists()
        assert BlindQualityAssignment.objects.filter(assignment_id=assignment_id).exists()

    def test_internal_mix_persists_random_choice_and_cannot_resample(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_mix import _choose_assignment as choose_assignment
        from classification_disagreements.blind_scoring import score_quality_revision
        from classification_disagreements.models import BlindMixDecision
        from classification_disagreements.tests.test_gold_scoring import answer
        from projects.models import ProjectMember

        reference = self.valid_reference()
        provenance_fixtures.enable_test_capture()
        member = UserFactory(active_organization=self.organization)
        self.organization.add_user(member)
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        source = TaskAssignment.objects.create(task=TaskFactory(project=self.project, data={'text': 'ordinary'}), project=self.project,
                                               assignee=member, assigned_by=self.manager)
        config = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        args = {'source': source, 'expected_version': source.version,
                'references': [{'id': reference.pk, 'hash': reference.manifest_hash}],
                'ratio_basis_points': 10000, 'principal': local_principal(self.manager), 'key': uuid4()}
        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ), patch('classification_disagreements.blind_mix.randbelow', side_effect=[0, 0]) as random:
            chosen = choose_assignment(**args)
            assert chosen.pk != source.pk
            assert choose_assignment(**args).pk == chosen.pk
            assert random.call_count == 2
            with self.assertRaises(ValidationError):
                choose_assignment(**{**args, 'key': uuid4()})
            with self.assertRaises(ValidationError):
                choose_assignment(**{**args, 'ratio_basis_points': 0})
            with self.assertRaises(PermissionDenied):
                choose_assignment(**{**args, 'key': uuid4(), 'principal': local_principal(self.annotator)})
        decision = BlindMixDecision.objects.get(project=self.project, key=args['key'])
        assert decision.selected and decision.reference_id_frozen == reference.pk
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindMixDecision.objects.filter(pk=decision.pk).update(ratio_basis_points=1)
        assert BlindQualityAssignment.objects.filter(assignment_id=chosen.pk, reference_id=reference.pk).exists()
        assert source.status == TaskAssignment.Status.ASSIGNED
        self.assignment, self.task, self.annotator = chosen, chosen.task, member
        _, submission = self._submit_new_annotation(answer('Positive'))
        assert not valid_source(submission, self.project.pk)
        assert score_quality_revision(submission.pk).manifest['result']['status'] == 'scored'
        chosen.task.delete()
        decision.refresh_from_db()
        assert decision.chosen_assignment_id is None and decision.chosen_id_frozen == chosen.pk
        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ):
            with self.assertRaises(ValidationError):
                choose_assignment(**args)

    def test_internal_mix_returns_ordinary_for_uncertified_account_and_zero_ratio(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_mix import _choose_assignment as choose_assignment
        from classification_disagreements.models import BlindMixDecision
        from projects.models import ProjectMember

        reference = self.valid_reference()
        provenance_fixtures.enable_test_capture()
        self.organization.add_user(self.other_user)
        ProjectMember.objects.create(project=self.project, user=self.other_user, role='annotator')
        source = TaskAssignment.objects.create(task=TaskFactory(project=self.project), project=self.project,
                                               assignee=self.other_user, assigned_by=self.manager)
        config = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        args = {'source': source, 'expected_version': source.version,
                'references': [{'id': reference.pk, 'hash': reference.manifest_hash}],
                'ratio_basis_points': 10000, 'principal': local_principal(self.manager), 'key': uuid4()}
        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ), patch('classification_disagreements.blind_mix.randbelow') as random:
            assert choose_assignment(**args).pk == source.pk
            random.assert_not_called()
            zero_source = TaskAssignment.objects.create(task=TaskFactory(project=self.project), project=self.project,
                                                        assignee=self.other_user, assigned_by=self.manager)
            assert choose_assignment(**{**args, 'source': zero_source, 'references': [],
                                         'ratio_basis_points': 0, 'key': uuid4()}).pk == zero_source.pk
        assert BlindMixDecision.objects.filter(project=self.project, selected=False).count() == 2

    def test_internal_mix_uses_basis_point_threshold(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_mix import _choose_assignment as choose_assignment
        from projects.models import ProjectMember

        reference = self.valid_reference()
        provenance_fixtures.enable_test_capture()
        members = [UserFactory(active_organization=self.organization) for _ in range(2)]
        for member in members:
            self.organization.add_user(member)
            ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        sources = [TaskAssignment.objects.create(task=TaskFactory(project=self.project, data={'text': 'ordinary'}), project=self.project,
                                                assignee=member, assigned_by=self.manager) for member in members]
        config = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        base = {'expected_version': 1, 'references': [{'id': reference.pk, 'hash': reference.manifest_hash}],
                'ratio_basis_points': 5000, 'principal': local_principal(self.manager)}
        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ), patch('classification_disagreements.blind_mix.randbelow', side_effect=[4999, 0, 5000]):
            assert choose_assignment(**base, source=sources[0], key=uuid4()).pk != sources[0].pk
            assert choose_assignment(**base, source=sources[1], key=uuid4()).pk == sources[1].pk

    def test_atomic_mix_defers_ordinary_work_until_quality_submission(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_mix import create_mixed_assignment, reassign_mixed_assignment
        from classification_disagreements.blind_reassignment import reassign_quality_task
        from classification_disagreements.blind_task_handles import issue_handle, lock_handle_for_write, resolve_handle
        from classification_disagreements.blind_scoring import score_quality_revision
        from classification_disagreements.models import BlindMixCompletion, BlindMixDecision
        from classification_disagreements.tests.test_gold_scoring import answer
        from projects.models import ProjectMember
        from projects.models import ProjectSummary
        from projects.functions.utils import recalculate_created_annotations_and_labels_from_scratch
        from tasks.assignments import create_assignment
        from tasks.exceptions import AssignmentConflictError
        from tasks.models import AnnotationDraft
        from tasks.my_work import annotation_work
        from rest_framework.exceptions import NotFound

        reference = self.valid_reference()
        provenance_fixtures.enable_test_capture()
        member = UserFactory(active_organization=self.organization)
        self.organization.add_user(member)
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        ordinary = TaskFactory(project=self.project, data={'text': 'ordinary'})
        summary_fields = ('all_data_columns', 'common_data_columns', 'created_annotations',
                          'created_labels', 'created_labels_drafts')
        def summary():
            return ProjectSummary.objects.filter(project=self.project).values(*summary_fields).get()

        before_summary = summary()
        config = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        args = {'task': ordinary, 'assignee': member, 'references': [{'id': reference.pk, 'hash': reference.manifest_hash}],
                'ratio_basis_points': 10000, 'principal': local_principal(self.manager), 'key': uuid4()}
        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ), patch('classification_disagreements.blind_mix.randbelow', side_effect=[0, 0]):
            chosen = create_mixed_assignment(**args)
            assert create_mixed_assignment(**args).pk == chosen.pk
        decision = BlindMixDecision.objects.get(project=self.project, key=args['key'])
        assert decision.selected
        self.client.force_authenticate(self.manager)
        with self.settings(ANNOTATION_FIXED_PACKAGES_ENABLED=True):
            for task_id in (ordinary.pk, chosen.task_id):
                response = self.client.post(f'/api/projects/{self.project.pk}/task-packages/',
                    {'name': 'fixed', 'task_ids': [task_id]}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
                assert response.status_code == 400
        assert summary() == before_summary
        draft = AnnotationDraft.objects.create(task=chosen.task, assignment=chosen, user=member,
                                               result=answer('Positive'))
        assert summary() == before_summary
        draft.delete()
        assert summary() == before_summary
        assert not Task.objects.for_user(member).filter(pk=ordinary.pk).exists()
        assert not annotation_work(self.project, member).filter(pk=decision.source_id_frozen).exists()
        assert Task.objects.for_user(member).filter(pk=chosen.task_id).exists()
        participant = local_principal(member)
        handle = issue_handle(principal=participant, assignment_id=chosen.pk)
        with self.assertRaises(NotFound):
            issue_handle(principal=participant, assignment_id=decision.source_id_frozen)
        self.client.force_authenticate(member)
        assert self.client.get(f'/api/tasks/{ordinary.pk}/').status_code == 404
        with self.settings(**config):
            listing = self.client.get(f'/api/projects/{self.project.pk}/blind-work/')
            assert listing.status_code == 200, listing.json()
            items = listing.json()['results']
            assert len(items) == 1 and items[0]['task_id'] == handle.task_handle
            assert self.client.get(items[0]['read_url']).status_code == 200
        self.assignment, self.task, self.annotator = chosen, chosen.task, member
        with self.settings(**config):
            submitted = self.client.post(f'/api/tasks/blind/{handle.task_handle}/annotations/', {
                'result': answer('Positive'), 'assignment_id': handle.assignment_handle,
                'assignment_version': chosen.version, 'submit_for_review': True,
            }, format='json')
            assert submitted.status_code == 201, submitted.json()
        chosen.refresh_from_db()
        submission = Submission.objects.get(assignment=chosen, revision=1)
        assert summary() == before_summary
        recalculate_created_annotations_and_labels_from_scratch(self.project, self.project.summary, self.organization.pk)
        assert summary() == before_summary
        assert not Task.objects.for_user(member).filter(pk=ordinary.pk).exists()
        assert not annotation_work(self.project, member).filter(pk=decision.source_id_frozen).exists()
        source = decision.source_assignment
        source.refresh_from_db()
        assert source.status == TaskAssignment.Status.CANCELLED
        assert BlindMixCompletion.objects.filter(decision_id_frozen=decision.pk, submission_id_frozen=submission.pk).exists()
        assert score_quality_revision(submission.pk).submission_id == submission.pk
        assert Task.objects.for_user(member).filter(pk=chosen.task_id).exists()
        assert resolve_handle(principal=participant, task_handle=handle.task_handle,
                              assignment_handle=handle.assignment_handle).pk == chosen.pk
        source_version = source.version
        previous_version = chosen.version
        from classification_disagreements.blind_resource_handles import issue_resource_handle
        resource = issue_resource_handle(principal=participant, assignment_id=chosen.pk,
                                         kind='annotation', object_id=chosen.annotation_id)
        response = self.client.patch(f'/api/tasks/blind/{handle.task_handle}/annotations/{resource.handle}/', {
            'result': answer('Negative'), 'assignment_id': handle.assignment_handle, 'assignment_version': chosen.version,
            'submit_for_review': True,
        }, format='json')
        assert response.status_code == 200, response.json()
        source.refresh_from_db()
        assert source.status == TaskAssignment.Status.CANCELLED and source.version == source_version
        assert BlindMixCompletion.objects.filter(decision_id_frozen=decision.pk).count() == 1
        second = Submission.objects.get(assignment=chosen, revision=2)
        assert score_quality_revision(second.pk).submission_id == second.pk
        assert score_quality_revision(submission.pk).submission_id == submission.pk
        with transaction.atomic(), self.assertRaises(AssignmentConflictError):
            lock_handle_for_write(principal=participant, task_handle=handle.task_handle,
                assignment_handle=handle.assignment_handle, assignment_version=previous_version)
        chosen.refresh_from_db()
        with transaction.atomic():
            assert lock_handle_for_write(principal=participant, task_handle=handle.task_handle,
                assignment_handle=handle.assignment_handle, assignment_version=chosen.version).pk == chosen.pk
        with self.assertRaises(DatabaseError), transaction.atomic():
            TaskAssignment.objects.filter(pk=source.pk).update(status=TaskAssignment.Status.ASSIGNED)
        source.delete()
        assert not Task.objects.for_user(member).filter(pk=chosen.task_id).exists()
        with self.assertRaises(NotFound):
            resolve_handle(principal=participant, task_handle=handle.task_handle)
        assert not submission.has_permission(member)
        assert not Annotation.objects.get(pk=chosen.annotation_id).has_permission(member)
        with self.assertRaises(ValidationError):
            score_quality_revision(submission.pk)
        with self.assertRaises(ValidationError):
            create_assignment(task=ordinary, assignee=member, principal=local_principal(self.manager))
        with self.assertRaises(DatabaseError), transaction.atomic():
            TaskAssignment.objects.create(task=ordinary, project=self.project, assignee=member,
                                          assigned_by=self.manager)
        chosen.cancel()
        replacement = UserFactory(active_organization=self.organization)
        self.organization.add_user(replacement)
        ProjectMember.objects.create(project=self.project, user=replacement, role='annotator')
        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ), self.assertRaises(ValidationError):
            reassign_quality_task(task=chosen.task, assignee=replacement, principal=local_principal(self.manager))
        assert not TaskAssignment.objects.filter(task=chosen.task, assignee=replacement).exists()
        replacement_task = TaskFactory(project=self.project, data={'text': 'unused replacement'})
        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ), self.assertRaises(ValidationError):
            reassign_mixed_assignment(previous_id=decision.pk, task=replacement_task, assignee=replacement,
                                      principal=local_principal(self.manager), key=uuid4())
        assert not TaskAssignment.objects.filter(task=replacement_task).exists()

    def test_participant_progress_changes_equally_for_ordinary_and_sample_submission(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_mix import create_mixed_assignment
        from classification_disagreements.blind_task_handles import issue_handle
        from classification_disagreements.tests.test_gold_scoring import answer
        from projects.models import ProjectMember
        from tasks.tests.factories import PredictionFactory

        reference = self.valid_reference()
        provenance_fixtures.enable_test_capture()
        members = [UserFactory(active_organization=self.organization) for _ in range(2)]
        for member in members:
            self.organization.add_user(member)
            ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        held = TaskFactory(project=self.project, data={'text': 'held ordinary example'})
        ordinary = TaskFactory(project=self.project, data={'text': 'other ordinary work'})
        config = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        fields = (*Project.objects.COUNTER_FIELDS, 'queue_total', 'queue_done')
        def projection(member):
            self.client.force_authenticate(member)
            response = self.client.get(f'/api/projects/{self.project.pk}/')
            assert response.status_code == 200
            summary = self.client.get(f'/api/projects/{self.project.pk}/summary/')
            assert summary.status_code == 200
            return ({field: response.data[field] for field in fields}, summary.json())

        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ), patch('classification_disagreements.blind_mix.randbelow', side_effect=[0, 0]):
            chosen = create_mixed_assignment(task=held, assignee=members[0],
                references=[{'id': reference.pk, 'hash': reference.manifest_hash}], ratio_basis_points=10000,
                principal=local_principal(self.manager), key=uuid4())
            normal = TaskAssignment.objects.create(task=ordinary, project=self.project,
                                                   assignee=members[1], assigned_by=self.manager)
            prediction = PredictionFactory(task=ordinary, result=[], model_version='offline-model')
            self.client.force_authenticate(self.manager)
            assert self.client.get(f'/api/predictions/{prediction.pk}/').status_code == 200
            before_quality, summary_before = projection(members[0])
            before_ordinary, ordinary_summary_before = projection(members[1])
            assert before_quality == before_ordinary
            assert summary_before == ordinary_summary_before
            assert before_ordinary['total_predictions_number'] == 0
            self.client.force_authenticate(members[1])
            assert self.client.get(f'/api/predictions/{prediction.pk}/').status_code == 404
            assert self.client.get(f'/api/projects/{self.project.pk}/model-versions').json() == {}
            normal_handle = issue_handle(principal=local_principal(members[1]), assignment_id=normal.pk)
            normal_response = self.client.get(f'/api/tasks/blind/{normal_handle.task_handle}/')
            listing = self.client.get('/api/tasks/', {'project': self.project.pk, 'fields': 'all'})
            assert listing.status_code == 404
            self.client.force_authenticate(members[0])
            sample_handle = issue_handle(principal=local_principal(members[0]), assignment_id=chosen.pk)
            sample_response = self.client.get(f'/api/tasks/blind/{sample_handle.task_handle}/')
            assert normal_response.status_code == sample_response.status_code == 200
            assert normal_response.data.keys() == sample_response.data.keys()
            assert normal_response.data['predictions'] == sample_response.data['predictions'] == []
            for member in members:
                self.client.force_authenticate(member)
                for response in (
                    self.client.get(f'/api/projects/{self.project.pk}/next/'),
                    self.client.post(f'/api/dm/actions/?project={self.project.pk}&id=next_task',
                                     {'selectedItems': {'all': True, 'excluded': []}}, format='json'),
                ):
                    assert response.status_code == 404, response.data
            self.assignment, self.task, self.annotator = chosen, chosen.task, members[0]
            self._submit_new_annotation(answer('Positive'))
            after_quality, summary_after = projection(members[0])
            self.assignment, self.task, self.annotator = normal, ordinary, members[1]
            self._submit_new_annotation(answer('Positive'))
            after_ordinary, ordinary_summary_after = projection(members[1])
            assert after_quality == after_ordinary
            assert after_quality['task_number'] == before_quality['task_number'] == 1
            assert after_quality['total_annotations_number'] == before_quality['total_annotations_number'] + 1
            assert summary_after == ordinary_summary_after == summary_before

    def test_mixed_reassignment_preserves_reference_and_has_one_idempotent_successor(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_mix import create_mixed_assignment, reassign_mixed_assignment
        from classification_disagreements.blind_task_handles import issue_handle
        from classification_disagreements.models import BlindMixDecision
        from projects.models import ProjectMember

        reference = self.valid_reference()
        provenance_fixtures.enable_test_capture()
        members = [UserFactory(active_organization=self.organization) for _ in range(3)]
        for member in members:
            self.organization.add_user(member)
            ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        original_task = TaskFactory(project=self.project, data={'text': 'first ordinary example'})
        replacement_task = TaskFactory(project=self.project, data={'text': 'second ordinary example'})
        config = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        principal = local_principal(self.manager)
        first_key, retry_key = uuid4(), uuid4()
        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ), patch('classification_disagreements.blind_mix.randbelow', side_effect=[0, 0, 0, 0]) as random:
            first = create_mixed_assignment(task=original_task, assignee=members[0],
                references=[{'id': reference.pk, 'hash': reference.manifest_hash}],
                ratio_basis_points=10000, principal=principal, key=first_key)
            predecessor = BlindMixDecision.objects.get(project=self.project, key=first_key)
            args = {'previous_id': predecessor.pk, 'task': replacement_task, 'assignee': members[1],
                    'principal': principal, 'key': retry_key}
            with self.assertRaises(ValidationError):
                reassign_mixed_assignment(**args)
            old_handle = issue_handle(principal=local_principal(members[0]), assignment_id=first.pk)
            old_url = f'/api/tasks/blind/{old_handle.task_handle}/'
            self.client.force_authenticate(members[0])
            assert self.client.get(old_url).status_code == 200
            first.cancel()
            replacement = reassign_mixed_assignment(**args)
            assert reassign_mixed_assignment(**args).pk == replacement.pk
            assert self.client.get(old_url).status_code == 404
            new_handle = issue_handle(principal=local_principal(members[1]), assignment_id=replacement.pk)
            self.client.force_authenticate(members[1])
            assert self.client.get(old_url).status_code == 404
            assert self.client.get(f'/api/tasks/blind/{new_handle.task_handle}/').status_code == 200
            assert random.call_count == 4
            with self.assertRaises(ValidationError):
                reassign_mixed_assignment(**{**args, 'key': uuid4()})
            with self.assertRaises(ValidationError):
                reassign_mixed_assignment(**{**args, 'assignee': members[2]})
            with self.assertRaises(PermissionDenied):
                reassign_mixed_assignment(**{**args, 'principal': local_principal(members[1])})
        successor = BlindMixDecision.objects.get(project=self.project, key=retry_key)
        assert successor.predecessor_id_frozen == predecessor.pk
        assert successor.reference_id_frozen == reference.pk
        assert successor.selected and successor.chosen_id_frozen == replacement.pk
        assert not Task.objects.for_user(members[0]).filter(pk=original_task.pk).exists()
        predecessor.source_assignment.refresh_from_db()
        assert predecessor.source_assignment.status == TaskAssignment.Status.CANCELLED
        assert not Task.objects.for_user(members[1]).filter(pk=replacement_task.pk).exists()
        assert Task.objects.for_user(members[1]).filter(pk=replacement.task_id).exists()
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindMixDecision.objects.filter(pk=successor.pk).update(predecessor_id_frozen=None)

    def test_deleted_mix_decision_cannot_unhide_or_reclassify_quality_work(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_mix import create_mixed_assignment
        from classification_disagreements.models import BlindMixDecision, BlindMixEvidence
        from projects.models import ProjectMember

        reference = self.valid_reference()
        provenance_fixtures.enable_test_capture()
        member = UserFactory(active_organization=self.organization)
        self.organization.add_user(member)
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        ordinary = TaskFactory(project=self.project, data={'text': 'ordinary source'})
        config = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        key = uuid4()
        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ), patch('classification_disagreements.blind_mix.randbelow', side_effect=[0, 0]):
            chosen = create_mixed_assignment(task=ordinary, assignee=member,
                references=[{'id': reference.pk, 'hash': reference.manifest_hash}], ratio_basis_points=10000,
                principal=local_principal(self.manager), key=key)
        decision = BlindMixDecision.objects.get(project=self.project, key=key)
        evidence_id = BlindMixEvidence.objects.get(decision_id_frozen=decision.pk).pk
        assert Task.objects.for_user(member).filter(pk=chosen.task_id).exists()
        decision.delete()
        assert not Task.objects.for_user(member).filter(pk=chosen.task_id).exists()
        assert not Task.objects.for_user(member).filter(pk=ordinary.pk).exists()
        with self.assertRaises(DatabaseError), transaction.atomic():
            TaskAssignment.objects.create(project=self.project, task=ordinary, assignee=member, assigned_by=self.manager)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindMixEvidence.objects.filter(pk=evidence_id).delete()
        self.project.delete()
        assert BlindMixEvidence.objects.filter(pk=evidence_id).exists()

    def test_manual_source_cancellation_revokes_unsubmitted_sample(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_mix import create_mixed_assignment
        from classification_disagreements.models import BlindMixCompletion, BlindMixDecision
        from projects.models import ProjectMember

        reference = self.valid_reference()
        provenance_fixtures.enable_test_capture()
        member = UserFactory(active_organization=self.organization)
        self.organization.add_user(member)
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        ordinary = TaskFactory(project=self.project, data={'text': 'ordinary source'})
        config = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        key = uuid4()
        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ), patch('classification_disagreements.blind_mix.randbelow', side_effect=[0, 0]):
            chosen = create_mixed_assignment(task=ordinary, assignee=member,
                references=[{'id': reference.pk, 'hash': reference.manifest_hash}], ratio_basis_points=10000,
                principal=local_principal(self.manager), key=key)
        decision = BlindMixDecision.objects.get(project=self.project, key=key)
        assert Task.objects.for_user(member).filter(pk=chosen.task_id).exists()
        decision.source_assignment.cancel()
        assert not Task.objects.for_user(member).filter(pk=chosen.task_id).exists()
        assert not Task.objects.for_user(member).filter(pk=ordinary.pk).exists()
        assert not BlindMixCompletion.objects.filter(decision_id_frozen=decision.pk).exists()

    def test_atomic_mix_rolls_back_ordinary_assignment_on_invalid_reference(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_mix import create_mixed_assignment
        from projects.models import ProjectMember

        reference = self.valid_reference()
        provenance_fixtures.enable_test_capture()
        member = UserFactory(active_organization=self.organization)
        self.organization.add_user(member)
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        ordinary = TaskFactory(project=self.project, data={'text': 'ordinary'})
        config = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ):
            with self.assertRaises(ValidationError):
                create_mixed_assignment(task=ordinary, assignee=member,
                    references=[{'id': reference.pk, 'hash': '0' * 64}], ratio_basis_points=10000,
                    principal=local_principal(self.manager), key=uuid4())
        assert not TaskAssignment.objects.filter(task=ordinary, assignee=member).exists()

    def test_atomic_mix_does_not_replace_previously_seen_ordinary_task(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_mix import create_mixed_assignment
        from projects.models import ProjectMember

        reference = self.valid_reference()
        provenance_fixtures.enable_test_capture()
        member = UserFactory(active_organization=self.organization)
        self.organization.add_user(member)
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        ordinary = TaskFactory(project=self.project, data={'text': 'ordinary'})
        prior = TaskAssignment.objects.create(task=ordinary, project=self.project, assignee=member)
        prior.cancel()
        config = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ):
            with self.assertRaises(ValidationError):
                create_mixed_assignment(task=ordinary, assignee=member,
                    references=[{'id': reference.pk, 'hash': reference.manifest_hash}], ratio_basis_points=10000,
                    principal=local_principal(self.manager), key=uuid4())
        assert not TaskAssignment.objects.filter(task=ordinary, assignee=member,
                                                 status__in=TaskAssignment.ACTIVE_STATUSES).exists()

    def test_atomic_mix_preserves_fixed_package_membership(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_mix import create_mixed_assignment
        from classification_disagreements.models import BlindMixDecision
        from projects.models import ProjectMember
        from task_packages.models import TaskPackage, TaskPackageItem

        reference = self.valid_reference()
        provenance_fixtures.enable_test_capture()
        member = UserFactory(active_organization=self.organization)
        self.organization.add_user(member)
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        ordinary = TaskFactory(project=self.project, data={'text': 'ordinary'})
        package = TaskPackage.objects.create(project=self.project, name='fixed', creator_principal='local:test',
                                             idempotency_key=uuid4(), request_hash='a' * 64,
                                             membership_hash='b' * 64)
        TaskPackageItem.objects.create(package=package, task=ordinary, original_task_id=ordinary.pk,
                                       original_created_at=ordinary.created_at)
        config = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        key = uuid4()
        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ), patch('classification_disagreements.blind_mix.randbelow') as random:
            chosen = create_mixed_assignment(task=ordinary, assignee=member,
                references=[{'id': reference.pk, 'hash': reference.manifest_hash}], ratio_basis_points=10000,
                principal=local_principal(self.manager), key=key)
            random.assert_not_called()
        assert chosen.task_id == ordinary.pk
        assert not BlindMixDecision.objects.get(project=self.project, key=key).selected
        assert TaskPackageItem.objects.filter(package=package).count() == 1

    def test_atomic_mix_hides_source_when_quality_eligibility_is_revoked(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_mix import create_mixed_assignment
        from classification_disagreements.blind_provenance import record_project_file_access
        from projects.models import ProjectMember

        reference = self.valid_reference()
        provenance_fixtures.enable_test_capture()
        member = UserFactory(active_organization=self.organization)
        self.organization.add_user(member)
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        ordinary = TaskFactory(project=self.project, data={'text': 'ordinary'})
        config = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        args = {'task': ordinary, 'assignee': member, 'references': [{'id': reference.pk, 'hash': reference.manifest_hash}],
                'ratio_basis_points': 10000, 'principal': local_principal(self.manager), 'key': uuid4()}
        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ), patch('classification_disagreements.blind_mix.randbelow', side_effect=[0, 0]):
            chosen = create_mixed_assignment(**args)
            assert not Task.objects.for_user(member).filter(pk=ordinary.pk).exists()
            record_project_file_access(account_id=member.pk, project_id=self.project.pk)
            assert not Task.objects.for_user(member).filter(pk=ordinary.pk).exists()
            assert not Task.objects.for_user(member).filter(pk=chosen.task_id).exists()
            with self.assertRaises(ValidationError):
                create_mixed_assignment(**args)

    def test_later_source_assignment_revokes_quality_access_and_scoring(self):
        from classification_disagreements.blind_scoring import score_quality_revision
        from classification_disagreements.tests.test_gold_scoring import answer

        reference, submission = self.qualified_submission(answer('Positive'), prior_ordinary=True)
        source = TaskFactory(project=self.project, id=reference.manifest['contents']['source']['task_id'])
        TaskAssignment.objects.create(task=source, project=self.project, assignee=self.annotator,
                                      assigned_by=self.manager)
        assert not Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        assert not submission.has_permission(self.annotator)
        with self.assertRaises(ValidationError):
            score_quality_revision(submission.pk)

    def test_same_reference_cannot_be_injected_twice_for_member(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_reassignment import create_quality_task
        from classification_disagreements.tests.test_gold_scoring import answer

        reference, _ = self.qualified_submission(answer('Positive'))
        config = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ):
            with self.assertRaises(ValidationError):
                create_quality_task(project_id=self.project.pk, reference_id=reference.pk,
                                    expected_reference_hash=reference.manifest_hash, assignee=self.annotator,
                                    principal=local_principal(self.manager), key=uuid4())

    def test_deleted_and_mutated_prior_task_cannot_hide_matching_text_exposure(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_reassignment import create_quality_task
        from classification_disagreements.models import BlindTextLengthExposure
        from projects.models import ProjectMember

        reference = self.valid_reference()
        provenance_fixtures.enable_test_capture()
        member = UserFactory(active_organization=self.organization)
        self.organization.add_user(member)
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        ordinary = TaskFactory(project=self.project, data={'text': 'a different length'})
        previous = TaskAssignment.objects.create(task=ordinary, project=self.project, assignee=member)
        previous_id = previous.pk
        Task.objects.filter(pk=ordinary.pk).update(data=reference.manifest['contents']['task_data'])
        Task.objects.filter(pk=ordinary.pk).update(data={'text': 'changed after exposure'})
        previous.delete()
        ordinary.delete()
        assert BlindTextLengthExposure.objects.filter(account_id=member.pk, assignment_id=previous_id,
            byte_length=len('synthetic classification text'.encode())).exists()
        config = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ):
            with self.assertRaises(ValidationError):
                create_quality_task(project_id=self.project.pk, reference_id=reference.pk,
                                    expected_reference_hash=reference.manifest_hash, assignee=member,
                                    principal=local_principal(self.manager), key=uuid4())

    def test_later_matching_text_revokes_quality_and_scoring(self):
        from classification_disagreements.blind_scoring import score_quality_revision
        from classification_disagreements.tests.test_gold_scoring import answer

        reference, submission = self.qualified_submission(answer('Positive'), prior_ordinary=True)
        ordinary = TaskAssignment.objects.get(assignee=self.annotator, project=self.project,
                                               status=TaskAssignment.Status.ASSIGNED)
        Task.objects.filter(pk=ordinary.task_id).update(data=reference.manifest['contents']['task_data'])
        assert not Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        assert not submission.has_permission(self.annotator)
        with self.assertRaises(ValidationError):
            score_quality_revision(submission.pk)

    def test_project_upload_read_revokes_quality_without_blocking_file(self):
        from classification_disagreements.blind_scoring import score_quality_revision
        from classification_disagreements.models import BlindCaptureEpoch, BlindProjectFileExposure
        from classification_disagreements.tests.test_gold_scoring import answer
        from data_import.models import FileUpload
        from django.core.files.uploadedfile import SimpleUploadedFile
        from urllib.parse import urlparse

        with TemporaryDirectory() as media, self.settings(MEDIA_ROOT=media, USE_NGINX_FOR_UPLOADS=False):
            _, submission = self.qualified_submission(answer('Positive'))
            upload = FileUpload.objects.create(project=self.project, user=self.manager,
                                               file=SimpleUploadedFile('ordinary.txt', b'synthetic file'))
            response = self.client.get(urlparse(upload.url).path)
            assert response.status_code == 200
            assert b''.join(response.streaming_content) == b'synthetic file'
            upload.delete()
            generation = BlindCaptureEpoch.objects.get(pk=1).generation
            exposure = BlindProjectFileExposure.objects.get(account_id=self.annotator.pk,
                                                            project_id=self.project.pk, generation=generation)
            assert not Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
            assert not submission.has_permission(self.annotator)
            with self.assertRaises(ValidationError):
                score_quality_revision(submission.pk)
            with self.assertRaises(DatabaseError), transaction.atomic():
                exposure.delete()

    def test_past_project_upload_read_blocks_new_quality_assignment(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_reassignment import create_quality_task
        from data_import.models import FileUpload
        from django.core.files.uploadedfile import SimpleUploadedFile
        from projects.models import ProjectMember
        from urllib.parse import urlparse

        with TemporaryDirectory() as media, self.settings(MEDIA_ROOT=media, USE_NGINX_FOR_UPLOADS=False):
            reference = self.valid_reference()
            provenance_fixtures.enable_test_capture()
            member = UserFactory(active_organization=self.organization)
            self.organization.add_user(member)
            ProjectMember.objects.create(project=self.project, user=member, role='annotator')
            upload = FileUpload.objects.create(project=self.project, user=self.manager,
                                               file=SimpleUploadedFile('ordinary.txt', b'synthetic file'))
            self.client.force_authenticate(member)
            response = self.client.get(urlparse(upload.url).path)
            assert response.status_code == 200
            assert b''.join(response.streaming_content) == b'synthetic file'
            settings = {
                'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
                'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
                'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            }
            with self.settings(**settings), patch(
                'classification_disagreements.blind_reassignment.get_identity_provider',
                return_value=LocalIdentityProvider(),
            ):
                with self.assertRaises(ValidationError):
                    create_quality_task(project_id=self.project.pk, reference_id=reference.pk,
                                        expected_reference_hash=reference.manifest_hash, assignee=member,
                                        principal=local_principal(self.manager), key=uuid4())

    def test_storage_download_route_records_file_exposure(self):
        from classification_disagreements.models import BlindCaptureEpoch, BlindProjectFileExposure
        from data_import.models import FileUpload
        from django.core.files.uploadedfile import SimpleUploadedFile
        from projects.models import ProjectMember

        with TemporaryDirectory() as media, self.settings(MEDIA_ROOT=media, USE_NGINX_FOR_UPLOADS=False):
            provenance_fixtures.enable_test_capture()
            member = UserFactory(active_organization=self.organization)
            self.organization.add_user(member)
            ProjectMember.objects.create(project=self.project, user=member, role='annotator')
            upload = FileUpload.objects.create(project=self.project, user=self.manager,
                                               file=SimpleUploadedFile('ordinary.txt', b'synthetic file'))
            self.client.force_authenticate(member)
            response = self.client.get('/storage-data/uploaded/', {'filepath': upload.file.name})
            assert response.status_code == 200
            assert b''.join(response.streaming_content) == b'synthetic file'
            assert BlindProjectFileExposure.objects.filter(account_id=member.pk, project_id=self.project.pk,
                generation=BlindCaptureEpoch.objects.get(pk=1).generation).exists()

    def test_upload_metadata_read_also_revokes_quality(self):
        from classification_disagreements.tests.test_gold_scoring import answer
        from data_import.models import FileUpload
        from django.core.files.uploadedfile import SimpleUploadedFile

        with TemporaryDirectory() as media, self.settings(MEDIA_ROOT=media):
            _, submission = self.qualified_submission(answer('Positive'))
            upload = FileUpload.objects.create(project=self.project, user=self.manager,
                                               file=SimpleUploadedFile('synthetic-positive.txt', b'synthetic'))
            response = self.client.get(f'/api/import/file-upload/{upload.pk}')
            assert response.status_code == 200
            assert not submission.has_permission(self.annotator)
            assert not Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()

    def test_global_label_metadata_access_revokes_live_quality_proof(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import issue_handle
        from classification_disagreements.models import BlindCaptureEpoch, BlindLabelExposure
        from classification_disagreements.tests.test_gold_scoring import answer

        _, submission = self.qualified_submission(answer('Positive'))
        handle = issue_handle(principal=local_principal(self.annotator), assignment_id=self.assignment.pk)
        assert self.client.get(f'/api/tasks/blind/{handle.task_handle}/').status_code == 200
        response = self.client.get('/api/label_links/')
        assert response.status_code == 200
        exposure = BlindLabelExposure.objects.get(account_id=self.annotator.pk,
                                                  generation=BlindCaptureEpoch.objects.get(pk=1).generation)
        assert self.client.get(f'/api/tasks/blind/{handle.task_handle}/').status_code == 404
        assert not submission.has_permission(self.annotator)
        assert not Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindLabelExposure.objects.filter(pk=exposure.pk).delete()
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindLabelExposure.objects.filter(pk=exposure.pk).update(generation=exposure.generation + 1)

    def test_request_wide_transactions_disable_quality_capture(self):
        from classification_disagreements.blind_provenance import project_file_scope_supported
        from classification_disagreements.tests.test_gold_scoring import answer

        _, submission = self.qualified_submission(answer('Positive'))
        assert Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        with patch.dict(connection.settings_dict, {'ATOMIC_REQUESTS': True}):
            assert not project_file_scope_supported(self.project.pk)
            assert not Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
            assert not submission.has_permission(self.annotator)

    def test_label_read_before_project_membership_prevents_quality_admission(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_reassignment import reassign_quality_task
        from classification_disagreements.models import BlindLabelExposure
        from projects.models import ProjectMember

        reference = self.valid_reference()
        bind_marker(self.assignment, reference)
        self.assignment.cancel()
        provenance_fixtures.enable_test_capture()
        member = UserFactory(active_organization=self.organization)
        self.organization.add_user(member)
        self.client.force_authenticate(member)
        assert self.client.get('/api/labels/').status_code == 200
        assert BlindLabelExposure.objects.filter(account_id=member.pk).exists()
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
                  'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ), self.assertRaises(ValidationError):
            reassign_quality_task(task=self.task, assignee=member, principal=local_principal(self.manager))

    def test_label_bulk_update_does_not_mutate_quality_only_annotations(self):
        from classification_disagreements.tests.test_gold_scoring import answer
        from labels_manager.functions import bulk_update_label

        bind_marker(self.assignment)
        quality, _ = self._submit_new_annotation(answer('Positive'))
        ordinary_task = TaskFactory(project=self.project)
        ordinary = Annotation.objects.create(project=self.project, task=ordinary_task, completed_by=self.annotator,
                                             result=answer('Positive'))
        assert bulk_update_label(['Positive'], ['Negative'], self.organization, self.project) == 1
        quality.refresh_from_db()
        ordinary.refresh_from_db()
        assert quality.result == answer('Positive')
        assert ordinary.result == answer('Negative')

    def test_label_bulk_update_requires_manager_and_never_partially_scopes_organization(self):
        from classification_disagreements.tests.test_gold_scoring import answer
        from projects.models import ProjectMember

        task = TaskFactory(project=self.project)
        annotation = Annotation.objects.create(project=self.project, task=task, completed_by=self.annotator,
                                               result=answer('Positive'))
        body = {'project': self.project.pk, 'old_label': ['Positive'], 'new_label': ['Negative']}
        self.client.force_authenticate(self.annotator)
        assert self.client.post('/api/labels/bulk', body, format='json').status_code == 403
        annotation.refresh_from_db()
        assert annotation.result == answer('Positive')
        self.client.force_authenticate(self.manager)
        response = self.client.post('/api/labels/bulk', body, format='json')
        assert response.status_code == 200, response.data
        assert response.data['annotations_updated'] == 1
        other = ProjectFactory(organization=self.organization, created_by=self.other_user)
        ProjectMember.objects.create(project=other, user=self.other_user, role='manager')
        response = self.client.post('/api/labels/bulk', {'old_label': ['Negative'], 'new_label': ['Positive']},
                                    format='json')
        assert response.status_code == 403
        annotation.refresh_from_db()
        assert annotation.result == answer('Negative')

    def test_upload_metadata_list_read_also_revokes_quality(self):
        from classification_disagreements.tests.test_gold_scoring import answer
        from data_import.models import FileUpload
        from django.core.files.uploadedfile import SimpleUploadedFile

        with TemporaryDirectory() as media, self.settings(MEDIA_ROOT=media):
            _, submission = self.qualified_submission(answer('Positive'))
            FileUpload.objects.create(project=self.project, user=self.annotator,
                                      file=SimpleUploadedFile('synthetic-positive.txt', b'synthetic'))
            response = self.client.get(f'/api/projects/{self.project.pk}/file-uploads', {'all': 'true'})
            assert response.status_code == 200
            assert response.data
            assert not submission.has_permission(self.annotator)

    def test_file_read_in_another_project_revokes_quality_conservatively(self):
        from classification_disagreements.tests.test_gold_scoring import answer
        from data_import.models import FileUpload
        from django.core.files.uploadedfile import SimpleUploadedFile
        from projects.models import ProjectMember
        from urllib.parse import urlparse

        with TemporaryDirectory() as media, self.settings(MEDIA_ROOT=media, USE_NGINX_FOR_UPLOADS=False):
            _, submission = self.qualified_submission(answer('Positive'))
            other = ProjectFactory(organization=self.organization)
            ProjectMember.objects.create(project=other, user=self.annotator, role='annotator')
            upload = FileUpload.objects.create(project=other, user=self.manager,
                                               file=SimpleUploadedFile('ordinary.txt', b'synthetic file'))
            response = self.client.get(urlparse(upload.url).path)
            assert response.status_code == 200
            assert b''.join(response.streaming_content) == b'synthetic file'
            assert not Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
            assert not submission.has_permission(self.annotator)

    def test_local_storage_file_read_revokes_quality(self):
        from classification_disagreements.tests.test_gold_scoring import answer
        from io_storages.localfiles.models import LocalFilesImportStorage
        from pathlib import Path

        with TemporaryDirectory() as root, self.settings(LOCAL_FILES_SERVING_ENABLED=True,
                                                         LOCAL_FILES_DOCUMENT_ROOT=root):
            path = Path(root) / 'synthetic.txt'
            path.write_text('synthetic data', encoding='utf-8')
            LocalFilesImportStorage.objects.create(project=self.project, path=root, use_blob_urls=True)
            _, submission = self.qualified_submission(answer('Positive'))
            response = self.client.get('/data/local-files/', {'d': 'synthetic.txt'})
            assert response.status_code == 200
            assert b''.join(response.streaming_content) == b'synthetic data'
            assert not Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
            assert not submission.has_permission(self.annotator)

    def test_storage_change_invalidates_capture_even_after_storage_removed(self):
        from classification_disagreements.models import BlindCaptureEpoch
        from classification_disagreements.tests.test_gold_scoring import answer
        from io_storages.tests.factories import S3ImportStorageFactory

        _, submission = self.qualified_submission(answer('Positive'))
        assert Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        storage = S3ImportStorageFactory(project=self.project)
        assert not BlindCaptureEpoch.objects.get(pk=1).enabled
        assert not Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        assert not submission.has_permission(self.annotator)
        storage.delete()
        provenance_fixtures.enable_test_capture()
        assert not Task.objects.for_user(self.annotator).filter(pk=self.task.pk).exists()
        assert not submission.has_permission(self.annotator)

    def test_internal_reassignment_rejects_revoked_and_old_capture_generations(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_reassignment import reassign_quality_task
        from classification_disagreements.models import BlindCaptureEpoch
        from projects.models import ProjectMember

        reference = self.valid_reference()
        bind_marker(self.assignment, reference)
        self.assignment.cancel()
        provenance_fixtures.enable_test_capture()
        member = UserFactory(active_organization=self.organization)
        self.organization.add_user(member)
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        settings = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        with self.settings(**settings), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider',
            return_value=LocalIdentityProvider(),
        ):
            BlindCaptureEpoch.objects.filter(pk=1).update(enabled=False)
            with self.assertRaises(ValidationError):
                reassign_quality_task(task=self.task, assignee=member, principal=local_principal(self.manager))
            provenance_fixtures.enable_test_capture()
            with self.assertRaises(ValidationError):
                reassign_quality_task(task=self.task, assignee=member, principal=local_principal(self.manager))
        assert not TaskAssignment.objects.filter(task=self.task, assignee=member).exists()

    def test_unbound_quality_marker_cannot_supply_a_reference(self):
        marker = bind_marker(self.assignment)
        with self.assertRaises(ValidationError):
            checked_blind_reference(marker=marker, project=self.project, task=self.task)

    def test_internal_score_is_idempotent_and_uses_frozen_revision(self):
        from classification_disagreements.blind_scoring import score_quality_revision
        from classification_disagreements.tests.test_gold_scoring import answer

        reference, submission = self.qualified_submission(answer('Negative'))
        Task.objects.filter(pk=self.task.pk).update(data={'text': 'changed after submission'})
        Project.objects.filter(pk=self.project.pk).update(label_config='<View/>')
        score = score_quality_revision(submission.pk)
        assert score.manifest['result']['matched_controls'] == 0
        assert score.manifest['result']['total_controls'] == 1
        assert score.manifest['submission_hash'] == submission.result_hash
        assert score.manifest['reference_hash'] == reference.manifest_hash
        assert score_quality_revision(submission.pk).pk == score.pk
        assert BlindGoldScore.objects.filter(submission=submission).count() == 1
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindGoldScore.objects.filter(pk=score.pk).update(manifest_hash='0' * 64)

    def test_internal_score_rejects_unbound_or_invalid_revision(self):
        from classification_disagreements.blind_scoring import score_quality_revision

        _, submission = self.qualified_submission([])
        with self.assertRaises(ValidationError):
            score_quality_revision(submission.pk)
        assert not BlindGoldScore.objects.filter(submission=submission).exists()

    def test_uniform_outbox_scores_only_quality_and_is_idempotent(self):
        from classification_disagreements.blind_score_outbox import dispatch, process
        from classification_disagreements.models import BlindScoreOutbox
        from classification_disagreements.tests.test_gold_scoring import answer

        _, quality = self.qualified_submission(answer('Positive'))
        normal_task = TaskFactory(project=self.project, data={'text': 'ordinary synthetic'})
        self.task = normal_task
        self.assignment = TaskAssignment.objects.create(
            task=normal_task, project=self.project, assignee=self.annotator, assigned_by=self.manager,
        )
        _, ordinary = self._submit_new_annotation(answer('Negative'))
        quality_job = BlindScoreOutbox.objects.get(submission=quality)
        ordinary_job = BlindScoreOutbox.objects.get(submission=ordinary)
        assert quality_job.state == ordinary_job.state == 'pending'
        with patch('data_export.approved.api_queue') as queue:
            dispatch(quality_job.pk)
            dispatch(ordinary_job.pk)
            assert queue.return_value.enqueue.call_count == 2
        quality_job.refresh_from_db()
        ordinary_job.refresh_from_db()
        assert quality_job.state == ordinary_job.state == 'queued'
        process(quality_job.pk, str(quality_job.token))
        process(ordinary_job.pk, str(ordinary_job.token))
        process(quality_job.pk, str(quality_job.token))
        quality_job.refresh_from_db()
        ordinary_job.refresh_from_db()
        assert quality_job.state == 'scored' and ordinary_job.state == 'ignored'
        assert BlindGoldScore.objects.filter(submission=quality).count() == 1
        assert not BlindGoldScore.objects.filter(submission=ordinary).exists()

    def test_queue_failure_is_pending_and_stale_delivery_is_fenced(self):
        from classification_disagreements.blind_score_outbox import dispatch, process, reconcile
        from classification_disagreements.models import BlindScoreOutbox
        from classification_disagreements.tests.test_gold_scoring import answer

        _, submission = self.qualified_submission(answer('Positive'))
        row = BlindScoreOutbox.objects.get(submission=submission)
        with patch('data_export.approved.api_queue', side_effect=RuntimeError('synthetic unavailable')):
            dispatch(row.pk)
        row.refresh_from_db()
        assert row.state == 'pending'
        old_token = str(row.token)
        row.state, row.queued_at = 'queued', timezone.now() - timedelta(minutes=20)
        row.save(update_fields=['state', 'queued_at'])
        with patch('data_export.approved.api_queue'), self.captureOnCommitCallbacks(execute=True):
            assert reconcile(limit=10, stale_seconds=300) >= 1
        row.refresh_from_db()
        assert row.state == 'queued' and str(row.token) != old_token
        process(row.pk, old_token)
        row.refresh_from_db()
        assert row.state == 'queued'
        process(row.pk, str(row.token))
        row.refresh_from_db()
        assert row.state == 'scored'
        assert BlindGoldScore.objects.filter(submission=submission).count() == 1

    @skipUnless(os.environ.get('LS82_TEST_REDIS_URL'), 'Dedicated synthetic Redis is not configured')
    def test_real_redis_worker_scores_quality_job(self):
        from classification_disagreements.blind_score_outbox import reconcile
        from classification_disagreements.models import BlindScoreOutbox
        from classification_disagreements.tests.test_gold_scoring import answer
        from redis import Redis
        from rq import Queue, SimpleWorker

        connection = Redis.from_url(os.environ['LS82_TEST_REDIS_URL'])
        queue = Queue(f'ls82-test-{uuid4().hex}', connection=connection)
        unavailable = Redis(host='127.0.0.1', port=1, socket_connect_timeout=0.2, socket_timeout=0.2)
        try:
            with patch('data_export.approved.api_queue', return_value=Queue(queue.name, connection=unavailable)):
                with self.captureOnCommitCallbacks(execute=True):
                    _, submission = self.qualified_submission(answer('Positive'))
                row = BlindScoreOutbox.objects.get(submission=submission)
                assert row.state == 'pending'
            with patch('data_export.approved.api_queue', return_value=queue):
                with self.captureOnCommitCallbacks(execute=True):
                    assert reconcile(limit=10) >= 1
                row.refresh_from_db()
                assert row.state == 'queued'
                assert queue.count == 1
                assert SimpleWorker([queue], connection=connection).work(burst=True)
                row.refresh_from_db()
                assert row.state == 'scored'
                assert BlindGoldScore.objects.filter(submission=submission).count() == 1
        finally:
            queue.empty()
            connection.delete(queue.key)
            connection.close()
            unavailable.close()

    def test_rolled_back_submission_never_dispatches_outbox(self):
        from classification_disagreements.blind_score_outbox import record_submission
        from classification_disagreements.models import BlindScoreOutbox

        _, submission = self._submit_new_annotation()
        projects = {f'{self.organization.pk}:{self.project.pk}': True}
        with self.settings(ANNOTATION_CLASSIFICATION_BLIND_ENABLED=True,
                           ANNOTATION_CLASSIFICATION_BLIND_PROJECTS=projects):
            with patch('data_export.approved.api_queue') as queue, self.captureOnCommitCallbacks(execute=True):
                with self.assertRaisesRegex(ValueError, 'rollback'):
                    with transaction.atomic():
                        record_submission(submission, self.project)
                        raise ValueError('rollback')
            queue.assert_not_called()
        assert not BlindScoreOutbox.objects.filter(submission=submission).exists()

    def test_queue_outage_does_not_reject_ordinary_submission(self):
        from classification_disagreements.models import BlindScoreOutbox

        projects = {f'{self.organization.pk}:{self.project.pk}': True}
        with self.settings(ANNOTATION_CLASSIFICATION_BLIND_ENABLED=True,
                           ANNOTATION_CLASSIFICATION_BLIND_PROJECTS=projects):
            with patch('data_export.approved.api_queue', side_effect=RuntimeError('synthetic unavailable')):
                with self.captureOnCommitCallbacks(execute=True):
                    _, submission = self._submit_new_annotation()
        assert Submission.objects.filter(pk=submission.pk).exists()
        assert BlindScoreOutbox.objects.get(submission=submission).state == 'pending'

    def test_unqualified_quality_submission_never_becomes_a_score(self):
        from classification_disagreements.blind_score_outbox import process
        from classification_disagreements.models import BlindScoreOutbox
        from classification_disagreements.tests.test_gold_scoring import answer

        bind_marker(self.assignment, self.valid_reference())
        _, submission = self._submit_new_annotation(answer('Positive'))
        row = BlindScoreOutbox.objects.get(submission=submission)
        row.state, row.queued_at = 'queued', timezone.now()
        row.save(update_fields=['state', 'queued_at'])
        process(row.pk, str(row.token))
        row.refresh_from_db()
        assert row.state == 'invalid'
        assert not BlindGoldScore.objects.filter(submission=submission).exists()

    def test_scoring_requires_qualification_and_current_capture_generation(self):
        from classification_disagreements.blind_scoring import score_quality_revision
        from classification_disagreements.models import BlindCaptureEpoch
        from classification_disagreements.tests.test_gold_scoring import answer

        reference = self.valid_reference()
        bind_marker(self.assignment, reference)
        _, unqualified = self._submit_new_annotation(answer('Positive'))
        with self.assertRaises(ValidationError):
            score_quality_revision(unqualified.pk)

        # A separate task demonstrates that a formerly valid proof is rejected
        # if capture has been disabled or restarted after that assignment.
        self.task = TaskFactory(project=self.project)
        self.assignment = TaskAssignment.objects.create(
            task=self.task, project=self.project, assignee=self.annotator, assigned_by=self.manager,
        )
        _, qualified = self.qualified_submission(answer('Positive'))
        BlindCaptureEpoch.objects.filter(pk=1).update(enabled=False)
        with self.assertRaises(ValidationError):
            score_quality_revision(qualified.pk)
        provenance_fixtures.enable_test_capture()
        with self.assertRaises(ValidationError):
            score_quality_revision(qualified.pk)

    def test_score_fails_closed_if_certification_evidence_disappears(self):
        from classification_disagreements.blind_scoring import score_quality_revision
        from classification_disagreements.models import BlindCaptureCertificate
        from classification_disagreements.tests.test_gold_scoring import answer

        _, submission = self.qualified_submission(answer('Positive'))
        BlindCaptureCertificate.objects.filter(account_id=self.annotator.pk).delete()
        with self.assertRaises(ValidationError):
            score_quality_revision(submission.pk)

    def test_deleting_project_keeps_minimal_quality_tombstone(self):
        marker = bind_marker(self.assignment)
        project_id, task_id, assignment_id = self.project.pk, self.task.pk, self.assignment.pk
        assignee_id = self.assignment.assignee_id
        self._submit_new_annotation()
        self.project.delete()
        marker.refresh_from_db()
        assert (marker.project_id, marker.task_id, marker.assignment_id, marker.assignee_id) == (
            project_id, task_id, assignment_id, assignee_id)
        assert not Project.all_objects.filter(pk=project_id).exists()
        assert not TaskAssignment.objects.filter(pk=assignment_id).exists()

    def test_deleting_project_removes_score_payload_but_keeps_tombstone(self):
        from classification_disagreements.blind_scoring import score_quality_revision
        from classification_disagreements.tests.test_gold_scoring import answer

        _, submission = self.qualified_submission(answer('Positive'))
        marker = BlindQualityAssignment.objects.get(assignment_id=self.assignment.pk)
        score = score_quality_revision(submission.pk)
        score_id, marker_id = score.pk, marker.pk
        self.project.delete()
        assert not BlindGoldScore.objects.filter(pk=score_id).exists()
        assert BlindQualityAssignment.objects.filter(pk=marker_id).exists()

    def test_deleted_assignment_cannot_erase_previous_assignee_history(self):
        bind_marker(self.assignment)
        self.assignment.cancel()
        self.assignment.delete()
        with self.assertRaises(DatabaseError), transaction.atomic():
            TaskAssignment.objects.create(task=self.task, project=self.project, assignee=self.annotator)

    def test_privilege_elevation_is_blocked_while_quality_assignment_is_active(self):
        from classification_disagreements.models import BlindPrivilegeHistory
        from projects.models import ProjectMember
        from users.models import User

        bind_marker(self.assignment)
        with self.assertRaises(DatabaseError), transaction.atomic():
            ProjectMember.objects.filter(project=self.project, user=self.annotator).update(role='manager')
        with self.assertRaises(DatabaseError), transaction.atomic():
            User.objects.filter(pk=self.annotator.pk).update(is_staff=True)
        self.assignment.cancel()
        ProjectMember.objects.filter(project=self.project, user=self.annotator).update(role='manager')
        assert BlindPrivilegeHistory.objects.get(account_id=self.annotator.pk).elevated

    def test_quality_marker_rejects_already_privileged_account(self):
        privileged = UserFactory(is_staff=True)
        task = TaskFactory(project=self.project)
        assignment = TaskAssignment.objects.create(task=task, project=self.project, assignee=privileged)
        with self.assertRaises(DatabaseError), transaction.atomic():
            bind_marker(assignment)


@skipUnless(connection.vendor == 'postgresql' and os.environ.get('LS82_TEST_REDIS_URL'),
            'Dedicated PostgreSQL and synthetic Redis are required')
class TestBlindScoreWorkerProcess(provenance_fixtures.SyntheticTransactionTestCase):
    valid_reference = TestBlindQualityBoundary.valid_reference
    qualified_submission = TestBlindQualityBoundary.qualified_submission
    _submit_new_annotation = TestBlindQualityBoundary._submit_new_annotation

    def setUp(self):
        from rest_framework.test import APIClient

        fixtures.TestSubmissionReviewWorkflow.setUpTestData.__func__(type(self))
        self.client = APIClient()
        fixtures.TestSubmissionReviewWorkflow.setUp(self)

    def test_committed_submission_is_scored_by_separate_worker_process(self):
        from classification_disagreements.models import BlindScoreOutbox
        from classification_disagreements.tests.test_gold_scoring import answer
        from redis import Redis
        from rq import Queue

        redis = Redis.from_url(os.environ['LS82_TEST_REDIS_URL'])
        queue = Queue(f'ls82-process-{uuid4().hex}', connection=redis)
        try:
            with patch('data_export.approved.api_queue', return_value=queue):
                _, submission = self.qualified_submission(answer('Positive'))
            row = BlindScoreOutbox.objects.get(submission=submission)
            assert row.state == 'queued' and queue.count == 1
            env = os.environ.copy()
            env['POSTGRE_NAME'] = connection.settings_dict['NAME']
            env['LS82_TEST_QUEUE'] = queue.name
            script = (
                "import os, django; django.setup(); "
                "from redis import Redis; from rq import Queue, SimpleWorker; "
                "r=Redis.from_url(os.environ['LS82_TEST_REDIS_URL']); "
                "q=Queue(os.environ['LS82_TEST_QUEUE'], connection=r); "
                "SimpleWorker([q], connection=r).work(burst=True)"
            )
            result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stderr[-4000:]
            row.refresh_from_db()
            assert row.state == 'scored'
            assert BlindGoldScore.objects.filter(submission=submission).count() == 1
        finally:
            queue.empty()
            redis.delete(queue.key)
            redis.close()

    def test_mixed_submission_scores_after_source_release_in_separate_worker(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_mix import create_mixed_assignment
        from classification_disagreements.models import BlindMixDecision, BlindScoreOutbox
        from classification_disagreements.tests.test_gold_scoring import answer
        from projects.models import ProjectMember
        from redis import Redis
        from rq import Queue

        reference = self.valid_reference()
        provenance_fixtures.enable_test_capture()
        member = UserFactory(active_organization=self.organization)
        self.organization.add_user(member)
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        ordinary = TaskFactory(project=self.project, data={'text': 'ordinary worker sample'})
        config = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        redis = Redis.from_url(os.environ['LS82_TEST_REDIS_URL'])
        queue = Queue(f'ls82-mixed-process-{uuid4().hex}', connection=redis)
        key = uuid4()
        try:
            with self.settings(**config), patch(
                'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
            ), patch('data_export.approved.api_queue', return_value=queue):
                chosen = create_mixed_assignment(task=ordinary, assignee=member,
                    references=[{'id': reference.pk, 'hash': reference.manifest_hash}], ratio_basis_points=10000,
                    principal=local_principal(self.manager), key=key)
                self.assignment, self.task, self.annotator = chosen, chosen.task, member
                _, submission = self._submit_new_annotation(answer('Positive'))
            decision = BlindMixDecision.objects.get(project=self.project, key=key)
            assert decision.source_assignment.status == TaskAssignment.Status.CANCELLED
            row = BlindScoreOutbox.objects.get(submission=submission)
            assert row.state == 'queued' and queue.count == 1
            env = os.environ.copy()
            env['POSTGRE_NAME'] = connection.settings_dict['NAME']
            env['LS82_TEST_QUEUE'] = queue.name
            script = (
                "import os, django; django.setup(); "
                "from redis import Redis; from rq import Queue, SimpleWorker; "
                "r=Redis.from_url(os.environ['LS82_TEST_REDIS_URL']); "
                "q=Queue(os.environ['LS82_TEST_QUEUE'], connection=r); "
                "SimpleWorker([q], connection=r).work(burst=True)"
            )
            result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stderr[-4000:]
            row.refresh_from_db()
            assert row.state == 'scored'
            assert BlindGoldScore.objects.filter(submission=submission).count() == 1
        finally:
            queue.empty()
            redis.delete(queue.key)
            redis.close()


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL transaction-lock test')
class TestBlindQualityBoundaryConcurrency(provenance_fixtures.SyntheticTransactionTestCase):
    valid_reference = TestBlindQualityBoundary.valid_reference

    def test_multi_project_fixture_locks_all_projects_before_capture_epoch(self):
        from classification_disagreements.blind_provenance import lock_capture_epoch_for_read
        from classification_disagreements.models import BlindCaptureEpoch
        from core.management.commands.seed_classification_blind_e2e import _lock_fixture_projects

        organization = fixtures.OrganizationFactory()
        first = ProjectFactory(organization=organization, created_by=organization.created_by)
        second = ProjectFactory(organization=organization, created_by=organization.created_by)
        provenance_fixtures.enable_test_capture()
        reader_locked, seed_started, allow_reader = Event(), Event(), Event()

        def read():
            close_old_connections()
            try:
                with transaction.atomic():
                    Project.objects.select_for_update().get(pk=second.pk)
                    reader_locked.set()
                    if not allow_reader.wait(10):
                        raise AssertionError('fixture reader was not released')
                    lock_capture_epoch_for_read()
            finally:
                close_old_connections()

        def seed():
            close_old_connections()
            try:
                with transaction.atomic():
                    seed_started.set()
                    _lock_fixture_projects([second.pk, first.pk])
                    BlindCaptureEpoch.objects.select_for_update().get(pk=1)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            reading = pool.submit(read)
            try:
                assert reader_locked.wait(10)
                seeding = pool.submit(seed)
                assert seed_started.wait(10)
                with self.assertRaises(TimeoutError):
                    seeding.result(timeout=0.3)
            finally:
                allow_reader.set()
            reading.result(timeout=20)
            seeding.result(timeout=20)

    def test_capture_readers_share_epoch_lock_and_fence_new_exposure(self):
        from classification_disagreements.blind_provenance import lock_capture_epoch_for_read, record_label_metadata_access

        provenance_fixtures.enable_test_capture()
        account = UserFactory()
        entered = [Event(), Event()]
        release, writer_started = Event(), Event()

        def read(index):
            close_old_connections()
            try:
                with transaction.atomic():
                    lock_capture_epoch_for_read()
                    entered[index].set()
                    if not release.wait(10):
                        raise AssertionError('capture reader was not released')
            finally:
                close_old_connections()

        def record():
            close_old_connections()
            try:
                writer_started.set()
                record_label_metadata_access(account_id=account.pk)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=3) as pool:
            readers = [pool.submit(read, index) for index in range(2)]
            try:
                assert all(event.wait(10) for event in entered)
                writer = pool.submit(record)
                assert writer_started.wait(10)
                with self.assertRaises(TimeoutError):
                    writer.result(timeout=0.3)
            finally:
                release.set()
            for reader in readers:
                reader.result(timeout=20)
            writer.result(timeout=20)

    def test_editor_read_completes_before_exposure_and_next_read_is_revoked(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements import blind_editor_api
        from classification_disagreements.blind_mix import create_mixed_assignment
        from classification_disagreements.blind_provenance import record_label_metadata_access
        from classification_disagreements.blind_task_handles import issue_handle
        from projects.models import ProjectMember
        from rest_framework.test import APIClient
        from users.models import User

        self.organization = fixtures.OrganizationFactory()
        self.manager = self.organization.created_by
        self.project = ProjectFactory(organization=self.organization, created_by=self.manager)
        self.task = TaskFactory(project=self.project)
        reference = self.valid_reference()
        provenance_fixtures.enable_test_capture()
        member = UserFactory(active_organization=self.organization)
        self.organization.add_user(member)
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        ordinary = TaskFactory(project=self.project, data={'text': 'synthetic independent carrier 12345'})
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
                  'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        read_entered, release, exposure_started = Event(), Event(), Event()
        original = blind_editor_api.participant_text_data

        def pause(*args):
            read_entered.set()
            if not release.wait(10):
                raise AssertionError('editor read was not released')
            return original(*args)

        def read(handle):
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=member.pk))
                return client.get(f'/api/tasks/blind/{handle}/').status_code
            finally:
                close_old_connections()

        def expose():
            close_old_connections()
            try:
                exposure_started.set()
                record_label_metadata_access(account_id=member.pk)
            finally:
                close_old_connections()

        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ):
            chosen = create_mixed_assignment(task=ordinary, assignee=member,
                references=[{'id': reference.pk, 'hash': reference.manifest_hash}], ratio_basis_points=10000,
                principal=local_principal(self.manager), key=uuid4())
            handle = issue_handle(principal=local_principal(member), assignment_id=chosen.pk).task_handle
            with patch.object(blind_editor_api, 'participant_text_data', side_effect=pause):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    reading = pool.submit(read, handle)
                    try:
                        assert read_entered.wait(10)
                        exposure = pool.submit(expose)
                        assert exposure_started.wait(10)
                        with self.assertRaises(TimeoutError):
                            exposure.result(timeout=0.3)
                    finally:
                        release.set()
                    assert reading.result(timeout=20) == 200
                    exposure.result(timeout=20)
            assert read(handle) == 404

    def test_annotator_directory_rechecks_membership_after_waiting_for_project_lock(self):
        from projects.models import ProjectMember
        from rest_framework.test import APIClient
        from users.models import User

        organization = fixtures.OrganizationFactory()
        project = ProjectFactory(organization=organization, created_by=organization.created_by)
        member = UserFactory(active_organization=organization)
        organization.add_user(member)
        membership = ProjectMember.objects.create(project=project, user=member, role='annotator')
        started = Event()

        def read():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=member.pk))
                started.set()
                return client.get(f'/api/projects/{project.pk}/annotators/').status_code
            finally:
                close_old_connections()

        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{organization.pk}:{project.pk}': True}}
        with self.settings(**config), ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=project.pk)
                reading = pool.submit(read)
                assert started.wait(10)
                with self.assertRaises(TimeoutError):
                    reading.result(timeout=0.3)
                ProjectMember.objects.filter(pk=membership.pk).update(enabled=False)
            assert reading.result(timeout=20) == 404

    def test_label_read_waits_for_admission_then_revokes_quality_access(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements import blind_reassignment
        from classification_disagreements.models import BlindLabelExposure
        from projects.models import ProjectMember
        from rest_framework.test import APIClient
        from users.models import User

        self.organization = fixtures.OrganizationFactory()
        self.manager = self.organization.created_by
        self.project = ProjectFactory(organization=self.organization, created_by=self.manager)
        self.task = TaskFactory(project=self.project)
        original = UserFactory(active_organization=self.organization)
        self.organization.add_user(original)
        ProjectMember.objects.create(project=self.project, user=original, role='annotator')
        assignment = TaskAssignment.objects.create(task=self.task, project=self.project,
                                                   assignee=original, assigned_by=self.manager)
        reference = self.valid_reference()
        bind_marker(assignment, reference)
        assignment.cancel()
        provenance_fixtures.enable_test_capture()
        member = UserFactory(active_organization=self.organization)
        self.organization.add_user(member)
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        bound, release, started = Event(), Event(), Event()
        issue = blind_reassignment._issue_proof

        def hold_proof(*args, **kwargs):
            result = issue(*args, **kwargs)
            bound.set()
            if not release.wait(10):
                raise AssertionError('quality admission was not released')
            return result

        def admit():
            close_old_connections()
            try:
                return blind_reassignment.reassign_quality_task(
                    task=self.task, assignee=member, principal=local_principal(self.manager),
                ).pk
            finally:
                close_old_connections()

        def read_labels():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=member.pk))
                started.set()
                return client.get('/api/labels/').status_code
            finally:
                close_old_connections()

        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
                  'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True}}
        with self.settings(**config), patch.object(blind_reassignment, '_issue_proof', side_effect=hold_proof), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                admission = pool.submit(admit)
                try:
                    assert bound.wait(10)
                    reading = pool.submit(read_labels)
                    assert started.wait(10)
                    with self.assertRaises(TimeoutError):
                        reading.result(timeout=0.3)
                finally:
                    release.set()
                quality_id = admission.result(timeout=20)
                assert reading.result(timeout=20) == 200
        assert BlindLabelExposure.objects.filter(account_id=member.pk).exists()
        assert not Task.objects.for_user(member).filter(pk=self.task.pk).exists()
        assert TaskAssignment.objects.filter(pk=quality_id).exists()

    def test_view_update_rechecks_role_after_waiting_for_project_lock(self):
        from data_manager.api import ViewAPI
        from data_manager.models import View
        from projects.models import ProjectMember
        from rest_framework.test import APIClient
        from users.models import User

        organization = fixtures.OrganizationFactory()
        manager = organization.created_by
        project = ProjectFactory(organization=organization, created_by=manager)
        reviewer = UserFactory(active_organization=organization)
        organization.add_user(reviewer)
        membership = ProjectMember.objects.create(project=project, user=reviewer, role='reviewer')
        view = View.objects.create(project=project, user=manager, selected_items={'all': False, 'included': [123]})
        entered, release = Event(), Event()
        original = ViewAPI.get_object

        def pause(api):
            result = original(api)
            if not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise AssertionError('view update was not released')
            return result

        def update():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=reviewer.pk))
                return client.patch(f'/api/dm/views/{view.pk}/', {'data': {'title': 'changed'}}, format='json').status_code
            finally:
                close_old_connections()

        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{organization.pk}:{project.pk}': True}}
        with self.settings(**config), patch.object(ViewAPI, 'get_object', autospec=True, side_effect=pause):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(update)
                try:
                    assert entered.wait(10)
                    ProjectMember.objects.filter(pk=membership.pk).update(role='annotator')
                finally:
                    release.set()
                assert pending.result(timeout=20) == 404
        view.refresh_from_db()
        assert view.data != {'title': 'changed'}

    def test_review_view_list_fences_concurrent_role_change(self):
        from data_manager.models import View
        from data_manager.serializers import ViewSerializer
        from projects.models import ProjectMember
        from rest_framework.test import APIClient
        from users.models import User

        organization = fixtures.OrganizationFactory()
        manager = organization.created_by
        project = ProjectFactory(organization=organization, created_by=manager)
        reviewer = UserFactory(active_organization=organization)
        organization.add_user(reviewer)
        membership = ProjectMember.objects.create(project=project, user=reviewer, role='reviewer')
        view = View.objects.create(project=project, user=manager, selected_items={'all': False, 'included': [123]})
        entered, release, started = Event(), Event(), Event()
        original = ViewSerializer.to_representation

        def pause(serializer, value):
            entered.set()
            if not release.wait(10):
                raise AssertionError('view serialization was not released')
            return original(serializer, value)

        def read():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=reviewer.pk))
                response = client.get('/api/dm/views/', {'project': project.pk})
                return response.status_code, response.data
            finally:
                close_old_connections()

        def change_role():
            close_old_connections()
            try:
                started.set()
                ProjectMember.objects.filter(pk=membership.pk).update(role='annotator')
            finally:
                close_old_connections()

        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{organization.pk}:{project.pk}': True}}
        with self.settings(**config), patch.object(ViewSerializer, 'to_representation', autospec=True, side_effect=pause):
            with ThreadPoolExecutor(max_workers=2) as pool:
                reading = pool.submit(read)
                try:
                    assert entered.wait(10)
                    changing = pool.submit(change_role)
                    assert started.wait(10)
                    with self.assertRaises(TimeoutError):
                        changing.result(timeout=0.3)
                finally:
                    release.set()
                status, body = reading.result(timeout=20)
                changing.result(timeout=20)
            assert status == 200 and [row['id'] for row in body] == [view.pk]
            client = APIClient()
            client.force_authenticate(User.objects.get(pk=reviewer.pk))
            assert client.get(f'/api/dm/views/{view.pk}/').status_code == 404

    def test_reviewer_shell_fences_concurrent_role_change(self):
        from data_manager.api import TaskListAPI
        from projects.models import ProjectMember
        from rest_framework.test import APIClient
        from users.models import User

        organization = fixtures.OrganizationFactory()
        manager = organization.created_by
        project = ProjectFactory(organization=organization, created_by=manager)
        reviewer = UserFactory(active_organization=organization)
        organization.add_user(reviewer)
        membership = ProjectMember.objects.create(project=project, user=reviewer, role='reviewer')
        task = TaskFactory(project=project)
        TaskAssignment.objects.create(task=task, project=project, assignee=reviewer, assigned_by=manager)
        entered, release, started = Event(), Event(), Event()
        original = TaskListAPI.get_task_queryset

        def pause(view, request, prepare_params):
            entered.set()
            if not release.wait(10):
                raise AssertionError('reviewer shell was not released')
            return original(view, request, prepare_params)

        def read():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=reviewer.pk))
                response = client.get('/api/tasks/', {'project': project.pk})
                return response.status_code, response.data
            finally:
                close_old_connections()

        def change_role():
            close_old_connections()
            try:
                started.set()
                ProjectMember.objects.filter(pk=membership.pk).update(role='annotator')
            finally:
                close_old_connections()

        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{organization.pk}:{project.pk}': True}}
        with self.settings(**config), patch.object(TaskListAPI, 'get_task_queryset', autospec=True, side_effect=pause):
            with ThreadPoolExecutor(max_workers=2) as pool:
                reading = pool.submit(read)
                try:
                    assert entered.wait(10)
                    changing = pool.submit(change_role)
                    assert started.wait(10)
                    with self.assertRaises(TimeoutError):
                        changing.result(timeout=0.3)
                finally:
                    release.set()
                status, body = reading.result(timeout=20)
                changing.result(timeout=20)
            assert status == 200 and body['tasks'] == []
            client = APIClient()
            client.force_authenticate(User.objects.get(pk=reviewer.pk))
            assert client.get('/api/tasks/', {'project': project.pk}).status_code == 404

    def test_review_queue_fences_concurrent_role_revocation(self):
        from projects.models import ProjectMember
        from rest_framework.test import APIClient
        from tasks.serializers import SubmissionSerializer
        from tasks.submissions import _canonical_snapshot, _snapshot_hash
        from users.models import User

        organization = fixtures.OrganizationFactory()
        manager = organization.created_by
        project = ProjectFactory(organization=organization, created_by=manager)
        reviewer = UserFactory(active_organization=organization)
        author = UserFactory(active_organization=organization)
        for user in (reviewer, author):
            organization.add_user(user)
        membership = ProjectMember.objects.create(project=project, user=reviewer, role='reviewer')
        ProjectMember.objects.create(project=project, user=author, role='annotator')
        task = TaskFactory(project=project)
        assignment = TaskAssignment.objects.create(task=task, project=project, assignee=author, assigned_by=manager)
        annotation = Annotation.objects.create(task=task, project=project, completed_by=author, result=[])
        assignment.annotation = annotation
        assignment.save(update_fields=['annotation'])
        snapshot = _canonical_snapshot(annotation, assignment)
        submission = Submission.objects.create(assignment=assignment, annotation=annotation, revision=1,
            submitted_by=author, result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
        entered, release, started = Event(), Event(), Event()
        original = SubmissionSerializer.to_representation

        def pause(serializer, value):
            entered.set()
            if not release.wait(10):
                raise AssertionError('review queue was not released')
            return original(serializer, value)

        def read():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=reviewer.pk))
                response = client.get('/api/submissions/', {'project': project.pk, 'reviewable': 'true'})
                return response.status_code, response.data
            finally:
                close_old_connections()

        def revoke():
            close_old_connections()
            try:
                started.set()
                ProjectMember.objects.filter(pk=membership.pk).update(enabled=False)
            finally:
                close_old_connections()

        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{organization.pk}:{project.pk}': True}}
        with self.settings(**config), patch.object(SubmissionSerializer, 'to_representation', autospec=True, side_effect=pause):
            with ThreadPoolExecutor(max_workers=2) as pool:
                reading = pool.submit(read)
                try:
                    assert entered.wait(10)
                    revoking = pool.submit(revoke)
                    assert started.wait(10)
                    with self.assertRaises(TimeoutError):
                        revoking.result(timeout=0.3)
                finally:
                    release.set()
                status, body = reading.result(timeout=20)
                revoking.result(timeout=20)
            assert status == 200 and [row['id'] for row in body['results']] == [submission.pk]
            client = APIClient()
            client.force_authenticate(User.objects.get(pk=reviewer.pk))
            assert client.get('/api/submissions/', {'project': project.pk, 'reviewable': 'true'}).status_code in (403, 404)

        ProjectMember.objects.filter(pk=membership.pk).update(enabled=True)
        entered.clear()
        release.clear()
        started.clear()

        def decide():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=reviewer.pk))
                response = client.post(f'/api/submissions/{submission.pk}/review/',
                                       {'decision': 'approved'}, format='json')
                return response.status_code, response.data
            finally:
                close_old_connections()

        with self.settings(**config), patch.object(SubmissionSerializer, 'to_representation', autospec=True, side_effect=pause):
            with ThreadPoolExecutor(max_workers=2) as pool:
                deciding = pool.submit(decide)
                try:
                    assert entered.wait(10)
                    revoking = pool.submit(revoke)
                    assert started.wait(10)
                    with self.assertRaises(TimeoutError):
                        revoking.result(timeout=0.3)
                finally:
                    release.set()
                status, body = deciding.result(timeout=20)
                revoking.result(timeout=20)
            assert status == 200 and body['status'] == 'approved'

    def test_concurrent_handle_issuance_returns_one_binding(self):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_task_handles import issue_handle, lock_handle_for_write
        from classification_disagreements.models import BlindTaskHandle
        from projects.models import ProjectMember

        organization = fixtures.OrganizationFactory()
        manager = organization.created_by
        project = ProjectFactory(organization=organization, created_by=manager)
        member = UserFactory(active_organization=organization)
        organization.add_user(member)
        ProjectMember.objects.create(project=project, user=member, role='annotator')
        task = TaskFactory(project=project)
        assignment = TaskAssignment.objects.create(task=task, project=project, assignee=member, assigned_by=manager)
        principal = local_principal(member)
        start = Event()

        def issue():
            close_old_connections()
            try:
                if not start.wait(10):
                    raise AssertionError('handle issuance was not released')
                handle = issue_handle(principal=principal, assignment_id=assignment.pk)
                return (handle.pk, handle.task_handle, handle.assignment_handle)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = [pool.submit(issue) for _ in range(2)]
            start.set()
            results = [attempt.result(timeout=30) for attempt in attempts]
        assert results[0] == results[1]
        assert BlindTaskHandle.objects.filter(assignment_id_frozen=assignment.pk).count() == 1
        with self.assertRaises(RuntimeError):
            lock_handle_for_write(principal=principal, task_handle=results[0][1],
                                  assignment_handle=results[0][2], assignment_version=assignment.version)

    def test_only_one_concurrent_mixed_reassignment_can_commit(self):
        from access_control.identity import LocalIdentityProvider
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_mix import create_mixed_assignment, reassign_mixed_assignment
        from classification_disagreements.models import BlindMixDecision
        from projects.models import ProjectMember

        self.organization = fixtures.OrganizationFactory()
        self.manager = self.organization.created_by
        self.project = ProjectFactory(organization=self.organization, created_by=self.manager)
        self.task = TaskFactory(project=self.project)
        reference = self.valid_reference()
        provenance_fixtures.enable_test_capture()
        members = [UserFactory(active_organization=self.organization) for _ in range(3)]
        for member in members:
            self.organization.add_user(member)
            ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        tasks = [TaskFactory(project=self.project, data={'text': value}) for value in
                 ('original task text', 'new task alpha', 'new task beta')]
        config = {
            'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
            'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': {f'{self.organization.pk}:{self.project.pk}': True},
        }
        principal, first_key = local_principal(self.manager), uuid4()
        with self.settings(**config), patch(
            'classification_disagreements.blind_reassignment.get_identity_provider', return_value=LocalIdentityProvider(),
        ):
            first = create_mixed_assignment(task=tasks[0], assignee=members[0],
                references=[{'id': reference.pk, 'hash': reference.manifest_hash}],
                ratio_basis_points=10000, principal=principal, key=first_key)
            first.cancel()
            previous = BlindMixDecision.objects.get(project=self.project, key=first_key)
            start = Event()

            def attempt(index):
                close_old_connections()
                try:
                    if not start.wait(10):
                        raise AssertionError('concurrent reassignment was not released')
                    try:
                        chosen = reassign_mixed_assignment(previous_id=previous.pk, task=tasks[index],
                                                           assignee=members[index], principal=principal, key=uuid4())
                        return ('success', chosen.pk)
                    except ValidationError:
                        return ('denied', None)
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as pool:
                attempts = [pool.submit(attempt, index) for index in (1, 2)]
                start.set()
                results = [item.result(timeout=30) for item in attempts]
        assert sorted(result[0] for result in results) == ['denied', 'success']
        assert BlindMixDecision.objects.filter(predecessor_id_frozen=previous.pk).count() == 1
        assert TaskAssignment.objects.filter(task_id__in=[tasks[1].pk, tasks[2].pk]).count() == 1

    def test_privilege_change_waits_for_binding_and_then_rejects(self):
        from projects.models import ProjectMember

        project = ProjectFactory()
        actor = UserFactory()
        member = ProjectMember.objects.create(project=project, user=actor, role='annotator')
        task = TaskFactory(project=project)
        assignment = TaskAssignment.objects.create(task=task, project=project, assignee=actor)
        bound, release, started = Event(), Event(), Event()

        def bind():
            close_old_connections()
            try:
                with transaction.atomic():
                    bind_marker(assignment)
                    bound.set()
                    if not release.wait(10):
                        raise AssertionError('binding transaction was not released')
            finally:
                close_old_connections()

        def promote():
            close_old_connections()
            try:
                started.set()
                ProjectMember.objects.filter(pk=member.pk).update(role='manager')
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            binding = pool.submit(bind)
            try:
                assert bound.wait(10)
                promotion = pool.submit(promote)
                assert started.wait(10)
                with self.assertRaises(TimeoutError):
                    promotion.result(timeout=0.3)
            finally:
                release.set()
            binding.result(timeout=10)
            with self.assertRaises(DatabaseError):
                promotion.result(timeout=10)
        member.refresh_from_db()
        assert member.role == 'annotator'

    def test_marker_waits_for_existing_mutable_delivery_fence(self):
        project = ProjectFactory()
        task = TaskFactory(project=project)
        assignment = TaskAssignment.objects.create(task=task, project=project, assignee=UserFactory())
        locked, release, started = Event(), Event(), Event()

        def delivery():
            close_old_connections()
            try:
                with transaction.atomic():
                    Project.objects.select_for_update().get(pk=project.pk)
                    assert not has_immutable_release_boundary(project.pk)
                    locked.set()
                    if not release.wait(10):
                        raise AssertionError('delivery transaction was not released')
            finally:
                close_old_connections()

        def bind():
            close_old_connections()
            try:
                started.set()
                bind_marker(assignment)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            delivering = pool.submit(delivery)
            try:
                assert locked.wait(10)
                binding = pool.submit(bind)
                assert started.wait(10)
                with self.assertRaises(TimeoutError):
                    binding.result(timeout=0.3)
            finally:
                release.set()
            delivering.result(timeout=10)
            binding.result(timeout=10)
        assert not has_immutable_release_boundary(project.pk)
        assert BlindQualityAssignment.objects.filter(assignment_id=assignment.pk).exists()

    def test_marker_waits_for_concurrent_annotation_and_then_rejects(self):
        project = ProjectFactory()
        actor = UserFactory()
        task = TaskFactory(project=project)
        assignment = TaskAssignment.objects.create(task=task, project=project, assignee=actor)
        locked, release, started = Event(), Event(), Event()

        def write_annotation():
            close_old_connections()
            try:
                with transaction.atomic():
                    Task.objects.select_for_update().get(pk=task.pk)
                    current = TaskAssignment.objects.select_for_update().get(pk=assignment.pk)
                    annotation = Annotation.objects.create(task=task, project=project, completed_by=actor, result=[])
                    current.annotation = annotation
                    current.status = TaskAssignment.Status.IN_PROGRESS
                    current.save(update_fields=['annotation', 'status'])
                    locked.set()
                    if not release.wait(10):
                        raise AssertionError('annotation transaction was not released')
            finally:
                close_old_connections()

        def mark():
            close_old_connections()
            try:
                started.set()
                return bind_marker(assignment)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            writing = pool.submit(write_annotation)
            try:
                assert locked.wait(10)
                marking = pool.submit(mark)
                assert started.wait(10)
                with self.assertRaises(TimeoutError):
                    marking.result(timeout=0.3)
            finally:
                release.set()
            writing.result(timeout=10)
            with self.assertRaises(DatabaseError):
                marking.result(timeout=10)
        assert not BlindQualityAssignment.objects.filter(assignment_id=assignment.pk).exists()

    def test_reassignment_waits_for_marker_and_then_rejects(self):
        project = ProjectFactory()
        actor, other = UserFactory(), UserFactory()
        task = TaskFactory(project=project)
        assignment = TaskAssignment.objects.create(task=task, project=project, assignee=actor)
        bound, release, started = Event(), Event(), Event()

        def bind():
            close_old_connections()
            try:
                with transaction.atomic():
                    bind_marker(assignment)
                    bound.set()
                    if not release.wait(10):
                        raise AssertionError('binding transaction was not released')
            finally:
                close_old_connections()

        def reassign():
            close_old_connections()
            try:
                started.set()
                TaskAssignment.objects.create(task_id=task.pk, project_id=project.pk, assignee_id=other.pk)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            binding = pool.submit(bind)
            try:
                assert bound.wait(10)
                reassignment = pool.submit(reassign)
                assert started.wait(10)
                with self.assertRaises(TimeoutError):
                    reassignment.result(timeout=0.3)
            finally:
                release.set()
            binding.result(timeout=10)
            with self.assertRaises(DatabaseError):
                reassignment.result(timeout=10)
        assert TaskAssignment.objects.filter(task=task).count() == 1
