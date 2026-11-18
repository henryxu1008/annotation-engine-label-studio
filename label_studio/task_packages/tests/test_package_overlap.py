from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from uuid import uuid4

from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from projects.models import ProjectMember
from rest_framework.test import APIClient
from task_packages.models import TaskPackageDispatch
from task_packages.tests.test_packages import PackageFixture
from tasks.models import Annotation, TaskAssignment
from users.tests.factories import UserFactory


@skipUnless(connection.vendor == 'postgresql', 'Package dispatch requires PostgreSQL row locks.')
@override_settings(
    ANNOTATION_FIXED_PACKAGES_ENABLED=True,
    ANNOTATION_PACKAGE_DISPATCH_ENABLED=True,
    ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
)
class TestPackageOverlap(PackageFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        result = self.create(task_ids=[self.tasks[0].pk])
        assert result.status_code == 201, result.content
        self.package_id = result.json()['id']
        self.dispatch_url = f'{self.url}{self.package_id}/dispatch/'

    def dispatch(self, user, key=None):
        client = APIClient()
        client.force_authenticate(self.manager)
        return client.post(
            self.dispatch_url,
            {'assignee_id': user.pk},
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(key or uuid4()),
        )

    def annotate(self, user):
        client = APIClient()
        client.force_authenticate(user)
        task = client.get(f'/api/tasks/{self.tasks[0].pk}/')
        assert task.status_code == 200, task.content
        token = {name: task.json()[name] for name in ('assignment_id', 'assignment_version')}
        response = client.post(
            f'/api/tasks/{self.tasks[0].pk}/annotations/',
            {'result': [], 'submit_for_review': False, **token},
            format='json',
        )
        assert response.status_code == 201, response.content
        return response.json()['id'], token

    def assert_independent_work(self, users):
        receipts = []
        for user in users:
            key = uuid4()
            first = self.dispatch(user, key)
            assert first.status_code == 201, first.content
            replay = self.dispatch(user, key)
            assert replay.status_code == 200 and replay.json()['assignment_ids'] == first.json()['assignment_ids']
            receipts.append(first.json()['assignment_ids'][0])
        assert len(set(receipts)) == len(users)
        work = [self.annotate(user) for user in users]
        assert len({annotation_id for annotation_id, _ in work}) == len(users)
        for user, assignment_id, (annotation_id, token) in zip(users, receipts, work):
            assignment = TaskAssignment.objects.get(pk=assignment_id)
            annotation = Annotation.objects.get(pk=annotation_id)
            assert token['assignment_id'] == assignment_id
            assert assignment.annotation_id == annotation_id and annotation.completed_by_id == user.pk
            client = APIClient()
            client.force_authenticate(user)
            for other_id, other_token in work:
                if other_id == annotation_id:
                    continue
                assert client.get(f'/api/annotations/{other_id}/').status_code in (403, 404)
                assert client.patch(f'/api/annotations/{other_id}/', {'result': []}, format='json').status_code in (
                    403,
                    404,
                )
                forged = client.patch(
                    f'/api/annotations/{annotation_id}/',
                    {'result': [], **other_token},
                    format='json',
                )
                assert forged.status_code in (400, 403, 404, 409)
        return receipts, work

    def test_two_assignees_have_separate_annotations_and_revoked_token_is_denied(self):
        users = [self.annotator, self.other]
        receipts, work = self.assert_independent_work(users)
        manager = APIClient()
        manager.force_authenticate(self.manager)
        assert manager.delete(f'/api/task-assignments/{receipts[0]}/').status_code == 204
        client = APIClient()
        client.force_authenticate(users[0])
        stale = client.patch(f'/api/annotations/{work[0][0]}/', {'result': [], **work[0][1]}, format='json')
        assert stale.status_code in (403, 404, 409)
        assert TaskAssignment.objects.get(pk=receipts[1]).annotation_id == work[1][0]
        reassigned = self.dispatch(users[0])
        assert reassigned.status_code == 201, reassigned.content
        assert reassigned.json()['assignment_ids'][0] != receipts[0]
        stale_again = client.post(
            f'/api/tasks/{self.tasks[0].pk}/annotations/',
            {'result': [], 'submit_for_review': False, **work[0][1]},
            format='json',
        )
        assert stale_again.status_code in (403, 404, 409)
        assert Annotation.objects.filter(task=self.tasks[0]).count() == 2
        client.force_authenticate(users[1])
        assert client.get(f'/api/annotations/{work[1][0]}/').status_code == 200

    def test_three_assignees_have_separate_annotations(self):
        third = UserFactory(active_organization=self.organization)
        self.organization.add_user(third)
        ProjectMember.objects.create(project=self.project, user=third, role=ProjectMember.Role.ANNOTATOR)
        self.assert_independent_work([self.annotator, self.other, third])

    def test_concurrent_dispatches_and_same_key_retry_preserve_distinct_work(self):
        barrier = Barrier(2)
        keys = [uuid4(), uuid4()]

        def worker(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = self.dispatch([self.annotator, self.other][index], keys[index])
                return response.status_code, response.json()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(worker, range(2)))
        assert [item[0] for item in results] == [201, 201]
        assert TaskPackageDispatch.objects.count() == 2 and TaskAssignment.objects.count() == 2
        for index, user in enumerate([self.annotator, self.other]):
            replay = self.dispatch(user, keys[index])
            assert replay.status_code == 200
            assert replay.json()['assignment_ids'] == results[index][1]['assignment_ids']
        annotation_barrier = Barrier(2)

        def create_work(user):
            close_old_connections()
            try:
                annotation_barrier.wait(timeout=10)
                return self.annotate(user)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            work = list(executor.map(create_work, [self.annotator, self.other]))
        assert work[0][0] != work[1][0]

    def test_package_id_cannot_be_used_from_another_real_project(self):
        from projects.tests.factories import ProjectFactory

        foreign_project = ProjectFactory(organization=self.organization, created_by=self.manager)
        foreign_url = f'/api/projects/{foreign_project.pk}/task-packages/{self.package_id}/dispatch/'
        response = self.client.post(
            foreign_url,
            {'assignee_id': self.annotator.pk},
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        )
        assert response.status_code == 404
        assert not TaskAssignment.objects.exists() and not TaskPackageDispatch.objects.exists()
