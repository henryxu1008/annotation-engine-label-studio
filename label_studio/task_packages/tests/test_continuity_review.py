"""Independent-review regressions for the complete fixed-package candidate."""

from unittest.mock import patch

from access_control.identity import Principal
from data_manager.serializers import DataManagerTaskSerializer
from django.test import override_settings
from projects.models import LabelStreamHistory, Project, ProjectMember
from rest_framework.test import APIClient, APITestCase
from tasks.models import AnnotationDraft, Task, TaskAssignment
from tasks.serializers import NextTaskSerializer
from tasks.tests.factories import AnnotationFactory

from .test_packages import PackageFixture


@override_settings(ANNOTATION_FIXED_PACKAGES_ENABLED=True)
class TestPackageReviewFindings(PackageFixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.package_a = self.create(name='A', task_ids=[self.tasks[0].pk]).json()['id']
        self.assignment_a = self.assignment(task=self.tasks[0])
        self.assignment(task=self.tasks[1], user=self.other)

    def mapped_request(self, toolbar, *, transport_member=True, history=False):
        # Both people are ordinary annotators. A session manager would mask
        # the first candidate query's accidental use of the transport account.
        if not transport_member:
            ProjectMember.objects.filter(project=self.project, user=self.other).delete()
        self.client.force_authenticate(self.other)
        principal = Principal(f'test:{self.annotator.pk}', 'test', 'mapped', self.annotator.pk)
        with patch('access_control.identity.get_identity_provider') as provider:
            provider.return_value.resolve.return_value = principal
            if toolbar:
                return self.client.post(
                    f'/api/dm/actions/?project={self.project.pk}&id=next_task&task_package={self.package_a}',
                    {'selectedItems': {'all': True, 'excluded': []}},
                    format='json',
                )
            endpoint = 'label-stream-history' if history else 'next'
            return self.client.get(f'/api/projects/{self.project.pk}/{endpoint}/', {'task_package': self.package_a})

    def test_toolbar_uses_mapped_annotator_before_filtering_queue(self):
        response = self.mapped_request(toolbar=True)
        assert response.status_code == 200, response.content
        assert response.json()['id'] == self.tasks[0].pk
        assert response.json()['assignment_id'] == self.assignment_a.pk

    def test_legacy_next_uses_mapped_annotator_before_filtering_queue(self):
        response = self.mapped_request(toolbar=False)
        assert response.status_code == 200, response.content
        assert response.json()['id'] == self.tasks[0].pk
        assert response.json()['assignment_id'] == self.assignment_a.pk

    def test_toolbar_project_entrance_uses_actor_without_transport_membership(self):
        response = self.mapped_request(toolbar=True, transport_member=False)
        assert response.status_code == 200, response.content
        assert response.json()['assignment_id'] == self.assignment_a.pk

    def test_legacy_next_project_entrance_uses_actor_without_transport_membership(self):
        response = self.mapped_request(toolbar=False, transport_member=False)
        assert response.status_code == 200, response.content
        assert response.json()['assignment_id'] == self.assignment_a.pk

    def test_history_project_entrance_uses_actor_without_transport_membership(self):
        LabelStreamHistory.objects.create(
            user=self.annotator,
            project=self.project,
            data=[{'taskId': self.tasks[0].pk, 'annotationId': None}],
        )
        response = self.mapped_request(toolbar=False, transport_member=False, history=True)
        assert response.status_code == 200, response.content
        assert [row['taskId'] for row in response.json()] == [self.tasks[0].pk]

    def test_mapped_package_and_task_reads_without_transport_membership(self):
        ProjectMember.objects.filter(project=self.project, user=self.other).delete()
        self.client.force_authenticate(self.other)
        principal = Principal(f'test:{self.annotator.pk}', 'test', 'mapped', self.annotator.pk)
        with patch('access_control.identity.get_identity_provider') as provider:
            provider.return_value.resolve.return_value = principal
            response = self.detail(self.package_a)
            assert response.status_code == 200, response.content
            response = self.client.get(f'/api/tasks/{self.tasks[0].pk}/', {'task_package': self.package_a})
            assert response.status_code == 200, response.content
            assert response.json()['assignment_id'] == self.assignment_a.pk

    def entrance_requests(self, *, scoped=True):
        context = {'task_package': self.package_a} if scoped else {}
        query = f'project={self.project.pk}&id=next_task' + (f'&task_package={self.package_a}' if scoped else '')
        return [
            self.client.post(f'/api/dm/actions/?{query}', {}, format='json'),
            self.client.get(f'/api/projects/{self.project.pk}/next/', context),
            self.client.get(f'/api/projects/{self.project.pk}/label-stream-history/', context),
        ]

    def test_scoped_entrances_never_borrow_transport_managers_project_role(self):
        ProjectMember.objects.filter(project=self.project, user=self.annotator).delete()
        self.client.force_authenticate(self.manager)
        principal = Principal(f'test:{self.annotator.pk}', 'test', 'mapped', self.annotator.pk)
        with patch('access_control.identity.get_identity_provider') as provider:
            provider.return_value.resolve.return_value = principal
            for response in self.entrance_requests():
                assert response.status_code in (403, 404), response.content

    def test_scoped_entrances_deny_actor_organization_revocation(self):
        self.annotator.om_through.filter(organization=self.organization).update(deleted_at=self.project.created_at)
        self.client.force_authenticate(self.manager)
        principal = Principal(f'test:{self.annotator.pk}', 'test', 'mapped', self.annotator.pk)
        with patch('access_control.identity.get_identity_provider') as provider:
            provider.return_value.resolve.return_value = principal
            for response in self.entrance_requests():
                assert response.status_code in (403, 404), response.content

    def test_unscoped_entrances_keep_legacy_transport_project_checks(self):
        ProjectMember.objects.filter(project=self.project, user=self.other).delete()
        self.client.force_authenticate(self.other)
        principal = Principal(f'test:{self.annotator.pk}', 'test', 'mapped', self.annotator.pk)
        with patch('access_control.identity.get_identity_provider') as provider:
            provider.return_value.resolve.return_value = principal
            for response in self.entrance_requests(scoped=False):
                assert response.status_code in (403, 404), response.content

    def attach_annotation(self):
        annotation = AnnotationFactory(task=self.tasks[0], completed_by=self.annotator, result=[])
        self.assignment_a.annotation = annotation
        self.assignment_a.save(update_fields=['annotation'])
        self.submission(self.assignment_a, status='rejected')
        return annotation

    def reassign_same_actor(self):
        manager = APIClient()
        manager.force_authenticate(self.manager)
        response = manager.delete(f'/api/task-assignments/{self.assignment_a.pk}/')
        assert response.status_code == 204, response.content
        response = manager.post(
            '/api/task-assignments/',
            {'task': self.tasks[0].pk, 'assignee': self.annotator.pk},
            format='json',
        )
        assert response.status_code == 201, response.content
        return response.json()['id']

    def materialized_reassignment(self, serializer_class, next_task):
        annotation = self.attach_annotation()
        self.client.force_authenticate(self.annotator)
        original = serializer_class.to_representation

        def rotate(serializer, task):
            data = original(serializer, task)
            self.reassign_same_actor()
            return data

        with patch.object(serializer_class, 'to_representation', rotate):
            if next_task == 'toolbar':
                response = self.client.post(
                    f'/api/dm/actions/?project={self.project.pk}&id=next_task&task_package={self.package_a}',
                    {'selectedItems': {'all': True, 'excluded': []}},
                    format='json',
                )
            elif next_task:
                response = self.client.get(
                    f'/api/projects/{self.project.pk}/next/',
                    {'task_package': self.package_a},
                )
            else:
                response = self.client.get(f'/api/tasks/{self.tasks[0].pk}/', {'task_package': self.package_a})
        assert self.client.get(f'/api/annotations/{annotation.pk}/').status_code in (403, 404)
        assert response.status_code in (403, 404), response.content

    def test_next_rejects_old_payload_after_same_actor_reassignment(self):
        self.materialized_reassignment(NextTaskSerializer, next_task=True)

    def test_direct_task_rejects_old_payload_after_same_actor_reassignment(self):
        self.materialized_reassignment(DataManagerTaskSerializer, next_task=False)

    def test_toolbar_rejects_old_payload_after_same_actor_reassignment(self):
        self.materialized_reassignment(NextTaskSerializer, next_task='toolbar')

    def test_task_rejects_changed_assignment_version_without_reassignment(self):
        self.client.force_authenticate(self.annotator)
        original = DataManagerTaskSerializer.to_representation

        def rotate(serializer, task):
            data = original(serializer, task)
            TaskAssignment.objects.filter(pk=self.assignment_a.pk).update(version=self.assignment_a.version + 1)
            return data

        with patch.object(DataManagerTaskSerializer, 'to_representation', rotate):
            response = self.client.get(f'/api/tasks/{self.tasks[0].pk}/', {'task_package': self.package_a})
        assert response.status_code == 403, response.content
        assert response.json()['display_context']['reason'] == 'PACKAGE_ACCESS_CHANGED'

    def test_task_read_preserves_current_annotation_draft_and_assignment(self):
        annotation = self.attach_annotation()
        draft = AnnotationDraft.objects.create(
            task=self.tasks[0],
            user=self.annotator,
            assignment=self.assignment_a,
            annotation=annotation,
            result=[],
        )
        self.client.force_authenticate(self.annotator)
        response = self.client.get(f'/api/tasks/{self.tasks[0].pk}/', {'task_package': self.package_a})
        assert response.status_code == 200, response.content
        assert [row['id'] for row in response.json()['annotations']] == [annotation.pk]
        assert [row['id'] for row in response.json()['drafts']] == [draft.pk]
        assert response.json()['assignment_id'] == self.assignment_a.pk
        assert response.json()['can_label'] is True

    def test_manager_demotion_cannot_return_another_actors_materialized_annotation(self):
        self.attach_annotation()
        self.assignment(task=self.tasks[0], user=self.other)
        ProjectMember.objects.filter(project=self.project, user=self.other).update(role='manager')
        self.client.force_authenticate(self.other)
        original = DataManagerTaskSerializer.to_representation

        def demote(serializer, task):
            data = original(serializer, task)
            assert data['annotations']
            ProjectMember.objects.filter(project=self.project, user=self.other).update(role='annotator')
            return data

        with patch.object(DataManagerTaskSerializer, 'to_representation', demote):
            response = self.client.get(f'/api/tasks/{self.tasks[0].pk}/', {'task_package': self.package_a})
        assert response.status_code == 403, response.content

    def test_history_drops_inaccessible_annotation_reference_without_pruning_history(self):
        annotation = self.attach_annotation()
        history = LabelStreamHistory.objects.create(
            user=self.annotator,
            project=self.project,
            data=[{'taskId': self.tasks[0].pk, 'annotationId': annotation.pk}],
        )
        self.reassign_same_actor()
        self.client.force_authenticate(self.annotator)
        assert self.client.get(f'/api/annotations/{annotation.pk}/').status_code in (403, 404)
        response = self.client.get(
            f'/api/projects/{self.project.pk}/label-stream-history/',
            {'task_package': self.package_a},
        )
        assert response.status_code == 200, response.content
        assert response.json() == [{'taskId': self.tasks[0].pk, 'annotationId': None}]
        history.refresh_from_db()
        assert history.data[0]['annotationId'] == annotation.pk

    def test_history_preserves_current_annotation_and_managers_existing_read_scope(self):
        annotation = self.attach_annotation()
        for actor in (self.annotator, self.manager):
            LabelStreamHistory.objects.create(
                user=actor,
                project=self.project,
                data=[{'taskId': self.tasks[0].pk, 'annotationId': annotation.pk}],
            )
            self.client.force_authenticate(actor)
            response = self.client.get(
                f'/api/projects/{self.project.pk}/label-stream-history/',
                {'task_package': self.package_a},
            )
            assert response.status_code == 200, response.content
            assert response.json()[0]['annotationId'] == annotation.pk

    @override_settings(ANNOTATION_PROJECT_OPERATION_PROVIDER='task_packages.tests.test_packages.ChangedProjectPolicy')
    def test_task_action_flag_narrows_when_project_becomes_read_only_during_serialization(self):
        self.client.force_authenticate(self.annotator)
        original = DataManagerTaskSerializer.to_representation

        def pause(serializer, task):
            data = original(serializer, task)
            assert data['can_label'] is True
            Project.objects.filter(pk=self.project.pk).update(description='read-only')
            return data

        with patch.object(DataManagerTaskSerializer, 'to_representation', pause):
            response = self.client.get(f'/api/tasks/{self.tasks[0].pk}/', {'task_package': self.package_a})
        assert response.status_code == 200, response.content
        assert response.json()['can_label'] is False

    def test_consumed_last_review_page_has_locale_independent_recovery_code(self):
        additional = Task.objects.bulk_create(
            [Task(project=self.project, data={'text': 'synthetic review'}) for _ in range(50)]
        )
        package_id = self.create(task_ids=[self.tasks[0].pk, *(task.pk for task in additional)]).json()['id']
        self.submission(self.assignment_a)
        for task in additional:
            self.submission(self.assignment(task=task))
        self.client.force_authenticate(self.reviewer)
        params = {'project': self.project.pk, 'reviewable': 'true', 'task_package': package_id, 'page': 2}
        page = self.client.get('/api/submissions/', params)
        assert page.status_code == 200 and len(page.json()['results']) == 1, page.content
        submission_id = page.json()['results'][0]['id']
        decision = self.client.post(
            f'/api/submissions/{submission_id}/review/?task_package={package_id}',
            {'decision': 'approved'},
            format='json',
        )
        assert decision.status_code == 200, decision.content
        response = self.client.get('/api/submissions/', params, HTTP_ACCEPT_LANGUAGE='zh-CN')
        assert response.status_code == 404, response.content
        assert (
            response.json().get('display_context', {}).get('reason') == 'SUBMISSION_PAGE_OUT_OF_RANGE'
        ), response.content
