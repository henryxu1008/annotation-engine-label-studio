from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from organizations.models import OrganizationMember
from organizations.tests.factories import OrganizationFactory
from projects.models import Project, ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.test import APIClient, APIRequestFactory, APITestCase, force_authenticate
from tasks.models import Task
from tasks.tests.factories import PredictionFactory, TaskFactory
from users.tests.factories import UserFactory


class TestProjectCountsListAPI(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project_1 = ProjectFactory()
        cls.project_2 = ProjectFactory(organization=cls.project_1.organization)
        Task.objects.create(project=cls.project_1, data={'text': 'Task 1'})
        Task.objects.create(project=cls.project_1, data={'text': 'Task 2'})
        Task.objects.create(project=cls.project_2, data={'text': 'Task 3'})

    def get_url(self, **params):
        return f'{reverse("projects:api:project-counts-list")}?{urlencode(params)}'

    def test_get_counts(self):
        client = APIClient()
        client.force_authenticate(user=self.project_1.created_by)
        response = client.get(self.get_url(include='id,task_number,finished_task_number,total_predictions_number'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['count'], 2)
        expected = [
            {
                'id': self.project_1.id,
                'task_number': 2,
                'finished_task_number': 0,
                'total_predictions_number': 0,
            },
            {
                'id': self.project_2.id,
                'task_number': 1,
                'finished_task_number': 0,
                'total_predictions_number': 0,
            },
        ]
        actual = sorted(response.json()['results'], key=lambda d: d['id'])
        self.assertEqual(actual, expected)


class TestProjectModelVersionsAPI(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = ProjectFactory()
        cls.user = cls.project.created_by

        cls.task = TaskFactory(project=cls.project)
        cls.prediction_m1 = PredictionFactory(task=cls.task, model_version='model_1')
        cls.prediction_m1_2 = PredictionFactory(task=cls.task, model_version='model_1')
        cls.prediction_m2 = PredictionFactory(task=cls.task, model_version='model_2')
        cls.prediction_m3 = PredictionFactory(task=cls.task, model_version='model_3')

        # To test ordering by last used
        cls.prediction_m2.created_at = timezone.now()
        cls.prediction_m2.save()

    def test_no_params(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(f'/api/projects/{self.project.id}/model-versions')
        assert response.status_code == 200
        assert response.json() == {
            'model_2': 1,
            'model_3': 1,
            'model_1': 2,
        }

    def test_limit(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(f'/api/projects/{self.project.id}/model-versions?limit=2')
        assert response.status_code == 200
        assert response.json() == {
            'model_2': 1,
            'model_3': 1,
        }

    def test_extended(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(f'/api/projects/{self.project.id}/model-versions?extended=true')
        assert response.status_code == 200
        assert response.json()['live'] is None
        assert response.json()['static'][0]['model_version'] == 'model_2'
        assert response.json()['static'][0]['count'] == 1
        assert response.json()['static'][1]['model_version'] == 'model_3'
        assert response.json()['static'][1]['count'] == 1
        assert response.json()['static'][2]['model_version'] == 'model_1'
        assert response.json()['static'][2]['count'] == 2



class TestProjectMemberAuthorizationHardening(APITestCase):
    def setUp(self):
        self.organization = OrganizationFactory()
        self.manager = self.organization.created_by
        self.project = ProjectFactory(
            organization=self.organization,
            created_by=self.manager,
        )
        ProjectMember.objects.update_or_create(
            project=self.project,
            user=self.manager,
            defaults={'role': ProjectMember.Role.MANAGER, 'enabled': True},
        )
        self.client.force_authenticate(user=self.manager)

    def _add_org_user(self):
        user = UserFactory(active_organization=self.organization)
        self.organization.add_user(user)
        return user

    def test_project_member_identity_is_immutable_on_update(self):
        original_user = self._add_org_user()
        replacement_user = self._add_org_user()
        membership = ProjectMember.objects.create(
            project=self.project,
            user=original_user,
            role=ProjectMember.Role.ANNOTATOR,
            enabled=True,
        )

        response = self.client.patch(
            f'/api/projects/{self.project.id}/members/{membership.id}/',
            data={'user_id': replacement_user.id},
            format='json',
        )

        assert response.status_code == 400
        membership.refresh_from_db()
        assert membership.user_id == original_user.id


    def test_full_update_allows_unchanged_member_identity(self):
        user = self._add_org_user()
        membership = ProjectMember.objects.create(
            project=self.project,
            user=user,
            role=ProjectMember.Role.ANNOTATOR,
            enabled=True,
        )

        response = self.client.put(
            f'/api/projects/{self.project.id}/members/{membership.id}/',
            data={
                'user_id': user.id,
                'role': ProjectMember.Role.REVIEWER,
                'enabled': True,
            },
            format='json',
        )

        assert response.status_code == 200, response.json()
        membership.refresh_from_db()
        assert membership.user_id == user.id
        assert membership.role == ProjectMember.Role.REVIEWER

    def test_revoked_manager_cannot_mutate_membership(self):
        revoked_manager = self._add_org_user()
        target_user = self._add_org_user()
        revoked_membership = ProjectMember.objects.create(
            project=self.project,
            user=revoked_manager,
            role=ProjectMember.Role.MANAGER,
            enabled=True,
        )
        target_membership = ProjectMember.objects.create(
            project=self.project,
            user=target_user,
            role=ProjectMember.Role.ANNOTATOR,
            enabled=True,
        )

        response = self.client.patch(
            f'/api/projects/{self.project.id}/members/{revoked_membership.id}/',
            data={'enabled': False},
            format='json',
        )
        assert response.status_code == 200, response.json()

        self.client.force_authenticate(user=revoked_manager)
        response = self.client.patch(
            f'/api/projects/{self.project.id}/members/{target_membership.id}/',
            data={'role': ProjectMember.Role.REVIEWER},
            format='json',
        )

        assert response.status_code in (403, 404)
        target_membership.refresh_from_db()
        assert target_membership.role == ProjectMember.Role.ANNOTATOR

    def test_soft_deleted_org_manager_does_not_satisfy_manager_quorum(self):
        organization = OrganizationFactory()
        manager_a = organization.created_by
        manager_b = UserFactory(active_organization=organization)
        organization.add_user(manager_b)
        project = ProjectFactory(
            organization=organization,
            created_by=None,
        )
        member_a = ProjectMember.objects.create(
            project=project,
            user=manager_a,
            role=ProjectMember.Role.MANAGER,
            enabled=True,
        )
        ProjectMember.objects.create(
            project=project,
            user=manager_b,
            role=ProjectMember.Role.MANAGER,
            enabled=True,
        )
        OrganizationMember.objects.filter(
            organization=organization,
            user=manager_b,
        ).update(deleted_at=timezone.now())

        self.client.force_authenticate(user=manager_a)
        response = self.client.patch(
            f'/api/projects/{project.id}/members/{member_a.id}/',
            data={'role': ProjectMember.Role.REVIEWER},
            format='json',
        )

        assert response.status_code == 400
        member_a.refresh_from_db()
        assert member_a.role == ProjectMember.Role.MANAGER

    def test_locked_project_authorizes_only_after_locked_read(self):
        from projects.api import ProjectMemberAPI

        events = []
        locked_queryset = Project.objects.filter(pk=self.project.pk)

        view = ProjectMemberAPI()
        membership = ProjectMember.objects.get(project=self.project, user=self.manager)
        view.kwargs = {'pk': self.project.pk, 'member_pk': membership.pk}
        request = APIRequestFactory().patch('/api/projects/member/')
        force_authenticate(request, user=self.manager)
        view.request = view.initialize_request(request)

        with (
            patch.object(Project.objects, 'select_for_update', side_effect=lambda: (
                events.append('lock_queryset') or locked_queryset
            )),
            patch('access_control.project_operations.get_object_or_404', side_effect=lambda queryset, **kwargs: (
                events.append('locked_read') or self.project
            )),
            patch.object(view, '_authorize_locked_project', side_effect=lambda project: events.append('authorize')),
        ):
            project = view._locked_project()

        assert project.pk == self.project.pk
        assert events == ['lock_queryset', 'locked_read', 'authorize']
