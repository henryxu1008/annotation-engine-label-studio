from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import patch
from uuid import uuid4

from access_control.identity import Principal
from access_control.project_operations import (
    EDIT,
    ORGANIZE_WORK,
    READ_WORK,
    REVIEW,
    ProjectOperationPolicy,
    local_principal,
    lock_project_operation,
)
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from organizations.models import OrganizationMember
from organizations.tests.factories import OrganizationFactory
from projects.models import ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.test import APIClient, APITestCase
from task_packages import api as package_api
from task_packages import progress as package_progress
from task_packages.models import TaskPackage, TaskPackageItem
from tasks.models import AnnotationDraft, Submission, Task, TaskAssignment
from tasks.tests.factories import TaskFactory
from users.tests.factories import UserFactory


class ReadOnlyPolicy:
    state = 'paused'

    def evaluate(self, project):
        return ProjectOperationPolicy('lifecycle-fixture-v1', self.state, frozenset({READ_WORK}), 'PROJECT_READ_ONLY')


class ChangedProjectPolicy:
    """Test-only stand-in for #86 reading persisted project state, not a state machine."""

    def evaluate(self, project):
        if project.description == 'deny-reads':
            return ProjectOperationPolicy('fresh-project-v1', 'paused', frozenset(), 'PROJECT_READ_ONLY')
        if project.description == 'read-only':
            return ProjectOperationPolicy('fresh-project-v1', 'paused', frozenset({READ_WORK}), 'PROJECT_READ_ONLY')
        return ProjectOperationPolicy(
            'fresh-project-v1', 'legacy', frozenset({READ_WORK, EDIT, REVIEW, ORGANIZE_WORK})
        )


class PackageFixture:
    def setUp(self):
        super().setUp()
        self.organization = OrganizationFactory()
        self.project = ProjectFactory(organization=self.organization)
        self.manager = self.organization.created_by
        self.annotator = UserFactory(active_organization=self.organization)
        self.other = UserFactory(active_organization=self.organization)
        self.reviewer = UserFactory(active_organization=self.organization)
        for user, role in [
            (self.annotator, 'annotator'),
            (self.other, 'annotator'),
            (self.reviewer, 'reviewer'),
        ]:
            ProjectMember.objects.create(project=self.project, user=user, role=role)
        self.tasks = [TaskFactory(project=self.project, data={'text': 'PRIVATE task body'}) for _ in range(3)]
        self.url = f'/api/projects/{self.project.pk}/task-packages/'
        self.client = APIClient()
        self.client.force_authenticate(self.manager)

    def create(self, **changes):
        data = {
            'name': '固定包 A',
            'instructions': '按建议规则 v0.1',
            'source_note': 'manager private source',
            'task_ids': [task.pk for task in self.tasks],
        }
        data.update(changes)
        return self.client.post(self.url, data, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))

    def detail(self, package_id):
        return self.client.get(f'{self.url}{package_id}/')

    def assignment(self, task=None, user=None, **kwargs):
        return TaskAssignment.objects.create(
            project=self.project,
            task=task or self.tasks[0],
            assignee=user or self.annotator,
            assigned_by=self.manager,
            **kwargs,
        )

    def submission(self, assignment, status='pending', revision=1):
        return Submission.objects.create(
            assignment=assignment,
            annotation=assignment.annotation,
            revision=revision,
            status=status,
            submitted_by=assignment.assignee,
            result_hash='hash',
            result_snapshot={'annotation': {'result': [{'secret': 'PRIVATE answer'}]}},
        )


