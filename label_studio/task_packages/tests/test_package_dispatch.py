from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.db import close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient
from task_packages.models import TaskPackageDispatch
from task_packages.tests.test_packages import PackageFixture
from tasks.models import Task, TaskAssignment


@skipUnless(connection.vendor == 'postgresql', 'Package dispatch requires PostgreSQL row locks.')
@override_settings(
    ANNOTATION_FIXED_PACKAGES_ENABLED=True,
    ANNOTATION_PACKAGE_DISPATCH_ENABLED=True,
    ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
)
class TestPackageDispatch(PackageFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        response = self.create()
        assert response.status_code == 201, response.content
        self.dispatch_url = f'{self.url}{response.json()["id"]}/dispatch/'

    def post(self, *, user=None, key=None, payload=None):
        client = APIClient()
        client.force_authenticate(user or self.manager)
        return client.post(
            self.dispatch_url,
            payload or {'assignee_id': self.annotator.pk},
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(key or uuid4()),
        )

    def test_atomic_receipt_replay_and_key_conflict(self):
        key = uuid4()
        first = self.post(key=key)
        assert first.status_code == 201, first.content
        assert first.json()['assignment_count'] == 3
        TaskAssignment.objects.filter(pk=first.json()['assignment_ids'][0]).update(status='cancelled')
        replay = self.post(key=key)
        assert replay.status_code == 200 and replay.json()['assignment_ids'] == first.json()['assignment_ids']
        assert replay.json()['replayed'] is True and TaskAssignment.objects.count() == 3
        assert self.post(key=key, payload={'assignee_id': self.other.pk}).status_code == 409

    def test_denials_and_unavailable_member_never_partially_dispatch(self):
        assert self.post(user=self.annotator).status_code in (403, 404)
        assert self.post(payload={'assignee_id': self.reviewer.pk}).status_code == 400
        assert self.post(payload={'assignee_id': self.annotator.pk, 'actor': self.manager.pk}).status_code == 400
        self.tasks[1].delete()
        assert self.post().status_code == 409
        assert TaskAssignment.objects.count() == TaskPackageDispatch.objects.count() == 0

    def test_existing_assignment_rejects_entire_batch(self):
        self.assignment(task=self.tasks[1])
        assert self.post().status_code == 409
        assert TaskAssignment.objects.count() == 1 and TaskPackageDispatch.objects.count() == 0

    def test_existing_assignment_for_another_member_allows_overlap(self):
        self.assignment(task=self.tasks[1], user=self.other)
        key = uuid4()
        first = self.post(key=key)
        assert first.status_code == 201, first.content
        assert first.json()['assignment_count'] == 3
        assert self.post(key=key).json()['assignment_ids'] == first.json()['assignment_ids']
        assert (
            TaskAssignment.objects.filter(task=self.tasks[1], status__in=TaskAssignment.ACTIVE_STATUSES).count() == 2
        )

    def test_transaction_failure_rolls_back_assignments(self):
        with patch('task_packages.dispatch.TaskPackageDispatch.objects.create', side_effect=RuntimeError('failure')):
            assert self.post().status_code == 500
        assert TaskAssignment.objects.count() == TaskPackageDispatch.objects.count() == 0

    def test_revoked_target_and_cross_project_are_rejected(self):
        from projects.models import ProjectMember

        ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=False)
        assert self.post().status_code == 400
        self.project.refresh_from_db()
        response = self.create(task_ids=[self.tasks[0].pk])
        foreign_url = self.dispatch_url.replace(f'/projects/{self.project.pk}/', '/projects/999999999/')
        assert (
            self.client.post(
                foreign_url, {'assignee_id': self.other.pk}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
            ).status_code
            == 404
        )
        assert response.status_code == 201
        assert TaskAssignment.objects.count() == TaskPackageDispatch.objects.count() == 0

    def test_nonempty_receipt_migration_cannot_reverse(self):
        assert self.post().status_code == 201
        executor = MigrationExecutor(connection)
        with self.assertRaisesRegex(RuntimeError, 'committed receipts'):
            executor.migrate([('task_packages', '0001_initial')])
        assert TaskPackageDispatch.objects.count() == 1
        # Later migrations may already have been reversed before the receipt
        # guard fires; use that historical model for cleanup and restoration.
        historical = (
            MigrationExecutor(connection)
            .loader.project_state([('task_packages', '0002_taskpackagedispatch')])
            .apps.get_model('task_packages', 'TaskPackageDispatch')
        )
        historical.objects.all().delete()
        executor = MigrationExecutor(connection)
        executor.migrate([('task_packages', '0001_initial')])
        MigrationExecutor(connection).migrate([('task_packages', '0007_pool_allocation_notices')])

    def test_paused_project_blocks_even_replay(self):
        key = uuid4()
        assert self.post(key=key).status_code == 201
        self.project.lifecycle_state = 'paused'
        self.project.save(update_fields=['lifecycle_state'])
        assert self.post(key=key).status_code == 403
        assert self.post().status_code == 403

    @override_settings(ANNOTATION_PACKAGE_DISPATCH_ENABLED=False)
    def test_dispatch_is_disabled_by_default_gate(self):
        assert self.post().status_code == 404
        assert TaskAssignment.objects.count() == TaskPackageDispatch.objects.count() == 0

    def test_concurrent_same_key_has_one_commit(self):
        assert connection.vendor == 'postgresql'
        barrier = Barrier(2)
        key = uuid4()

        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = self.post(key=key)
                return response.status_code, response.json()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        assert sorted(code for code, _ in results) == [200, 201], results
        assert results[0][1]['dispatch_id'] == results[1][1]['dispatch_id']
        assert TaskAssignment.objects.count() == 3 and TaskPackageDispatch.objects.count() == 1

    def test_concurrent_different_keys_cannot_duplicate_assignments(self):
        barrier = Barrier(2)

        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return self.post().status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        assert sorted(results) == [201, 409], results
        assert TaskAssignment.objects.count() == 3 and TaskPackageDispatch.objects.count() == 1

    def test_waiting_dispatch_rechecks_revoked_target(self):
        from projects.models import Project, ProjectMember
        from task_packages.dispatch import lock_project_operation

        entered = Event()

        def traced_lock(*args, **kwargs):
            entered.set()
            return lock_project_operation(*args, **kwargs)

        def worker():
            close_old_connections()
            try:
                return self.post().status_code
            finally:
                close_old_connections()

        with patch('task_packages.dispatch.lock_project_operation', side_effect=traced_lock):
            with ThreadPoolExecutor(max_workers=1) as pool:
                with transaction.atomic():
                    Project.objects.select_for_update().get(pk=self.project.pk)
                    result = pool.submit(worker)
                    assert entered.wait(10)
                    ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=False)
                assert result.result(timeout=20) == 400
        assert TaskAssignment.objects.count() == TaskPackageDispatch.objects.count() == 0

    def test_thousand_tasks_dispatch(self):
        extra = Task.objects.bulk_create([Task(project=self.project, data={}) for _ in range(997)])
        response = self.create(task_ids=[t.pk for t in [*self.tasks, *extra]])
        url = f'{self.url}{response.json()["id"]}/dispatch/'
        result = self.client.post(
            url, {'assignee_id': self.annotator.pk}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
        )
        assert result.status_code == 201, result.content
        assert result.json()['assignment_count'] == TaskAssignment.objects.count() == 1000
