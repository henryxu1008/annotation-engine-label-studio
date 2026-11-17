from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.db import close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from projects.models import ProjectMember
from rest_framework.test import APIClient, APITestCase
from task_packages.models import TaskPackageClaim, TaskPackagePool
from task_packages.tests.test_packages import PackageFixture
from tasks.models import TaskAssignment


@skipUnless(connection.vendor == 'sqlite', 'This regression covers the unsupported SQLite backend.')
@override_settings(
    ANNOTATION_FIXED_PACKAGES_ENABLED=True,
    ANNOTATION_PACKAGE_CLAIM_ENABLED=True,
    ANNOTATION_PACKAGE_DISPATCH_ENABLED=True,
    ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LegacyProjectOperationProvider',
)
class TestUnsupportedPoolDatabase(PackageFixture, APITestCase):
    def test_dispatch_and_pool_configuration_fail_closed(self):
        response = self.create()
        assert response.status_code == 201, response.content
        package_id = response.json()['id']
        configured = self.client.put(
            f'{self.url}{package_id}/pool/',
            {'expected_revision': 0, 'candidate_ids': [self.annotator.pk], 'enabled': True, 'due_at': None},
            format='json',
        )
        dispatched = self.client.post(
            f'{self.url}{package_id}/dispatch/',
            {'assignee_id': self.annotator.pk},
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        )
        assert configured.status_code == dispatched.status_code == 409
        assert not TaskAssignment.objects.exists() and not TaskPackagePool.objects.exists()