@override_settings(ANNOTATION_FIXED_PACKAGES_ENABLED=True)
class TestPackages(PackageFixture, APITestCase):
    def test_thousand_task_package_is_paged_without_dispatch_or_silent_deduplication(self):
        additional = Task.objects.bulk_create(
            [Task(project=self.project, data={'text': 'synthetic'}) for _ in range(997)]
        )
        ids = [task.pk for task in [*self.tasks, *additional]]
        response = self.create(task_ids=ids)
        assert response.status_code == 201, response.content
        if connection.vendor == 'postgresql':
            # Repeated membership semijoins can make this small summary scan
            # a thousand-item scope for minutes under a different query plan.
            # Bound actual SQL execution, rather than asserting a SQL shape.
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '10s'")
        page = self.client.get(f'{self.url}{response.json()["id"]}/', {'page_size': 100}).json()
        assert page['count'] == page['counts']['fixed_tasks'] == page['counts']['unassigned_tasks'] == 1000
        assert len(page['results']) == 100 and page['next'] is not None
        # Packages are non-exclusive groupings; repeating membership never assigns work.
        assert self.create(task_ids=ids).status_code == 201
        assert TaskPackageItem.objects.count() == 2000 and TaskAssignment.objects.count() == 0

    def test_fixed_membership_idempotency_and_no_assignment_writes(self):
        key = str(uuid4())
        payload = {'name': '固定包', 'task_ids': [task.pk for task in self.tasks]}
        first = self.client.post(self.url, payload, format='json', HTTP_IDEMPOTENCY_KEY=key)
        second = self.client.post(
            self.url,
            {**payload, 'task_ids': list(reversed(payload['task_ids']))},
            format='json',
            HTTP_IDEMPOTENCY_KEY=key,
        )
        assert first.status_code == 201, first.content
        assert second.status_code == 200 and second.json()['id'] == first.json()['id']
        assert second.json()['replayed'] is True
        changed = self.client.post(
            self.url,
            {**payload, 'name': 'changed'},
            format='json',
            HTTP_IDEMPOTENCY_KEY=key,
        )
        assert changed.status_code == 409
        assert TaskPackage.objects.count() == 1 and TaskPackageItem.objects.count() == 3
        assert TaskAssignment.objects.count() == Submission.objects.count() == 0
        for method in ('patch', 'delete', 'put'):
            assert (
                getattr(self.client, method)(f"{self.url}{first.json()['id']}/", {}, format='json').status_code == 405
            )

    def test_invalid_selection_is_atomic_and_never_creates_assignments(self):
        foreign = TaskFactory()
        for ids in (
            [],
            [self.tasks[0].pk] * 2,
            [self.tasks[0].pk, foreign.pk],
            [99999999],
            list(range(1, 1002)),
        ):
            response = self.create(task_ids=ids)
            assert response.status_code == 400, response.content
        for extra in (
            'actor',
            'assignee',
            'reviewer',
            'state',
            'priority',
            'task_filter',
        ):
            assert self.create(**{extra: self.other.pk}).status_code == 400
        assert TaskPackage.objects.count() == TaskAssignment.objects.count() == 0

    def test_key_required_and_scoped_to_creator(self):
        payload = {'name': 'test', 'task_ids': [self.tasks[0].pk]}
        for key in ('', 'bad-key'):
            assert self.client.post(self.url, payload, format='json', HTTP_IDEMPOTENCY_KEY=key).status_code == 400
        key = str(uuid4())
        assert self.client.post(self.url, payload, format='json', HTTP_IDEMPOTENCY_KEY=key).status_code == 201
        ProjectMember.objects.filter(project=self.project, user=self.other).update(role='manager')
        self.client.force_authenticate(self.other)
        assert self.client.post(self.url, payload, format='json', HTTP_IDEMPOTENCY_KEY=key).status_code == 409

    def test_manager_counts_samples_separately_and_reuses_assignee_draft_state(self):
        package_id = self.create().json()['id']
        first = self.assignment()
        self.submission(first, status='approved')
        AnnotationDraft.objects.create(
            task=first.task,
            assignment=first,
            user=first.assignee,
            result=[{'changed': True}],
        )
        second = self.assignment(user=self.other)
        self.submission(second)
        cancelled = self.assignment(task=self.tasks[1], status='cancelled')
        self.submission(cancelled)
        data = self.detail(package_id).json()
        assert data['counts'] == {
            'pending_review': 2,
            'assigned': 0,
            'in_progress': 1,
            'rework': 0,
            'submitted': 1,
            'approved': 0,
            'conflict': 0,
            'assignments': 2,
            'fixed_tasks': 3,
            'available_tasks': 3,
            'unavailable_tasks': 0,
            'unassigned_tasks': 2,
            'blocked_assignments': 0,
        }
        assert data['results'][0]['states'] == {'in_progress': 1, 'submitted': 1}
        self.other.is_active = False
        administrator = UserFactory(is_staff=True, is_superuser=True, active_organization=self.organization)
        self.other.save(update_fields=['is_active'], session_actor=administrator)
        assert self.detail(package_id).json()['counts']['blocked_assignments'] == 1
        assert b'PRIVATE' not in self.detail(package_id).content

    def test_deleted_or_moved_tasks_remain_in_fixed_denominator(self):
        package_id = self.create().json()['id']
        deleted_id = self.tasks[0].pk
        self.tasks[0].delete()
        other_project = ProjectFactory(organization=self.organization)
        type(self.tasks[1]).objects.filter(pk=self.tasks[1].pk).update(project=other_project)
        data = self.detail(package_id).json()
        assert data['counts']['fixed_tasks'] == 3 and data['counts']['unavailable_tasks'] == 2
        assert data['results'][0] == {
            'task_id': deleted_id,
            'available': False,
            'states': {},
        }

    def test_annotator_sees_only_own_active_work_without_global_denominator(self):
        package_id = self.create().json()['id']
        own = self.assignment()
        self.assignment(task=self.tasks[1], user=self.other)
        self.client.force_authenticate(self.annotator)
        data = self.detail(package_id).json()
        assert [row['task_id'] for row in data['results']] == [own.task_id]
        assert 'fixed_tasks' not in data['counts'] and 'membership_hash' not in data['package']
        assert 'source_note' not in data['package'] and b'PRIVATE' not in self.detail(package_id).content
        assert data['results'][0]['editor_url'] == (
            f'/projects/{self.project.pk}/data?task={own.task_id}&task_package={package_id}'
        )
        assert self.create().status_code == 403
        TaskAssignment.objects.filter(pk=own.pk).update(status='cancelled')
        assert self.client.get(self.url).json()['count'] == 0
        assert self.detail(package_id).status_code == 404

    def test_reviewer_nonself_pending_and_cancelled_source_without_editor(self):
        package_id = self.create().json()['id']
        retained = self.submission(self.assignment(status='cancelled'))
        self.submission(self.assignment(task=self.tasks[1]), status='approved')
        self.submission(self.assignment(task=self.tasks[2], user=self.reviewer))
        self.client.force_authenticate(self.reviewer)
        data = self.detail(package_id).json()
        assert data['counts'] == {'pending_review': 1}
        assert data['results'] == [
            {
                'task_id': retained.assignment.task_id,
                'submission_id': retained.pk,
                'revision': 1,
                'state': 'pending',
                'source_cancelled': True,
            }
        ]
        assert data['can_review'] is True and b'PRIVATE' not in self.detail(package_id).content
        assert self.create().status_code == 403
        Submission.objects.filter(pk=retained.pk).update(status='approved')
        assert self.detail(package_id).status_code == 404

    def test_cross_project_and_unsupported_filters_rejected(self):
        package_id = self.create().json()['id']
        project = ProjectFactory(organization=self.organization)
        assert self.client.get(f'/api/projects/{project.pk}/task-packages/{package_id}/').status_code == 404
        for url in (self.url, f'{self.url}{package_id}/'):
            for params in (
                {'assignee': self.other.pk},
                {'project': project.pk},
                {'state': 'unsupported'},
            ):
                assert self.client.get(url, params).status_code == 400
        assert self.client.post(f'{self.url}{package_id}/dispatch/', {}, format='json').status_code == 404
        assert self.client.get(self.url, {'state': 'approved'}).status_code == 400

    def test_revocation_is_live_for_list_detail_and_create_and_no_store(self):
        package_id = self.create().json()['id']
        self.assignment()
        self.client.force_authenticate(self.annotator)
        assert self.detail(package_id).status_code == 200
        ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=False)
        for response in (
            self.client.get(self.url),
            self.detail(package_id),
            self.create(),
        ):
            assert response.status_code in (403, 404) and response['Cache-Control'] == 'private, no-store'
        self.client.force_authenticate(self.manager)
        OrganizationMember.objects.filter(organization=self.organization, user=self.manager).update(
            deleted_at='2026-10-22T00:00Z'
        )
        assert self.detail(package_id).status_code in (403, 404)
        assert self.create().status_code in (403, 404)

    def test_mapped_actor_never_borrows_session_manager(self):
        package_id = self.create().json()['id']
        own = self.assignment()
        principal = Principal(f'test:{self.annotator.pk}', 'test', 'mapped', self.annotator.pk)
        with patch('access_control.identity.get_identity_provider') as provider:
            provider.return_value.resolve.return_value = principal
            assert self.detail(package_id).json()['results'][0]['assignment_id'] == own.pk
            assert self.create().status_code == 403

    def test_manager_list_rechecks_role_before_serializing_private_metadata(self):
        self.create()
        ProjectMember.objects.filter(project=self.project, user=self.other).update(role='manager')
        self.client.force_authenticate(self.other)
        original = package_api.package_metadata

        def demote_before_serializing(package, role):
            ProjectMember.objects.filter(project=self.project, user=self.other).update(role='annotator')
            return original(package, role)

        with patch('task_packages.api.package_metadata', side_effect=demote_before_serializing):
            response = self.client.get(self.url)
        assert response.status_code in (403, 404), response.json()
        assert b'source_note' not in response.content and b'membership_hash' not in response.content

    def test_list_rechecks_package_scope_after_last_own_assignment_is_cancelled(self):
        self.create()
        assignment = self.assignment()
        self.client.force_authenticate(self.annotator)
        original = package_api.package_metadata

        def cancel_before_serializing(package, role):
            TaskAssignment.objects.filter(pk=assignment.pk).update(status='cancelled')
            return original(package, role)

        with patch('task_packages.api.package_metadata', side_effect=cancel_before_serializing):
            response = self.client.get(self.url)
        assert response.status_code in (403, 404), response.json()
        assert '固定包 A' not in response.content.decode()

    def test_detail_rechecks_package_scope_after_last_own_assignment_is_cancelled(self):
        package_id = self.create().json()['id']
        assignment = self.assignment()
        self.client.force_authenticate(self.annotator)
        original = package_api.current_read_policy

        def cancel_before_final_check(*args, **kwargs):
            TaskAssignment.objects.filter(pk=assignment.pk).update(status='cancelled')
            return original(*args, **kwargs)

        with patch('task_packages.api.current_read_policy', side_effect=cancel_before_final_check):
            response = self.detail(package_id)
        assert response.status_code in (403, 404), response.json()
        assert 'package' not in response.json()

    def test_detail_rechecks_package_scope_after_last_reviewable_submission_leaves_pending(self):
        package_id = self.create().json()['id']
        submission = self.submission(self.assignment())
        self.client.force_authenticate(self.reviewer)
        original = package_api.current_read_policy

        def approve_before_final_check(*args, **kwargs):
            Submission.objects.filter(pk=submission.pk).update(status='approved')
            return original(*args, **kwargs)

        with patch('task_packages.api.current_read_policy', side_effect=approve_before_final_check):
            response = self.detail(package_id)
        assert response.status_code in (403, 404), response.json()
        assert 'package' not in response.json()

    def test_detail_rejects_partial_assignment_revocation_even_when_package_remains_visible(self):
        package_id = self.create().json()['id']
        revoked = self.assignment()
        retained = self.assignment(task=self.tasks[1])
        self.client.force_authenticate(self.annotator)
        original = package_api.current_read_policy

        def cancel_before_final_check(*args, **kwargs):
            TaskAssignment.objects.filter(pk=revoked.pk).update(status='cancelled')
            return original(*args, **kwargs)

        with patch('task_packages.api.current_read_policy', side_effect=cancel_before_final_check):
            response = self.detail(package_id)
        assert response.status_code == 403, response.content
        assert response.json()['display_context']['reason'] == 'PACKAGE_ACCESS_CHANGED'
        assert 'results' not in response.json() and 'counts' not in response.json()
        fresh = self.detail(package_id)
        assert fresh.status_code == 200, fresh.content
        assert [row['assignment_id'] for row in fresh.json()['results']] == [retained.pk]
        assert fresh.json()['counts']['assignments'] == 1

    def test_detail_rejects_partial_review_departure_even_when_package_remains_visible(self):
        package_id = self.create().json()['id']
        departed = self.submission(self.assignment())
        retained = self.submission(self.assignment(task=self.tasks[1]))
        self.client.force_authenticate(self.reviewer)
        original = package_api.current_read_policy

        def approve_before_final_check(*args, **kwargs):
            Submission.objects.filter(pk=departed.pk).update(status='approved')
            return original(*args, **kwargs)

        with patch('task_packages.api.current_read_policy', side_effect=approve_before_final_check):
            response = self.detail(package_id)
        assert response.status_code == 403, response.content
        assert response.json()['display_context']['reason'] == 'PACKAGE_ACCESS_CHANGED'
        assert 'results' not in response.json() and 'counts' not in response.json()
        fresh = self.detail(package_id)
        assert fresh.status_code == 200, fresh.content
        assert [row['submission_id'] for row in fresh.json()['results']] == [retained.pk]
        assert fresh.json()['counts']['pending_review'] == 1

    def test_detail_rechecks_returned_assignment_version_even_when_still_owned(self):
        package_id = self.create().json()['id']
        assignment = self.assignment()
        self.client.force_authenticate(self.annotator)
        original = package_api.current_read_policy

        def rotate_before_final_check(*args, **kwargs):
            TaskAssignment.objects.filter(pk=assignment.pk).update(version=assignment.version + 1)
            return original(*args, **kwargs)

        with patch('task_packages.api.current_read_policy', side_effect=rotate_before_final_check):
            response = self.detail(package_id)
        assert response.status_code == 403, response.content
        assert 'results' not in response.json()
        fresh = self.detail(package_id)
        assert fresh.status_code == 200, fresh.content
        assert fresh.json()['results'][0]['assignment_version'] == assignment.version + 1

    def test_read_actions_consume_fresh_project_policy_after_queries(self):
        package_id = self.create().json()['id']
        self.submission(self.assignment())
        original = package_api.current_read_policy

        def transition_before_final_check(*args, **kwargs):
            type(self.project).objects.filter(pk=self.project.pk).update(description='read-only')
            return original(*args, **kwargs)

        with override_settings(ANNOTATION_PROJECT_OPERATION_PROVIDER=f'{__name__}.ChangedProjectPolicy'):
            for actor, role in ((self.manager, 'manager'), (self.annotator, 'annotator'), (self.reviewer, 'reviewer')):
                type(self.project).objects.filter(pk=self.project.pk).update(description='')
                self.client.force_authenticate(actor)
                with patch('task_packages.api.current_read_policy', side_effect=transition_before_final_check):
                    response = self.client.get(self.url) if role == 'manager' else self.detail(package_id)
                assert response.status_code == 200, response.content
                data = response.json()
                if role == 'manager':
                    assert data['can_create'] is False
                elif role == 'annotator':
                    assert data['results'] and all(row['editor_url'] is None for row in data['results'])
                    assert data['policy']['state'] == 'paused'
                else:
                    assert data['can_review'] is False and data['policy']['state'] == 'paused'

    def test_final_read_policy_denial_discards_materialized_package_data(self):
        package_id = self.create().json()['id']
        original = package_api.current_read_policy

        def deny_before_final_check(*args, **kwargs):
            type(self.project).objects.filter(pk=self.project.pk).update(description='deny-reads')
            return original(*args, **kwargs)

        with override_settings(ANNOTATION_PROJECT_OPERATION_PROVIDER=f'{__name__}.ChangedProjectPolicy'), patch(
            'task_packages.api.current_read_policy', side_effect=deny_before_final_check
        ):
            response = self.detail(package_id)
        assert response.status_code == 403, response.content
        assert 'package' not in response.json() and 'counts' not in response.json()

    def test_feature_off_and_read_only_policy_are_independent(self):
        package_id = self.create().json()['id']
        with override_settings(ANNOTATION_FIXED_PACKAGES_ENABLED=False):
            assert self.client.get(f'{self.url}capability/').json() == {
                'enabled': False,
                'can_create': False,
            }
            assert self.detail(package_id).status_code == 404 and self.create().status_code == 404
        for state in ('paused', 'completed', 'archived'):
            with patch.object(ReadOnlyPolicy, 'state', state), override_settings(
                ANNOTATION_PROJECT_OPERATION_PROVIDER=f'{__name__}.ReadOnlyPolicy'
            ):
                data = self.detail(package_id).json()
                assert data['policy']['state'] == state
                assert self.create().status_code == 403
        with override_settings(ANNOTATION_PROJECT_OPERATION_PROVIDER=None):
            assert self.detail(package_id).status_code == 403 and self.create().status_code == 403

    def test_pagination_has_machine_code_and_read_does_not_write(self):
        package_id = self.create().json()['id']
        before = (
            TaskPackage.objects.count(),
            TaskPackageItem.objects.count(),
            TaskAssignment.objects.count(),
        )
        response = self.client.get(f'{self.url}{package_id}/', {'page_size': 1, 'page': 99})
        assert (
            response.status_code == 404 and response.json()['display_context']['reason'] == 'PACKAGE_PAGE_OUT_OF_RANGE'
        )
        assert self.detail(package_id).status_code == 200
        assert before == (
            TaskPackage.objects.count(),
            TaskPackageItem.objects.count(),
            TaskAssignment.objects.count(),
        )


