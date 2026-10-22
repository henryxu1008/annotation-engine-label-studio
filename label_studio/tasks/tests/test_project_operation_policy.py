from unittest.mock import patch

from access_control.identity import Principal
from access_control.project_operations import READ_WORK, SUBMIT, ProjectOperationPolicy
from django.test import override_settings
from organizations.models import OrganizationMember
from organizations.tests.factories import OrganizationFactory
from projects.models import ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase
from tasks.models import Annotation, AnnotationDraft, ReviewDecision, Submission, TaskAssignment
from tasks.submissions import create_submission
from tasks.tests.factories import AnnotationFactory, TaskFactory
from users.tests.factories import UserFactory


class PausedProvider:
    def evaluate(self, project):
        return ProjectOperationPolicy('test-paused-v1', 'paused', frozenset({READ_WORK}), 'PROJECT_PAUSED')


class DraftProvider:
    def evaluate(self, project):
        return ProjectOperationPolicy('test-draft-v1', 'draft', frozenset({READ_WORK}), 'PROJECT_DRAFT')


class SubmitOnlyProvider:
    def evaluate(self, project):
        return ProjectOperationPolicy('test-submit-v1', 'active', frozenset({SUBMIT}))


class InvalidProvider:
    def evaluate(self, project):
        return ProjectOperationPolicy('future-v1', 'unexpected', frozenset({READ_WORK, SUBMIT}))


