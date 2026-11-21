from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from access_control.identity import Principal
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import Project
from rest_framework.test import APIClient
from task_packages.automation import scan_allocations
from task_packages.dispatch import DispatchConflict
from task_packages.models import TaskPackageClaim, TaskPackageDispatch, TaskPackageOverdueNotice, TaskPackagePool
from task_packages.pools import configure_pool
from task_packages.tests.test_packages import PackageFixture
from tasks.models import Task, TaskAssignment


@skipUnless(connection.vendor == 'postgresql', 'Package automation requires PostgreSQL row locks.')
@override_settings(
    ANNOTATION_FIXED_PACKAGES_ENABLED=True,
    ANNOTATION_PACKAGE_CLAIM_ENABLED=True,
    ANNOTATION_PACKAGE_DISPATCH_ENABLED=True,
    ANNOTATION_PACKAGE_ADVANCED_ENABLED=True,
    ANNOTATION_PACKAGE_AUTOMATION_ENABLED=True,
    ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
)
class TestPackageAutomation(PackageFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        created = self.create()
        assert created.status_code == 201, created.content
        self.package_id = created.json()['id']
        self.url_pool = f'{self.url}{self.package_id}/pool/'

    def configure(self, **changes):
        data = {
            'expected_revision': 0,
            'candidate_ids': [self.annotator.pk, self.other.pk],
            'due_at': None,
            'enabled': True,
            'priority': 0,
            'automatic': True,
        }
        data.update(changes)
        return self.client.put(self.url_pool, data, format='json')

    def test_automatic_allocation_skips_blind_projects(self):
        assert self.configure().status_code == 200
        with self.settings(
            ANNOTATION_CLASSIFICATION_BLIND_ENABLED=True,
            ANNOTATION_CLASSIFICATION_BLIND_PROJECTS={f'{self.organization.pk}:{self.project.pk}': True},
        ):
            result = scan_allocations()
            assert result['allocated'] == 0 and result['skipped'] == 1
            assert not TaskAssignment.objects.exists() and not TaskPackageDispatch.objects.exists()
        assert scan_allocations()['allocated'] == 1

    def test_generation_survives_edit_and_requires_explicit_reenable(self):
        assert self.configure().status_code == 200
        assert scan_allocations()['allocated'] == 1
        for assignment in TaskAssignment.objects.all():
            assignment.cancel()
        assert self.configure(expected_revision=1, priority=9).status_code == 200
        result = scan_allocations()
        assert result['allocated'] == 0 and TaskAssignment.objects.count() == 3
        assert self.configure(expected_revision=2, automatic=False).status_code == 200
        assert self.configure(expected_revision=3, automatic=True).status_code == 200
        assert scan_allocations()['allocated'] == 1
        assert TaskAssignment.objects.count() == 6 and TaskPackageDispatch.objects.count() == 2

    def test_legacy_update_cannot_silently_reenable_automatic_allocation(self):
        assert self.configure().status_code == 200
        body = {'expected_revision': 1, 'candidate_ids': [self.annotator.pk], 'due_at': None, 'enabled': True}
        assert self.client.put(self.url_pool, body, format='json').status_code == 409
        assert TaskPackagePool.objects.get().automation_generation == 1

    def test_platform_identity_cannot_authorize_local_background_impersonation(self):
        principal = Principal('platform:example', 'platform', 'synthetic', self.manager.pk)
        with self.assertRaises(DispatchConflict):
            configure_pool(
                project_id=self.project.pk,
                package_id=self.package_id,
                principal=principal,
                expected_revision=0,
                candidate_ids=[self.annotator.pk],
                due_at=None,
                enabled=True,
                automatic=True,
            )
        assert not TaskPackagePool.objects.exists()

    def test_two_background_workers_do_not_duplicate_assignments(self):
        assert self.configure().status_code == 200
        barrier = Barrier(2)

        def worker(_):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return scan_allocations()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(worker, range(2)))
        assert sum(result['allocated'] for result in results) == 1
        assert TaskAssignment.objects.count() == 3 and TaskPackageDispatch.objects.count() == 1

    def test_claim_and_background_allocation_cannot_both_win(self):
        assert self.configure().status_code == 200
        barrier = Barrier(2)

        def allocate():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return scan_allocations()
            finally:
                close_old_connections()

        def claim():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(self.annotator)
                barrier.wait(timeout=10)
                return client.post(
                    f'{self.url}{self.package_id}/claim/', {}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
                ).status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            allocated = executor.submit(allocate)
            claimed = executor.submit(claim)
            result, status = allocated.result(timeout=30), claimed.result(timeout=30)
        assert status in (201, 409), status
        assert result['allocated'] + int(status == 201) == 1
        assert TaskAssignment.objects.count() == 3
        assert TaskPackageDispatch.objects.count() + TaskPackageClaim.objects.count() == 1

    def test_global_disable_stops_work_even_if_a_pool_is_configured(self):
        assert self.configure().status_code == 200
        with override_settings(ANNOTATION_PACKAGE_AUTOMATION_ENABLED=False):
            assert scan_allocations()['allocated'] == 0
            with self.assertRaises(CommandError):
                call_command('run_package_automation', once=True, stdout=StringIO())
        assert not TaskAssignment.objects.exists()

    def test_identity_provider_change_stops_previously_authorized_automation(self):
        assert self.configure().status_code == 200
        with override_settings(ANNOTATION_IDENTITY_PROVIDER='platform.identity.Provider'):
            assert scan_allocations()['allocated'] == 0
        assert not TaskAssignment.objects.exists()

    def test_pause_is_a_barrier_to_automatic_allocation(self):
        assert self.configure().status_code == 200
        self.project.lifecycle_state = 'paused'
        self.project.save(update_fields=['lifecycle_state'])
        assert scan_allocations()['allocated'] == 0
        assert not TaskAssignment.objects.exists()

    def test_waiting_worker_rechecks_revoked_manager(self):
        from task_packages.automation import balanced_dispatch

        assert self.configure().status_code == 200
        entered = Event()

        def traced(*args, **kwargs):
            entered.set()
            return balanced_dispatch(*args, **kwargs)

        def worker():
            close_old_connections()
            try:
                return scan_allocations()
            finally:
                close_old_connections()

        with patch('task_packages.automation.balanced_dispatch', side_effect=traced):
            with ThreadPoolExecutor(max_workers=1) as executor:
                with transaction.atomic():
                    Project.objects.select_for_update().get(pk=self.project.pk)
                    future = executor.submit(worker)
                    assert entered.wait(10)
                    OrganizationMember.objects.filter(organization=self.organization, user=self.manager).update(
                        deleted_at=timezone.now()
                    )
                assert future.result(timeout=20)['allocated'] == 0
        assert not TaskAssignment.objects.exists()

    def test_waiting_worker_rechecks_disabled_automation(self):
        from task_packages.automation import balanced_dispatch

        assert self.configure().status_code == 200
        entered = Event()

        def traced(*args, **kwargs):
            entered.set()
            return balanced_dispatch(*args, **kwargs)

        def worker():
            close_old_connections()
            try:
                return scan_allocations()
            finally:
                close_old_connections()

        with patch('task_packages.automation.balanced_dispatch', side_effect=traced):
            with ThreadPoolExecutor(max_workers=1) as executor:
                with transaction.atomic():
                    Project.objects.select_for_update().get(pk=self.project.pk)
                    future = executor.submit(worker)
                    assert entered.wait(10)
                    TaskPackagePool.objects.filter(package_id=self.package_id).update(automatic=False)
                assert future.result(timeout=20)['allocated'] == 0
        assert not TaskAssignment.objects.exists()

    def test_restart_after_committed_allocation_does_not_allocate_again(self):
        from task_packages.automation import balanced_dispatch

        assert self.configure().status_code == 200

        def fail_after_commit(*args, **kwargs):
            balanced_dispatch(*args, **kwargs)
            raise RuntimeError('worker stopped after commit')

        with patch('task_packages.automation.balanced_dispatch', side_effect=fail_after_commit):
            with self.assertRaises(CommandError):
                call_command('run_package_automation', once=True, stdout=StringIO())
        assert TaskAssignment.objects.count() == 3 and TaskPackageDispatch.objects.count() == 1
        call_command('run_package_automation', once=True, stdout=StringIO())
        assert TaskAssignment.objects.count() == 3 and TaskPackageDispatch.objects.count() == 1

    def test_once_reconciles_notices_and_retries_after_transient_failure(self):
        assert self.configure(automatic=False).status_code == 200
        client = APIClient()
        client.force_authenticate(self.annotator)
        assert (
            client.post(
                f'{self.url}{self.package_id}/claim/', {}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
            ).status_code
            == 201
        )
        assert (
            self.configure(
                expected_revision=1, automatic=False, due_at=(timezone.now() - timedelta(seconds=1)).isoformat()
            ).status_code
            == 200
        )
        with patch('task_packages.automation.call_command', side_effect=RuntimeError('failure')):
            with self.assertRaises(CommandError):
                call_command('run_package_automation', once=True, stdout=StringIO())
        assert TaskPackageClaim.objects.count() == 1 and not TaskPackageOverdueNotice.objects.exists()
        call_command('run_package_automation', once=True, stdout=StringIO())
        call_command('run_package_automation', once=True, stdout=StringIO())
        assert TaskPackageOverdueNotice.objects.count() == 1

    def test_automatic_allocation_produces_recoverable_overdue_notice(self):
        assert self.configure().status_code == 200
        assert scan_allocations()['allocated'] == 1
        pool = TaskPackagePool.objects.get(package_id=self.package_id)
        assert pool.allocation_id is not None
        assert (
            self.configure(expected_revision=1, due_at=(timezone.now() - timedelta(seconds=1)).isoformat()).status_code
            == 200
        )
        with patch('task_packages.automation.call_command', side_effect=RuntimeError('failure')):
            with self.assertRaises(CommandError):
                call_command('run_package_automation', once=True, stdout=StringIO())
        assert not TaskPackageOverdueNotice.objects.exists()
        call_command('run_package_automation', once=True, stdout=StringIO())
        call_command('run_package_automation', once=True, stdout=StringIO())
        notice = TaskPackageOverdueNotice.objects.get()
        assert notice.dispatch_id_snapshot == pool.allocation_id
        assert notice.recipient_id == pool.allocation.assignee_id and notice.overdue_count == 3

    def test_priority_order_and_cursor_are_stable(self):
        assert self.configure(priority=1).status_code == 200
        task = Task.objects.create(project=self.project, data={})
        second = self.create(task_ids=[task.pk])
        second_id = second.json()['id']
        self.url_pool = f'{self.url}{second_id}/pool/'
        assert self.configure(priority=9).status_code == 200
        first = scan_allocations(limit=1)
        assert first['allocated'] == 1 and first['next'] is not None
        assert TaskAssignment.objects.get(task=task).project_id == self.project.pk
        assert scan_allocations(first['next'], limit=1)['allocated'] == 1

    def test_reopen_turns_off_automation_for_new_claim_generation(self):
        assert self.configure(automatic=False).status_code == 200
        client = APIClient()
        client.force_authenticate(self.annotator)
        assert (
            client.post(
                f'{self.url}{self.package_id}/claim/', {}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
            ).status_code
            == 201
        )
        assert self.configure(expected_revision=1, automatic=True).status_code == 200
        pool = TaskPackagePool.objects.get(package_id=self.package_id)
        for assignment in TaskAssignment.objects.all():
            assignment.cancel()
        response = self.client.post(
            f'{self.url}{self.package_id}/reopen/',
            {'expected_revision': pool.revision, 'claim_id': pool.claim.pk},
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        )
        assert response.status_code == 200, response.content
        pool.refresh_from_db()
        assert not pool.automatic and pool.automation_actor_id is None
        assert scan_allocations()['allocated'] == 0