@override_settings(ANNOTATION_FIXED_PACKAGES_ENABLED=True)
class TestPackageConcurrency(PackageFixture, TransactionTestCase):
    @staticmethod
    def client_for(user):
        # Reuse the existing write-fence harness's middleware warm-up. Upstream
        # SlashMiddleware construction must not race in HTTP worker threads.
        from tasks.tests.test_project_write_concurrency import TestProjectWriteConcurrency

        return TestProjectWriteConcurrency.client_for(user)

    def test_same_key_serializes_to_one_fixed_package(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Real row-lock behavior requires PostgreSQL')
        barrier = Barrier(2)
        key = str(uuid4())
        clients = [self.client_for(self.manager) for _ in range(2)]

        def create(client):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return client.post(
                    self.url,
                    {'name': 'race', 'task_ids': [t.pk for t in self.tasks]},
                    format='json',
                    HTTP_IDEMPOTENCY_KEY=key,
                ).status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(create, client) for client in clients]
            assert sorted(f.result(timeout=30) for f in futures) == [200, 201]
        assert TaskPackage.objects.count() == 1 and TaskPackageItem.objects.count() == 3

    def test_creation_rechecks_role_after_waiting_for_project_fence(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Real row-lock behavior requires PostgreSQL')
        ProjectMember.objects.filter(project=self.project, user=self.other).update(role='manager')
        entered = Event()
        client = self.client_for(self.other)

        def create():
            close_old_connections()
            try:
                return client.post(
                    self.url,
                    {'name': 'stale manager', 'task_ids': [self.tasks[0].pk]},
                    format='json',
                    HTTP_IDEMPOTENCY_KEY=str(uuid4()),
                ).status_code
            finally:
                close_old_connections()

        def observed_fence(*args, **kwargs):
            entered.set()
            return lock_project_operation(*args, **kwargs)

        with (
            patch('task_packages.api.lock_project_operation', side_effect=observed_fence),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            with transaction.atomic():
                lock_project_operation(self.project.pk, local_principal(self.manager), 'security_cancel')
                future = pool.submit(create)
                assert entered.wait(10)
                ProjectMember.objects.filter(project=self.project, user=self.other).update(enabled=False)
            assert future.result(timeout=30) in (403, 404)
        assert TaskPackage.objects.count() == 0

    def test_manager_read_rejects_committed_revocation_before_global_progress(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Independent committed read/write connections require PostgreSQL')
        package_id = self.create().json()['id']
        self.submission(self.assignment())
        member = ProjectMember.objects.get(project=self.project, user=self.other)
        reader = self.client_for(self.other)
        writer = self.client_for(self.manager)
        original = package_progress.live_items
        for change in ({'enabled': False}, {'role': 'annotator'}):
            ProjectMember.objects.filter(pk=member.pk).update(role='manager', enabled=True)
            entered, release = Event(), Event()

            def pause_before_aggregates(project):
                entered.set()
                assert release.wait(15)
                return original(project)

            def read():
                close_old_connections()
                try:
                    return reader.get(f'{self.url}{package_id}/')
                finally:
                    close_old_connections()

            with patch('task_packages.progress.live_items', side_effect=pause_before_aggregates), ThreadPoolExecutor(
                max_workers=1
            ) as pool:
                future = pool.submit(read)
                try:
                    assert entered.wait(15)
                    revoked = writer.patch(
                        f'/api/projects/{self.project.pk}/members/{member.pk}/', change, format='json'
                    )
                    assert revoked.status_code == 200, revoked.content
                finally:
                    release.set()
                response = future.result(timeout=30)
            assert response.status_code in (403, 404), response.json()
            assert b'counts' not in response.content and b'source_note' not in response.content

    def read_after_committed_row_change(self, actor, package_id, change):
        if connection.vendor != 'postgresql':
            self.skipTest('Independent committed read/write connections require PostgreSQL')
        reader = self.client_for(actor)
        entered, release = Event(), Event()
        original = package_api.current_read_policy

        def pause_after_materialization(*args, **kwargs):
            entered.set()
            assert release.wait(15)
            return original(*args, **kwargs)

        def read():
            close_old_connections()
            try:
                return reader.get(f'{self.url}{package_id}/')
            finally:
                close_old_connections()

        with (
            patch('task_packages.api.current_read_policy', side_effect=pause_after_materialization),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            future = pool.submit(read)
            try:
                assert entered.wait(10)
                change()
            finally:
                release.set()
            response = future.result(timeout=30)
        assert response.status_code == 403, response.content
        assert response.json()['display_context']['reason'] == 'PACKAGE_ACCESS_CHANGED'
        assert not {'results', 'counts', 'package'} & response.json().keys()

    def test_partial_cancellation_and_reassignment_commit_before_detail_response(self):
        package_id = self.create().json()['id']
        cancelled = self.assignment()
        retained = self.assignment(task=self.tasks[1])
        writer = self.client_for(self.manager)

        def reassign():
            response = writer.delete(f'/api/task-assignments/{cancelled.pk}/')
            assert response.status_code == 204, response.content
            response = writer.post(
                '/api/task-assignments/', {'task': cancelled.task_id, 'assignee': self.other.pk}, format='json'
            )
            assert response.status_code == 201, response.content

        self.read_after_committed_row_change(self.annotator, package_id, reassign)
        self.client.force_authenticate(self.annotator)
        response = self.detail(package_id)
        assert response.status_code == 200, response.content
        assert [row['assignment_id'] for row in response.json()['results']] == [retained.pk]
        assert response.json()['counts']['assignments'] == 1

    def test_partial_review_departure_commits_before_detail_response(self):
        package_id = self.create().json()['id']
        departed = self.submission(self.assignment())
        retained = self.submission(self.assignment(task=self.tasks[1]))
        writer = self.client_for(self.reviewer)

        def approve():
            response = writer.post(f'/api/submissions/{departed.pk}/review/', {'decision': 'approved'}, format='json')
            assert response.status_code == 200, response.content

        self.read_after_committed_row_change(self.reviewer, package_id, approve)
        self.client.force_authenticate(self.reviewer)
        response = self.detail(package_id)
        assert response.status_code == 200, response.content
        assert [row['submission_id'] for row in response.json()['results']] == [retained.pk]
        assert response.json()['counts']['pending_review'] == 1
