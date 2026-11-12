"""Regression tests for filtering and ordering of private peer annotations."""

import json
from unittest.mock import patch

from django.test import RequestFactory
from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request
from rest_framework.test import APITestCase
from tasks.models import Annotation
from tasks.serializers import NextTaskSerializer
from tasks.tests import test_task_assignment_authorization as fixtures


class TestPrivateFilterScope(APITestCase):
    setUpTestData = classmethod(fixtures.TestTaskAssignmentAuthorization.setUpTestData.__func__)

    def setUp(self):
        self.peer = Annotation.objects.create(project=self.project, task=self.shared_task, completed_by=self.annotator_b,
            result=[{'from_name': 'sentiment', 'to_name': 'text', 'type': 'choices',
                     'value': {'choices': ['synthetic-private-value']}}])
        self.assignment_b.annotation = self.peer
        self.assignment_b.save(update_fields=['annotation'])
        self.client.force_authenticate(self.annotator_a)

    def task_query(self, payload):
        return self.client.get('/api/tasks/', {'project': self.project.pk, 'query': json.dumps(payload)})

    def test_peer_result_filters_are_not_available_to_annotators(self):
        payload = {'filters': {'conjunction': 'and', 'items': [
            {'filter': 'filter:tasks:annotations_results', 'operator': 'contains', 'type': 'String',
             'value': 'synthetic-private-value'}]}}
        response = self.task_query(payload)
        assert response.status_code in (400, 403), response.data

    def test_peer_metadata_ordering_is_not_available_to_annotators(self):
        response = self.task_query({'ordering': ['tasks:total_annotations']})
        assert response.status_code in (400, 403), response.data

    def test_safe_task_id_filter_remains_available(self):
        payload = {'filters': {'conjunction': 'and', 'items': [
            {'filter': 'filter:tasks:id', 'operator': 'equal', 'type': 'Number', 'value': self.shared_task.pk}]}}
        response = self.task_query(payload)
        assert response.status_code == 200, response.data
        assert [row['id'] for row in response.data['tasks']] == [self.shared_task.pk]

    def test_blind_participant_cannot_query_task_identity_or_creation_order(self):
        config = {f'{self.organization.pk}:{self.project.pk}': True}
        with self.settings(ANNOTATION_CLASSIFICATION_BLIND_ENABLED=True,
                           ANNOTATION_CLASSIFICATION_BLIND_PROJECTS=config):
            for field, kind, value in (('id', 'Number', self.shared_task.pk),
                                       ('inner_id', 'Number', 1),
                                       ('created_at', 'Datetime', '2026-01-01T00:00:00.000Z')):
                payload = {'filters': {'conjunction': 'and', 'items': [
                    {'filter': f'filter:tasks:{field}', 'operator': 'equal', 'type': kind, 'value': value}]}}
                assert self.task_query(payload).status_code == 404
                assert self.task_query({'ordering': [f'tasks:{field}']}).status_code == 404
                assert self.client.get(f'/api/projects/{self.project.pk}/blind-work/',
                                       {'ordering': f'tasks:{field}'}).status_code == 404
            self.client.force_authenticate(self.manager)
            assert self.task_query({'ordering': ['tasks:id']}).status_code == 200

    def test_blind_default_order_is_stable_and_not_database_creation_order(self):
        import hashlib
        import hmac
        from classification_disagreements.models import BlindTaskHandle
        from classification_disagreements.tests.test_gold_scoring import CONFIG
        from django.conf import settings
        from projects.models import Project
        from tasks.models import Task, TaskAssignment
        from tasks.tests.factories import TaskFactory

        self.client.force_authenticate(self.annotator_b)
        extra = [TaskFactory(project=self.project) for _ in range(4)]
        for task in extra:
            TaskAssignment.objects.create(task=task, project=self.project, assignee=self.annotator_b,
                                          assigned_by=self.manager)
        config = {f'{self.organization.pk}:{self.project.pk}': True}
        with self.settings(ANNOTATION_CLASSIFICATION_BLIND_ENABLED=True,
                           ANNOTATION_CLASSIFICATION_BLIND_PROJECTS=config):
            expected_ids = [self.shared_task.pk, self.other_task.pk, *(task.pk for task in extra)]
            Project.objects.filter(pk=self.project.pk).update(label_config=CONFIG)
            Task.objects.filter(pk__in=expected_ids).update(data={'text': 'synthetic task'})
            seed = hmac.new(str(settings.SECRET_KEY).encode(),
                            f'blind-work-v1:{self.project.pk}:{self.annotator_b.pk}'.encode(), hashlib.sha256).hexdigest()
            expected_ids.sort(key=lambda pk: hashlib.md5(f'{seed}{pk}'.encode()).hexdigest())
            url = f'/api/projects/{self.project.pk}/blind-work/'
            first, second = self.client.get(url), self.client.get(url)
            assert first.status_code == second.status_code == 200
            handles = [row['id'] for row in first.data['results']]
            mapping = dict(BlindTaskHandle.objects.filter(task_handle__in=handles)
                           .values_list('task_handle', 'task_id_frozen'))
            assert all(handle >= 2 ** 51 for handle in handles)
            assert [mapping[handle] for handle in handles] == expected_ids
            assert [row['id'] for row in second.data['results']] == handles

    def test_nested_private_filter_is_denied(self):
        payload = {'filters': {'conjunction': 'and', 'items': [
            {'filter': 'filter:tasks:id', 'operator': 'equal', 'type': 'Number', 'value': self.shared_task.pk,
             'child_filter': {'filter': 'filter:tasks:total_annotations', 'operator': 'greater',
                              'type': 'Number', 'value': 0}}]}}
        assert self.task_query(payload).status_code in (400, 403)

    def test_saved_view_and_next_task_action_cannot_bypass_filter_scope(self):
        from data_manager.models import View

        view = View.objects.create(project=self.project, user=self.manager, ordering=['tasks:total_annotations'])
        response = self.client.get('/api/tasks/', {'project': self.project.pk, 'view': view.pk})
        assert response.status_code in (400, 403), response.data
        response = self.client.post(f'/api/dm/actions/?project={self.project.pk}&id=next_task', {
            'selectedItems': {'all': True, 'excluded': []}, 'ordering': ['tasks:total_annotations'],
        }, format='json')
        assert response.status_code in (400, 403), response.data

    def test_managers_keep_existing_filter_capability(self):
        self.client.force_authenticate(self.manager)
        response = self.task_query({'filters': {'conjunction': 'and', 'items': [
            {'filter': 'filter:tasks:annotations_results', 'operator': 'contains', 'type': 'String',
             'value': 'synthetic-private-value'}]}})
        assert response.status_code == 200, response.data

    def test_background_export_rechecks_its_creator_for_saved_views(self):
        from data_export.models import Export
        from data_manager.models import View

        view = View.objects.create(project=self.project, user=self.manager, ordering=['tasks:total_annotations'])
        export = Export.objects.create(project=self.project, created_by=self.manager)
        assert self.shared_task.pk in list(export._get_filtered_tasks(self.project.tasks, {'view': view.pk})
                                           .values_list('pk', flat=True))
        export.created_by = self.annotator_a
        with self.assertRaises(PermissionDenied):
            list(export._get_filtered_tasks(self.project.tasks, {'view': view.pk}))

    def test_manager_demotion_is_rechecked_in_executed_filter_query(self):
        from data_manager.prepare_params import PrepareParams
        from projects.models import ProjectMember
        from tasks.models import Task
        from users.tests.factories import UserFactory

        manager = UserFactory(active_organization=self.organization)
        self.organization.add_user(manager)
        membership = ProjectMember.objects.create(project=self.project, user=manager, role='manager')
        request = Request(RequestFactory().get('/'))
        request.user = manager
        params = PrepareParams(project=self.project.pk, ordering=['tasks:total_annotations'], request=request)
        queryset = Task.prepared.only_filtered(prepare_params=params)
        membership.role = 'annotator'
        membership.save(update_fields=['role'])
        assert list(queryset) == []

    def test_next_task_uses_mapped_actor_assignment_for_both_annotation_modes(self):
        request = Request(RequestFactory().get('/'))
        request.user = self.annotator_b  # authenticated transport user is not the mapped actor
        with patch('tasks.serializers.resolve_actor', return_value=(None, self.annotator_a)):
            for use_stub in (True, False):
                serializer = NextTaskSerializer(self.shared_task, context={
                    'request': request, 'annotations': True, 'annotations_stub': use_stub,
                })
                assert serializer.get_annotations(self.shared_task) == []

    def test_both_queues_select_tasks_for_the_mapped_actor(self):
        from access_control.identity import Principal

        self.assignment_b.cancel()  # the transport user has a different remaining task
        self.client.force_authenticate(self.annotator_b)
        actor = self.annotator_a

        class MappedProvider:
            def resolve(self, request):
                return Principal(f'mapped:{actor.pk}', 'test-mapped', actor.email, actor.pk)

        with patch('access_control.identity.get_identity_provider', return_value=MappedProvider()):
            for response in (
                self.client.get(f'/api/projects/{self.project.pk}/next/'),
                self.client.post(f'/api/dm/actions/?project={self.project.pk}&id=next_task',
                                 {'selectedItems': {'all': True, 'excluded': []}}, format='json'),
            ):
                assert response.status_code == 200, response.data
                assert response.data['id'] == self.shared_task.pk
                assert response.data['assignment_id'] == self.assignment_a.pk

    def test_ordinary_shared_task_retains_completion_without_exposing_editor_identity(self):
        self.shared_task.is_labeled = True
        self.shared_task.updated_by = self.annotator_b
        self.shared_task.save(update_fields=['is_labeled', 'updated_by'])
        for url in (f'/api/tasks/{self.shared_task.pk}/', f'/api/projects/{self.project.pk}/next/'):
            response = self.client.get(url)
            assert response.status_code == 200, response.data
            assert response.data['is_labeled'] is True
            assert response.data['updated_by'] == []
        self.client.force_authenticate(self.manager)
        response = self.client.get(f'/api/tasks/{self.shared_task.pk}/')
        assert response.status_code == 200 and response.data['is_labeled'] is True

    def test_sensitive_project_serializer_hides_peer_completion(self):
        from rest_framework.test import force_authenticate

        self.shared_task.is_labeled = True
        self.shared_task.save(update_fields=['is_labeled'])
        request = RequestFactory().get('/')
        force_authenticate(request, user=self.annotator_a)
        request = Request(request)
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True,
                  'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS':
                      {f'{self.project.organization_id}:{self.project.pk}': True}}
        with self.settings(**config):
            result = NextTaskSerializer(self.shared_task, context={'request': request}).data
        assert result['is_labeled'] is False
        assert not result['annotations']

    def test_mismatched_assignment_cannot_expose_or_edit_peer_annotation(self):
        self.assignment_b.annotation = None
        self.assignment_b.save(update_fields=['annotation'])
        self.assignment_a.annotation = self.peer
        self.assignment_a.save(update_fields=['annotation'])
        detail = self.client.get(f'/api/tasks/{self.shared_task.pk}/?fields=all')
        assert detail.status_code == 200 and not detail.data.get('annotations')
        listing = self.client.get(f'/api/tasks/{self.shared_task.pk}/annotations/')
        assert listing.status_code == 200 and not listing.data
        direct = self.client.get(f'/api/annotations/{self.peer.pk}/')
        assert direct.status_code in (403, 404)
        update = self.client.patch(f'/api/annotations/{self.peer.pk}/', {'result': []}, format='json')
        assert update.status_code in (403, 404)
        from rest_framework.exceptions import ValidationError
        from tasks.submissions import create_submission

        with self.assertRaises(ValidationError):
            create_submission(assignment=self.assignment_a, annotation=self.peer, actor=self.annotator_a)