@skipUnless(connection.vendor == 'postgresql', 'Claim pools require PostgreSQL row locks.')
@override_settings(
    ANNOTATION_FIXED_PACKAGES_ENABLED=True,
    ANNOTATION_PACKAGE_CLAIM_ENABLED=True,
    ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
)
class TestPackagePools(PackageFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        result = self.create()
        assert result.status_code == 201, result.content
        self.package_id = result.json()['id']
        self.pool_url = f'{self.url}{self.package_id}/pool/'
        self.claim_url = f'{self.url}{self.package_id}/claim/'

    def configure(self, **changes):
        payload = {
            'expected_revision': 0,
            'candidate_ids': [self.annotator.pk, self.other.pk],
            'enabled': True,
            'due_at': (timezone.now() + timedelta(days=1)).isoformat(),
        }
        payload.update(changes)
        return self.client.put(self.pool_url, payload, format='json')

    def claim(self, user=None, key=None, payload=None):
        client = APIClient()
        client.force_authenticate(user or self.annotator)
        return client.post(self.claim_url, payload or {}, format='json', HTTP_IDEMPOTENCY_KEY=str(key or uuid4()))

    def test_scoped_discovery_claim_and_idempotent_recovery(self):
        assert self.configure().status_code == 200
        self.client.force_authenticate(self.annotator)
        visible = self.client.get(f'{self.url}claim-pools/').json()['results']
        assert len(visible) == 1 and visible[0]['can_claim'] and 'candidate_ids' not in visible[0]
        self.client.force_authenticate(self.reviewer)
        assert self.client.get(f'{self.url}claim-pools/').status_code == 403
        assert self.claim(self.reviewer).status_code == 403
        key = uuid4()
        first = self.claim(key=key)
        assert first.status_code == 201, first.content
        assert first.json()['assignment_count'] == 3
        receipt = TaskPackageClaim.objects.get()
        assert receipt.idempotency_key == key, (receipt.idempotency_key, key)
        assert receipt.actor_id_snapshot == self.annotator.pk
        assert receipt.principal == f'label_studio_local:{self.annotator.pk}'
        replay = self.claim(key=key)
        assert replay.status_code == 200 and replay.json()['assignment_ids'] == first.json()['assignment_ids']
        assert self.claim(self.other).status_code == 409
        assert TaskAssignment.objects.count() == 3 and TaskPackageClaim.objects.count() == 1

    def test_configuration_requires_manager_revision_and_valid_candidate(self):
        assert self.configure(candidate_ids=[self.reviewer.pk]).status_code == 409
        assert self.configure(candidate_ids=[self.annotator.pk], due_at='2026-01-01T00:00:00').status_code == 400
        self.client.force_authenticate(self.annotator)
        assert self.configure().status_code == 403
        self.client.force_authenticate(self.manager)
        assert self.configure().status_code == 200
        assert self.configure().status_code == 409
        assert self.configure(expected_revision=1, enabled=False).status_code == 200
        assert self.claim().status_code == 409

    def test_claim_cannot_forge_actor_and_feature_gate_defaults_closed(self):
        assert self.configure().status_code == 200
        assert self.claim(payload={'assignee_id': self.other.pk}).status_code == 400
        with override_settings(ANNOTATION_PACKAGE_CLAIM_ENABLED=False):
            assert self.claim().status_code == 404
            assert self.client.get(f'{self.url}claim-pools/').status_code == 404
        assert not TaskAssignment.objects.exists() and not TaskPackageClaim.objects.exists()

    def test_deadline_is_enforced_and_does_not_cancel_existing_work(self):
        assert self.configure(due_at=(timezone.now() - timedelta(seconds=1)).isoformat()).status_code == 200
        assert self.claim().status_code == 409
        assert self.configure(expected_revision=1, due_at=None).status_code == 200
        assert self.claim().status_code == 201
        assert (
            self.configure(expected_revision=2, due_at=(timezone.now() - timedelta(seconds=1)).isoformat()).status_code
            == 200
        )
        assert TaskAssignment.objects.filter(status__in=TaskAssignment.ACTIVE_STATUSES).count() == 3
        self.client.force_authenticate(self.annotator)
        item = self.client.get(f'{self.url}claim-pools/').json()['results'][0]
        assert item['deadline_passed'] is True and item['overdue_count'] == 3

    def test_existing_assignments_and_unavailable_tasks_fail_closed(self):
        assert self.configure().status_code == 200
        self.assignment(user=self.other)
        assert self.claim().status_code == 409
        assert TaskAssignment.objects.count() == 1 and not TaskPackageClaim.objects.exists()
        TaskAssignment.objects.all().delete()
        self.tasks[1].delete()
        assert self.claim().status_code == 409
        assert not TaskAssignment.objects.exists() and not TaskPackageClaim.objects.exists()

    def test_revocation_and_paused_project_block_claim(self):
        assert self.configure().status_code == 200
        ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=False)
        assert self.claim().status_code in (403, 404)
        self.project.lifecycle_state = 'paused'
        self.project.save(update_fields=['lifecycle_state'])
        assert self.claim(self.other).status_code == 403
        assert not TaskAssignment.objects.exists()

    def test_failure_before_receipt_rolls_back_all_assignments(self):
        assert self.configure().status_code == 200
        with patch('task_packages.pools.TaskPackageClaim.objects.create', side_effect=RuntimeError('failure')):
            assert self.claim().status_code == 500
        assert not TaskAssignment.objects.exists() and not TaskPackageClaim.objects.exists()

    def test_two_candidates_claim_concurrently(self):
        assert connection.vendor == 'postgresql'
        assert self.configure().status_code == 200
        barrier = Barrier(2)

        def worker(user):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return self.claim(user).status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, [self.annotator, self.other]))
        assert sorted(results) == [201, 409], results
        assert TaskAssignment.objects.count() == 3 and TaskPackageClaim.objects.count() == 1
        assert TaskAssignment.objects.values('assignee_id').distinct().count() == 1

    def test_waiting_claim_rechecks_target_membership(self):
        from projects.models import Project
        from task_packages.pools import lock_project_operation

        assert self.configure().status_code == 200
        entered = Event()

        def traced_lock(*args, **kwargs):
            entered.set()
            return lock_project_operation(*args, **kwargs)

        def worker():
            close_old_connections()
            try:
                return self.claim().status_code
            finally:
                close_old_connections()

        with patch('task_packages.pools.lock_project_operation', side_effect=traced_lock):
            with ThreadPoolExecutor(max_workers=1) as executor:
                with transaction.atomic():
                    Project.objects.select_for_update().get(pk=self.project.pk)
                    result = executor.submit(worker)
                    assert entered.wait(10)
                    ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=False)
                assert result.result(timeout=20) == 403
        assert not TaskAssignment.objects.exists() and not TaskPackageClaim.objects.exists()

    def test_nonempty_pool_migration_cannot_reverse(self):
        assert self.configure().status_code == 200
        with self.assertRaisesRegex(RuntimeError, 'pool configuration or claims'):
            MigrationExecutor(connection).migrate([('task_packages', '0002_taskpackagedispatch')])
        assert TaskPackagePool.objects.count() == 1
