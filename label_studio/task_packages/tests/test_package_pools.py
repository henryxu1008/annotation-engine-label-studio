from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.core.management import call_command
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from projects.models import ProjectMember
from rest_framework.test import APIClient, APITestCase
from task_packages.models import (
    TaskPackage,
    TaskPackageClaim,
    TaskPackageClaimHistory,
    TaskPackageOverdueNotice,
    TaskPackagePool,
)
from task_packages.tests.test_packages import PackageFixture
from tasks.models import Task, TaskAssignment


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
    ANNOTATION_PACKAGE_DISPATCH_ENABLED=True,
    ANNOTATION_PACKAGE_ADVANCED_ENABLED=True,
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

    def test_advanced_operations_are_disabled_until_all_workers_are_upgraded(self):
        assert self.configure().status_code == 200
        with override_settings(ANNOTATION_PACKAGE_ADVANCED_ENABLED=False):
            url = f'{self.url}{self.package_id}/balance/'
            assert self.client.post(url, {}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())).status_code == 404
            assert self.configure(expected_revision=1, priority=4).status_code == 404
        assert not TaskAssignment.objects.exists()

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
        with self.assertRaisesRegex(RuntimeError, 'pool configuration'):
            MigrationExecutor(connection).migrate([('task_packages', '0002_taskpackagedispatch')])
        assert TaskPackagePool.objects.count() == 1

    def test_reopen_requires_cancelled_original_and_replays_history_safely(self):
        assert self.configure().status_code == 200
        claim_key = uuid4()
        assert self.claim(key=claim_key).status_code == 201
        pool = TaskPackagePool.objects.get(package_id=self.package_id)
        original_claim_id = pool.claim.pk
        url = f'{self.url}{self.package_id}/reopen/'
        key = uuid4()
        body = {'expected_revision': pool.revision, 'claim_id': original_claim_id}
        assert self.client.post(url, body, format='json', HTTP_IDEMPOTENCY_KEY=str(key)).status_code == 409
        for item in TaskAssignment.objects.all():
            item.cancel()
        first = self.client.post(url, body, format='json', HTTP_IDEMPOTENCY_KEY=str(key))
        assert first.status_code == 200, first.content
        assert self.client.post(url, body, format='json', HTTP_IDEMPOTENCY_KEY=str(key)).json()['replayed']
        new_claim = self.claim(self.other)
        assert new_claim.status_code == 201, new_claim.content
        old = self.claim(key=claim_key)
        assert old.status_code == 200 and old.json()['released'] is True
        replay = self.client.post(url, body, format='json', HTTP_IDEMPOTENCY_KEY=str(key))
        assert replay.status_code == 200 and TaskPackageClaim.objects.get().actor_id_snapshot == self.other.pk

    def test_reopen_rolls_back_history_on_failure(self):
        assert self.configure().status_code == 200
        assert self.claim().status_code == 201
        pool = TaskPackagePool.objects.get(package_id=self.package_id)
        for item in TaskAssignment.objects.all():
            item.cancel()
        with patch('task_packages.pools.TaskPackageClaim.delete', side_effect=RuntimeError('failure')):
            response = self.client.post(
                f'{self.url}{self.package_id}/reopen/',
                {'expected_revision': pool.revision, 'claim_id': pool.claim.pk},
                format='json',
                HTTP_IDEMPOTENCY_KEY=str(uuid4()),
            )
        assert response.status_code == 500
        assert TaskPackageClaim.objects.count() == 1 and not pool.claim_history.exists()

    def test_reopen_recovers_after_database_connection_is_killed(self):
        assert self.configure().status_code == 200
        assert self.claim().status_code == 201
        pool = TaskPackagePool.objects.get(package_id=self.package_id)
        for item in TaskAssignment.objects.all():
            item.cancel()
        body = {'expected_revision': pool.revision, 'claim_id': pool.claim.pk}
        key = uuid4()
        ready, resume = Event(), Event()
        pids = []
        create_history = TaskPackageClaimHistory.objects.create

        def pause_after_history(**kwargs):
            history = create_history(**kwargs)
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_backend_pid()')
                pids.append(cursor.fetchone()[0])
            ready.set()
            assert resume.wait(20)
            return history

        def worker():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(self.manager)
                try:
                    return client.post(
                        f'{self.url}{self.package_id}/reopen/', body, format='json', HTTP_IDEMPOTENCY_KEY=str(key)
                    ).status_code
                except DatabaseError:
                    return 500
            finally:
                close_old_connections()

        with patch('task_packages.pools.TaskPackageClaimHistory.objects.create', side_effect=pause_after_history):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(worker)
                try:
                    assert ready.wait(20)
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT pg_terminate_backend(%s)', [pids[0]])
                        assert cursor.fetchone()[0] is True
                finally:
                    resume.set()
                assert future.result(timeout=30) == 500
        assert TaskPackageClaim.objects.count() == 1 and not pool.claim_history.exists()
        retry = self.client.post(
            f'{self.url}{self.package_id}/reopen/', body, format='json', HTTP_IDEMPOTENCY_KEY=str(key)
        )
        assert retry.status_code == 200

    def test_balanced_assignment_locks_all_candidates_with_actor(self):
        from task_packages.pools import lock_project_operation

        assert self.configure().status_code == 200
        with patch('task_packages.pools.lock_project_operation', wraps=lock_project_operation) as fence:
            response = self.client.post(
                f'{self.url}{self.package_id}/balance/', {}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
            )
        assert response.status_code == 201, response.content
        assert fence.call_count == 1
        assert fence.call_args.kwargs['related_user_ids'] == sorted([self.annotator.pk, self.other.pk])

    def test_balancers_in_different_projects_lock_crossed_identities_without_deadlock(self):
        from projects.tests.factories import ProjectFactory
        from task_packages.pools import lock_project_operation

        assert self.configure(candidate_ids=[self.other.pk]).status_code == 200
        other_project = ProjectFactory(organization=self.organization, created_by=self.other)
        ProjectMember.objects.create(project=other_project, user=self.manager, role='annotator')
        other_task = Task.objects.create(project=other_project, data={})
        client = APIClient()
        client.force_authenticate(self.other)
        other_url = f'/api/projects/{other_project.pk}/task-packages/'
        package = client.post(
            other_url,
            {'name': 'Other package', 'task_ids': [other_task.pk]},
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        )
        assert package.status_code == 201, package.content
        other_id = package.json()['id']
        configured = client.put(
            f'{other_url}{other_id}/pool/',
            {'expected_revision': 0, 'candidate_ids': [self.manager.pk], 'enabled': True, 'due_at': None},
            format='json',
        )
        assert configured.status_code == 200, configured.content
        barrier = Barrier(2)

        def synchronized_lock(*args, **kwargs):
            # On the old implementation each worker first held its own actor
            # before meeting here, forcing the cross-project deadlock.
            if not kwargs.get('related_user_ids'):
                result = lock_project_operation(*args, **kwargs)
                barrier.wait(timeout=10)
                return result
            barrier.wait(timeout=10)
            return lock_project_operation(*args, **kwargs)

        def allocate(actor, url):
            close_old_connections()
            try:
                worker = APIClient()
                worker.force_authenticate(actor)
                return worker.post(url, {}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())).status_code
            finally:
                close_old_connections()

        with patch('task_packages.pools.lock_project_operation', side_effect=synchronized_lock):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(allocate, self.manager, f'{self.url}{self.package_id}/balance/')
                second = executor.submit(allocate, self.other, f'{other_url}{other_id}/balance/')
                assert [first.result(timeout=30), second.result(timeout=30)] == [201, 201]

    def test_claim_rechecks_blind_scope_after_waiting_for_project_lock(self):
        from classification_disagreements.models import BlindQualityAssignment
        from projects.models import Project
        from task_packages.pools import lock_project_operation
        from users.models import User

        assert self.configure().status_code == 200
        started = Event()

        def observed_lock(*args, **kwargs):
            started.set()
            return lock_project_operation(*args, **kwargs)

        def claim():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=self.annotator.pk))
                return client.post(self.claim_url, {}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())).status_code
            finally:
                close_old_connections()

        with patch('task_packages.pools.lock_project_operation', side_effect=observed_lock):
            with ThreadPoolExecutor(max_workers=1) as pool:
                with transaction.atomic():
                    Project.objects.select_for_update().get(pk=self.project.pk)
                    reading = pool.submit(claim)
                    assert started.wait(10)
                    marker_task = Task.objects.create(project=self.project, data={'text': 'private sample'})
                    assignment = self.assignment(task=marker_task, user=self.other)
                    BlindQualityAssignment.objects.create(
                        assignment_id=assignment.pk, task_id=marker_task.pk,
                        project_id=self.project.pk, assignee_id=self.other.pk,
                    )
                assert reading.result(timeout=20) == 404
        assert not TaskPackageClaim.objects.exists()
        assert not TaskAssignment.objects.filter(task_id__in=[task.pk for task in self.tasks]).exists()

    def test_balancing_is_unavailable_for_blind_projects_without_creating_work(self):
        assert self.configure().status_code == 200
        url = f'{self.url}{self.package_id}/balance/'
        key = uuid4()
        with self.settings(
            ANNOTATION_CLASSIFICATION_BLIND_ENABLED=True,
            ANNOTATION_CLASSIFICATION_BLIND_PROJECTS={f'{self.organization.pk}:{self.project.pk}': True},
        ):
            denied = self.client.post(url, {}, format='json', HTTP_IDEMPOTENCY_KEY=str(key))
            assert denied.status_code == 409, denied.content
            assert not TaskAssignment.objects.exists()
            assert TaskPackagePool.objects.get(package_id=self.package_id).allocation_id is None
        ordinary = self.client.post(url, {}, format='json', HTTP_IDEMPOTENCY_KEY=str(key))
        assert ordinary.status_code == 201, ordinary.content

    def test_balanced_assignment_uses_current_load_and_is_idempotent(self):
        assert self.configure().status_code == 200
        extra_task = Task.objects.create(project=self.project, data={})
        self.assignment(task=extra_task, user=self.annotator)
        key = uuid4()
        url = f'{self.url}{self.package_id}/balance/'
        first = self.client.post(url, {}, format='json', HTTP_IDEMPOTENCY_KEY=str(key))
        assert first.status_code == 201, first.content
        assert first.json()['assignee_id'] == self.other.pk
        second = self.client.post(url, {}, format='json', HTTP_IDEMPOTENCY_KEY=str(key))
        assert second.status_code == 200 and second.json()['replayed']
        assert TaskAssignment.objects.count() == 4
        self.client.force_authenticate(self.annotator)
        assert self.client.get(f'{self.url}claim-pools/').json()['results'] == []

    def test_discovery_prioritizes_higher_priority_and_old_clients_preserve_it(self):
        assert self.configure(priority=9).status_code == 200
        assert self.configure(expected_revision=1).status_code == 200
        assert TaskPackagePool.objects.get(package_id=self.package_id).priority == 9
        second = self.create(task_ids=[self.tasks[0].pk])
        other_id = second.json()['id']
        self.pool_url = f'{self.url}{other_id}/pool/'
        assert self.configure(priority=30).status_code == 200
        self.client.force_authenticate(self.annotator)
        rows = self.client.get(f'{self.url}claim-pools/').json()['results']
        assert [row['priority'] for row in rows] == [30, 9]

    def test_overdue_notice_is_durable_scoped_and_reconciled_idempotently(self):
        assert self.configure(due_at=None).status_code == 200
        assert self.claim().status_code == 201
        assert (
            self.configure(expected_revision=1, due_at=(timezone.now() - timedelta(seconds=1)).isoformat()).status_code
            == 200
        )
        url = f'{self.url}overdue-notices/'
        self.client.force_authenticate(self.other)
        assert self.client.post(url, {}, format='json').json()['results'] == []
        self.client.force_authenticate(self.annotator)
        first = self.client.post(url, {}, format='json')
        assert first.status_code == 200, first.content
        assert first.json()['results'][0]['overdue_count'] == 3
        assert self.client.post(url, {}, format='json').json()['results'][0]['id'] == first.json()['results'][0]['id']
        assert TaskPackageOverdueNotice.objects.count() == 1
        ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=False)
        assert self.client.post(url, {}, format='json').status_code in (403, 404)
        assert TaskPackageOverdueNotice.objects.count() == 1

    def test_balanced_allocation_deadline_notice_and_stale_receipt(self):
        assert self.configure(due_at=None).status_code == 200
        key = uuid4()
        balance_url = f'{self.url}{self.package_id}/balance/'
        result = self.client.post(balance_url, {}, format='json', HTTP_IDEMPOTENCY_KEY=str(key))
        assert result.status_code == 201, result.content
        recipient = result.json()['assignee_id']
        other = self.other if recipient == self.annotator.pk else self.annotator
        allocated = self.annotator if recipient == self.annotator.pk else self.other
        pool = TaskPackagePool.objects.get(package_id=self.package_id)
        assert pool.allocation_id == result.json()['dispatch_id']
        assert (
            self.configure(expected_revision=1, due_at=(timezone.now() - timedelta(seconds=1)).isoformat()).status_code
            == 200
        )
        TaskAssignment.objects.get(pk=result.json()['assignment_ids'][0]).cancel()
        assert self.client.get(self.pool_url).json()['overdue_count'] == 2
        self.client.force_authenticate(other)
        assert self.client.post(f'{self.url}overdue-notices/', {}, format='json').json()['results'] == []
        self.client.force_authenticate(allocated)
        visible = self.client.get(f'{self.url}claim-pools/').json()['results']
        assert len(visible) == 1 and visible[0]['allocated'] and visible[0]['overdue_count'] == 2
        notices = self.client.post(f'{self.url}overdue-notices/', {}, format='json').json()['results']
        assert len(notices) == 1 and notices[0]['overdue_count'] == 2
        call_command('reconcile_package_overdue', limit=1, stdout=StringIO())
        notice = TaskPackageOverdueNotice.objects.get()
        assert notice.dispatch_id_snapshot == pool.allocation_id and notice.claim_id_snapshot is None
        for assignment in TaskAssignment.objects.filter(status__in=TaskAssignment.ACTIVE_STATUSES):
            assignment.cancel()
        self.client.force_authenticate(self.manager)
        assert self.configure(expected_revision=2, due_at=None).status_code == 200
        assert self.claim(user=other).status_code == 201
        assert self.client.post(balance_url, {}, format='json', HTTP_IDEMPOTENCY_KEY=str(key)).status_code == 200
        pool.refresh_from_db()
        assert pool.allocation_id is None and pool.claim.actor_id == other.pk
        self.client.force_authenticate(allocated)
        assert self.client.post(f'{self.url}overdue-notices/', {}, format='json').json()['results'] == []

    def test_notice_failure_rolls_back_without_losing_existing_claim(self):
        assert self.configure(due_at=None).status_code == 200
        assert self.claim().status_code == 201
        assert (
            self.configure(expected_revision=1, due_at=(timezone.now() - timedelta(seconds=1)).isoformat()).status_code
            == 200
        )
        self.client.force_authenticate(self.annotator)
        with patch(
            'task_packages.api.TaskPackageOverdueNotice.objects.get_or_create', side_effect=RuntimeError('failure')
        ):
            assert self.client.post(f'{self.url}overdue-notices/', {}, format='json').status_code == 500
        assert not TaskPackageOverdueNotice.objects.exists() and TaskPackageClaim.objects.count() == 1

    def test_concurrent_allocation_notice_is_created_once(self):
        assert self.configure(due_at=None).status_code == 200
        response = self.client.post(
            f'{self.url}{self.package_id}/balance/', {}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
        )
        assert response.status_code == 201
        recipient = self.annotator if response.json()['assignee_id'] == self.annotator.pk else self.other
        assert (
            self.configure(expected_revision=1, due_at=(timezone.now() - timedelta(seconds=1)).isoformat()).status_code
            == 200
        )
        barrier = Barrier(2)

        def reconcile(_):
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(recipient)
                barrier.wait(timeout=10)
                result = client.post(f'{self.url}overdue-notices/', {}, format='json')
                return result.status_code, result.json()['results'][0]['id']
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reconcile, range(2)))
        assert results[0] == results[1] and results[0][0] == 200
        assert TaskPackageOverdueNotice.objects.count() == 1

    def test_nonempty_allocation_migration_refuses_rollback(self):
        assert self.configure(due_at=None).status_code == 200
        response = self.client.post(
            f'{self.url}{self.package_id}/balance/', {}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
        )
        assert response.status_code == 201
        with self.assertRaisesRegex(RuntimeError, 'pool configuration or notices'):
            MigrationExecutor(connection).migrate([('task_packages', '0006_pool_automation')])
        assert TaskPackagePool.objects.get(package_id=self.package_id).allocation_id == response.json()['dispatch_id']

    def test_ambiguous_legacy_pool_dispatch_blocks_upgrade_without_deleting_receipt(self):
        old_target = [('task_packages', '0006_pool_automation')]
        MigrationExecutor(connection).migrate(old_target)
        old_apps = MigrationExecutor(connection).loader.project_state(old_target).apps
        old_pool = old_apps.get_model('task_packages', 'TaskPackagePool')
        old_dispatch = old_apps.get_model('task_packages', 'TaskPackageDispatch')
        try:
            old_pool.objects.create(package_id=self.package_id)
            receipt = old_dispatch.objects.create(
                project_id=self.project.pk,
                package_id=self.package_id,
                assignee_id=self.annotator.pk,
                created_by_id=self.manager.pk,
                assignee_id_snapshot=self.annotator.pk,
                actor_id_snapshot=self.manager.pk,
                creator_principal=f'label_studio_local:{self.manager.pk}',
                idempotency_key=uuid4(),
                request_hash='a' * 64,
                assignment_ids=[],
            )
            with self.assertRaisesRegex(RuntimeError, 'legacy package pool dispatch receipts'):
                MigrationExecutor(connection).migrate([('task_packages', '0007_pool_allocation_notices')])
            assert old_dispatch.objects.filter(pk=receipt.pk).exists()
        finally:
            old_dispatch.objects.filter(package_id=self.package_id).delete()
            old_pool.objects.filter(package_id=self.package_id).delete()
            MigrationExecutor(connection).migrate([('task_packages', '0007_pool_allocation_notices')])

    def test_package_cascade_deletes_pool_allocation(self):
        assert self.configure(due_at=None).status_code == 200
        response = self.client.post(
            f'{self.url}{self.package_id}/balance/', {}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
        )
        assert response.status_code == 201
        TaskPackage.objects.get(pk=self.package_id).delete()
        assert not TaskPackagePool.objects.filter(package_id=self.package_id).exists()

    def test_project_cascade_deletes_pool_allocation(self):
        assert self.configure(due_at=None).status_code == 200
        response = self.client.post(
            f'{self.url}{self.package_id}/balance/', {}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
        )
        assert response.status_code == 201
        self.project.delete()
        assert not TaskPackagePool.objects.filter(package_id=self.package_id).exists()

    def test_scheduled_reconciliation_is_idempotent_and_skips_revoked_users(self):
        assert self.configure(due_at=None).status_code == 200
        assert self.claim().status_code == 201
        assert (
            self.configure(expected_revision=1, due_at=(timezone.now() - timedelta(seconds=1)).isoformat()).status_code
            == 200
        )
        out = StringIO()
        call_command('reconcile_package_overdue', limit=1, stdout=out)
        assert out.getvalue().strip() == 'next_after='
        assert TaskPackageOverdueNotice.objects.count() == 1
        call_command('reconcile_package_overdue', limit=1, stdout=StringIO())
        assert TaskPackageOverdueNotice.objects.count() == 1
        ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=False)
        call_command('reconcile_package_overdue', limit=1, stdout=StringIO())
        assert TaskPackageOverdueNotice.objects.count() == 1

    def test_concurrent_reopen_only_archives_once(self):
        assert self.configure().status_code == 200
        assert self.claim().status_code == 201
        pool = TaskPackagePool.objects.get(package_id=self.package_id)
        for item in TaskAssignment.objects.all():
            item.cancel()
        body = {'expected_revision': pool.revision, 'claim_id': pool.claim.pk}
        barrier = Barrier(2)

        def worker(_):
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(self.manager)
                barrier.wait(timeout=10)
                return client.post(
                    f'{self.url}{self.package_id}/reopen/', body, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
                ).status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(worker, range(2)))
        assert sorted(results) == [200, 409], results
        assert pool.claim_history.count() == 1 and not TaskPackageClaim.objects.exists()

    def test_concurrent_balancers_do_not_duplicate_work(self):
        assert self.configure().status_code == 200
        barrier = Barrier(2)

        def worker(_):
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(self.manager)
                barrier.wait(timeout=10)
                return client.post(
                    f'{self.url}{self.package_id}/balance/', {}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
                ).status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(worker, range(2)))
        assert sorted(results) == [201, 409], results
        assert TaskAssignment.objects.count() == 3
