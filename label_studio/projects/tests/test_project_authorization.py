from organizations.tests.factories import OrganizationFactory
from projects.models import ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.test import APITestCase
from users.tests.factories import UserFactory
from tasks.tests.factories import TaskFactory
from tasks.models import Task
from unittest.mock import patch


class TestProjectAuthorization(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = OrganizationFactory()
        cls.manager = cls.organization.created_by
        cls.annotator = UserFactory(active_organization=cls.organization)
        cls.reviewer = UserFactory(active_organization=cls.organization)
        cls.unassigned = UserFactory(active_organization=cls.organization)

        cls.assigned_project = ProjectFactory(
            organization=cls.organization,
            created_by=cls.manager,
            title='Assigned Project',
        )
        cls.other_project = ProjectFactory(
            organization=cls.organization,
            created_by=cls.manager,
            title='Other Project',
        )

        ProjectMember.objects.create(
            project=cls.assigned_project,
            user=cls.annotator,
            role=ProjectMember.Role.ANNOTATOR,
        )
        ProjectMember.objects.create(
            project=cls.assigned_project,
            user=cls.reviewer,
            role=ProjectMember.Role.REVIEWER,
        )

    def test_annotator_project_list_is_membership_scoped(self):
        self.client.force_authenticate(user=self.annotator)

        response = self.client.get('/api/projects/')

        assert response.status_code == 200
        project_ids = {project['id'] for project in response.json()['results']}
        assert self.assigned_project.id in project_ids
        assert self.other_project.id not in project_ids

    def test_reviewer_project_list_is_membership_scoped(self):
        self.client.force_authenticate(user=self.reviewer)

        response = self.client.get('/api/projects/counts/')

        assert response.status_code == 200
        project_ids = {project['id'] for project in response.json()['results']}
        assert project_ids == {self.assigned_project.id}

    def test_same_organization_user_without_membership_cannot_open_project(self):
        self.client.force_authenticate(user=self.unassigned)

        response = self.client.get(f'/api/projects/{self.assigned_project.id}/')

        assert response.status_code == 404

    def test_annotator_can_view_but_cannot_modify_project(self):
        self.client.force_authenticate(user=self.annotator)

        get_response = self.client.get(f'/api/projects/{self.assigned_project.id}/')
        patch_response = self.client.patch(
            f'/api/projects/{self.assigned_project.id}/',
            data={'title': 'Unauthorized Rename'},
            format='json',
        )

        assert get_response.status_code == 200
        assert patch_response.status_code == 403

        self.assigned_project.refresh_from_db()
        assert self.assigned_project.title == 'Assigned Project'

    def test_creator_is_effective_manager_even_without_explicit_membership(self):
        ProjectMember.objects.filter(project=self.assigned_project, user=self.manager).delete()
        self.client.force_authenticate(user=self.manager)

        response = self.client.patch(
            f'/api/projects/{self.assigned_project.id}/',
            data={'title': 'Manager Rename'},
            format='json',
        )

        assert response.status_code == 200
        self.assigned_project.refresh_from_db()
        assert self.assigned_project.title == 'Manager Rename'

    def test_create_project_creates_manager_membership(self):
        self.client.force_authenticate(user=self.manager)

        response = self.client.post(
            '/api/projects/',
            data={
                'title': 'Created Through API',
                'label_config': '<View></View>',
            },
            format='json',
        )

        assert response.status_code == 201
        project_id = response.json()['id']

        membership = ProjectMember.objects.get(project_id=project_id, user=self.manager)
        assert membership.enabled is True
        assert membership.role == ProjectMember.Role.MANAGER

    def test_finished_tasks_are_not_multiplied_by_project_members(self):
        finished = TaskFactory(project=self.assigned_project)
        TaskFactory(project=self.assigned_project)
        Task.objects.filter(pk=finished.pk).update(is_labeled=True)
        self.client.force_authenticate(user=self.manager)

        for optimized in (True, False):
            for endpoint in ('/api/projects/', '/api/projects/counts/'):
                with self.subTest(optimized=optimized, endpoint=endpoint):
                    with patch('projects.functions.flag_set', return_value=optimized):
                        response = self.client.get(endpoint)
                    assert response.status_code == 200
                    project = next(p for p in response.json()['results'] if p['id'] == self.assigned_project.pk)
                    assert project['task_number'] == 2
                    assert project['finished_task_number'] == 1