class TestProjectOperationPolicy(APITestCase):
    def setUp(self):
        self.organization = OrganizationFactory()
        self.project = ProjectFactory(organization=self.organization)
        self.manager = self.organization.created_by
        self.annotator = UserFactory(active_organization=self.organization)
        self.reviewer = UserFactory(active_organization=self.organization)
        for user, role in [(self.annotator, 'annotator'), (self.reviewer, 'reviewer')]:
            ProjectMember.objects.create(project=self.project, user=user, role=role)
        self.task = TaskFactory(project=self.project)
        self.annotation = AnnotationFactory(task=self.task, completed_by=self.annotator, result=[])
        self.assignment = TaskAssignment.objects.create(
            task=self.task,
            project=self.project,
            assignee=self.annotator,
            assigned_by=self.manager,
            annotation=self.annotation,
        )
        self.submission = create_submission(
            assignment=self.assignment, annotation=self.annotation, actor=self.annotator
        )
        self.assignment.refresh_from_db()
        self.draft = AnnotationDraft.objects.create(
            task=self.task,
            user=self.annotator,
            annotation=self.annotation,
            assignment=self.assignment,
            result=[],
        )
        self.token = {'assignment_id': self.assignment.id, 'assignment_version': self.assignment.version}

    def test_all_editor_and_review_mutations_use_the_same_policy(self):
        attempts = [
            (
                self.annotator,
                'patch',
                f'/api/annotations/{self.annotation.id}/',
                {**self.token, 'result': [], 'submit_for_review': True},
            ),
            (self.annotator, 'delete', f'/api/annotations/{self.annotation.id}/', self.token),
            (self.annotator, 'post', f'/api/annotations/{self.annotation.id}/convert-to-draft', self.token),
            (self.annotator, 'post', f'/api/tasks/{self.task.id}/annotations/', {**self.token, 'result': []}),
            (self.annotator, 'post', f'/api/tasks/{self.task.id}/drafts', {**self.token, 'result': []}),
            (
                self.annotator,
                'patch',
                f'/api/drafts/{self.draft.id}/',
                {**self.token, 'result': [], 'draft_revision': self.draft.revision},
            ),
            (
                self.annotator,
                'delete',
                f'/api/drafts/{self.draft.id}/',
                {**self.token, 'draft_revision': self.draft.revision},
            ),
            (self.reviewer, 'post', f'/api/submissions/{self.submission.id}/review/', {'decision': 'approved'}),
            (self.manager, 'post', '/api/task-assignments/', {'task': self.task.id, 'assignee': self.manager.id}),
        ]
        for provider in [
            'tasks.tests.test_project_operation_policy.PausedProvider',
            'tasks.tests.test_project_operation_policy.DraftProvider',
            None,
            'tasks.tests.test_project_operation_policy.InvalidProvider',
        ]:
            with override_settings(ANNOTATION_PROJECT_OPERATION_PROVIDER=provider):
                for user, method, url, data in attempts:
                    with self.subTest(provider=provider, url=url, method=method):
                        self.client.force_authenticate(user=user)
                        response = getattr(self.client, method)(url, data, format='json')
                        assert response.status_code == 403, response.content
        self.submission.refresh_from_db()
        self.draft.refresh_from_db()
        assert self.submission.status == Submission.Status.PENDING
        assert self.draft.revision == 1
        assert Annotation.objects.filter(pk=self.annotation.pk).exists()
        assert not ReviewDecision.objects.exists()
        assert TaskAssignment.objects.count() == 1

    @override_settings(
        ANNOTATION_PROJECT_OPERATION_PROVIDER='tasks.tests.test_project_operation_policy.PausedProvider'
    )
    def test_security_cancellation_remains_available_while_paused(self):
        self.client.force_authenticate(user=self.manager)
        response = self.client.delete(f'/api/task-assignments/{self.assignment.pk}/')
        assert response.status_code == 204, response.content
        self.assignment.refresh_from_db()
        assert self.assignment.status == TaskAssignment.Status.CANCELLED
        assert AnnotationDraft.objects.filter(pk=self.draft.pk).exists()
        assert Submission.objects.filter(pk=self.submission.pk).exists()

    def test_assignment_audit_uses_provider_mapped_actor(self):
        principal = Principal('test:manager', 'test', self.manager.email, self.manager.pk)
        task = TaskFactory(project=self.project)
        self.client.force_authenticate(user=self.annotator)
        with patch('tasks.api.resolve_actor', return_value=(principal, self.manager)):
            response = self.client.post(
                '/api/task-assignments/',
                {'task': task.pk, 'assignee': self.annotator.pk, 'assigned_by': self.annotator.pk},
                format='json',
            )
        assert response.status_code == 201, response.content
        assert TaskAssignment.objects.get(pk=response.json()['id']).assigned_by_id == self.manager.pk

    def test_revoked_organization_membership_denies_stale_editor_and_reviewer(self):
        OrganizationMember.objects.filter(user__in=[self.annotator, self.reviewer]).update(
            deleted_at='2026-10-20T00:00Z'
        )
        self.client.force_authenticate(user=self.annotator)
        response = self.client.patch(
            f'/api/annotations/{self.annotation.pk}/', {**self.token, 'result': []}, format='json'
        )
        assert response.status_code == 403, response.content
        self.client.force_authenticate(user=self.reviewer)
        response = self.client.post(
            f'/api/submissions/{self.submission.pk}/review/', {'decision': 'approved'}, format='json'
        )
        assert response.status_code == 403, response.content
        assert not ReviewDecision.objects.exists()

    def test_manager_does_not_gain_review_permission(self):
        self.client.force_authenticate(user=self.manager)
        response = self.client.post(
            f'/api/submissions/{self.submission.pk}/review/', {'decision': 'approved'}, format='json'
        )
        assert response.status_code == 403

    def test_submission_rejects_a_stale_assignment_scope_without_rewriting_it(self):
        other_project = ProjectFactory()
        TaskAssignment.objects.filter(pk=self.assignment.pk).update(project_id=other_project.pk)
        with self.assertRaises(ValidationError):
            create_submission(assignment=self.assignment, annotation=self.annotation, actor=self.annotator)
        self.assignment.refresh_from_db()
        assert self.assignment.project_id == other_project.pk
        assert Submission.objects.count() == 1

    @override_settings(
        ANNOTATION_PROJECT_OPERATION_PROVIDER='tasks.tests.test_project_operation_policy.PausedProvider'
    )
    def test_paused_task_remains_readable_but_editor_capability_is_false(self):
        self.client.force_authenticate(user=self.annotator)
        response = self.client.get(f'/api/tasks/{self.task.pk}/')
        assert response.status_code == 200, response.content
        assert response.json()['can_label'] is False
