"""Exercise real HTTP write transactions on PostgreSQL, including their outer locks."""

import threading
from concurrent.futures import ThreadPoolExecutor

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase
from organizations.models import OrganizationMember
from organizations.tests.factories import OrganizationFactory
from projects.models import ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.test import APIClient
from tasks.models import Submission, TaskAssignment
from tasks.tests.factories import AnnotationFactory, TaskFactory
from users.tests.factories import UserFactory


class TestProjectWriteConcurrency(TransactionTestCase):
    @staticmethod
    def client_for(user):
        # Production workers construct middleware before handling concurrent
        # requests. Upstream slash middleware temporarily changes MIDDLEWARE
        # during construction, so do not cold-start test handlers in threads.
        client = APIClient()
        client.force_authenticate(user=user)
        client.handler.load_middleware()
        return client

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL row locks are required')
        self.organization = OrganizationFactory()
        self.project = ProjectFactory(organization=self.organization)
        self.manager = self.organization.created_by
        self.annotator = UserFactory(active_organization=self.organization)
        self.membership = ProjectMember.objects.create(
            project=self.project, user=self.annotator, role=ProjectMember.Role.ANNOTATOR
        )
        self.task = TaskFactory(project=self.project)
        self.annotation = AnnotationFactory(task=self.task, completed_by=self.annotator, result=[])
        self.assignment = TaskAssignment.objects.create(
            project=self.project,
            task=self.task,
            assignee=self.annotator,
            assigned_by=self.manager,
            annotation=self.annotation,
        )

    def test_editor_submit_and_member_revocation_have_no_reverse_lock_cycle(self):
        first_lock = threading.Event()
        revocation_attempted = threading.Event()
        revocation_locked_project = threading.Event()
        locks = []

        def editor_sql(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if 'FOR UPDATE' in sql and not locks:
                locks.append(sql)
                first_lock.set()
                assert revocation_attempted.wait(8), 'Revocation did not reach its project lock'
                # On the unfixed baseline the editor holds Assignment first.
                # Let revocation hold Project, then run the actual API onward.
                # With Project first, revocation waits and the editor may finish.
                if '"tasks_taskassignment"' in sql:
                    assert revocation_locked_project.wait(8)
            return result

        def revocation_sql(execute, sql, params, many, context):
            project_lock = 'FOR UPDATE' in sql and '"project"' in sql
            if project_lock:
                revocation_attempted.set()
            result = execute(sql, params, many, context)
            if project_lock:
                revocation_locked_project.set()
            return result

        def request(client, method, url, data, wrapper):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '10s'")
                with connection.execute_wrapper(wrapper):
                    response = getattr(client, method)(url, data=data, format='json')
                return response.status_code
            finally:
                connection.close()

        editor_client = self.client_for(self.annotator)
        revoker_client = self.client_for(self.manager)
        with ThreadPoolExecutor(max_workers=2) as pool:
            editor = pool.submit(
                request,
                editor_client,
                'patch',
                f'/api/annotations/{self.annotation.id}/',
                {
                    'result': [],
                    'assignment_id': self.assignment.id,
                    'assignment_version': 1,
                    'submit_for_review': True,
                },
                editor_sql,
            )
            assert first_lock.wait(8), 'Editor did not acquire a row lock'
            revoke = pool.submit(
                request,
                revoker_client,
                'patch',
                f'/api/projects/{self.project.id}/members/{self.membership.id}/',
                {'enabled': False},
                revocation_sql,
            )
            assert editor.result(timeout=25) == 200
            assert revoke.result(timeout=25) == 200

        self.assignment.refresh_from_db()
        assert self.assignment.status == TaskAssignment.Status.CANCELLED
        assert Submission.objects.filter(assignment=self.assignment).count() == 1

    def _revocation_wins(self, *, cancel=False, reassign=False):
        project_locked = threading.Event()
        editor_waiting = threading.Event()
        successor = UserFactory(active_organization=self.organization)
        ProjectMember.objects.create(project=self.project, user=successor, role='annotator')
        revoker_client = self.client_for(self.manager)
        editor_client = self.client_for(self.annotator)

        def revoke_sql(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if 'FOR UPDATE' in sql and '"project"' in sql and not project_locked.is_set():
                project_locked.set()
                assert editor_waiting.wait(8)
            return result

        def editor_sql(execute, sql, params, many, context):
            if 'FOR UPDATE' in sql and '"project"' in sql:
                editor_waiting.set()
            return execute(sql, params, many, context)

        def revoke():
            close_old_connections()
            try:
                client = revoker_client
                with connection.execute_wrapper(revoke_sql), transaction.atomic():
                    if cancel:
                        response = client.delete(f'/api/task-assignments/{self.assignment.pk}/')
                        assert response.status_code == 204, response.content
                        if reassign:
                            assigned = client.post(
                                '/api/task-assignments/',
                                {'task': self.task.pk, 'assignee': successor.pk},
                                format='json',
                            )
                            assert assigned.status_code == 201, assigned.content
                    else:
                        response = client.patch(
                            f'/api/projects/{self.project.pk}/members/{self.membership.pk}/',
                            {'enabled': False},
                            format='json',
                        )
                        assert response.status_code == 200, response.content
            finally:
                connection.close()

        def edit():
            close_old_connections()
            try:
                client = editor_client
                with connection.execute_wrapper(editor_sql):
                    return client.patch(
                        f'/api/annotations/{self.annotation.pk}/',
                        {
                            'assignment_id': self.assignment.pk,
                            'assignment_version': 1,
                            'result': [],
                            'submit_for_review': True,
                        },
                        format='json',
                    ).status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            revocation = pool.submit(revoke)
            assert project_locked.wait(8)
            editing = pool.submit(edit)
            revocation.result(timeout=25)
            assert editing.result(timeout=25) in (403, 409)
        assert not Submission.objects.exists()
        self.assignment.refresh_from_db()
        assert self.assignment.status == TaskAssignment.Status.CANCELLED
        if reassign:
            new = TaskAssignment.objects.get(task=self.task, status__in=TaskAssignment.ACTIVE_STATUSES)
            assert new.assignee_id == successor.pk
            assert new.annotation_id is None
            assert not new.drafts.exists()
            assert self.assignment.annotation_id == self.annotation.pk

    def test_member_revocation_commits_first_stale_editor_is_rejected(self):
        self._revocation_wins()

    def test_assignment_cancellation_commits_first_stale_editor_is_rejected(self):
        self._revocation_wins(cancel=True)

    def test_cancel_and_new_assignment_do_not_copy_answers_or_accept_old_editor(self):
        self._revocation_wins(cancel=True, reassign=True)

    def test_assignment_create_revalidates_target_after_member_revocation(self):
        create_waiting = threading.Event()
        revocation_done = threading.Event()
        task = TaskFactory(project=self.project)
        assigning_client = self.client_for(self.manager)
        revoking_client = self.client_for(self.manager)

        def before_project_lock(execute, sql, params, many, context):
            if 'FOR UPDATE' in sql and '"project"' in sql and not create_waiting.is_set():
                create_waiting.set()
                assert revocation_done.wait(8)
            return execute(sql, params, many, context)

        def assign():
            close_old_connections()
            try:
                client = assigning_client
                with connection.execute_wrapper(before_project_lock):
                    return client.post(
                        '/api/task-assignments/', {'task': task.pk, 'assignee': self.annotator.pk}, format='json'
                    ).status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            assigning = pool.submit(assign)
            assert create_waiting.wait(8)
            client = revoking_client
            response = client.patch(
                f'/api/projects/{self.project.pk}/members/{self.membership.pk}/',
                {'enabled': False},
                format='json',
            )
            assert response.status_code == 200, response.content
            revocation_done.set()
            assert assigning.result(timeout=25) == 400
        assert not TaskAssignment.objects.filter(task=task).exists()

    def _create_after_target_change(self, change):
        before_lock = threading.Event()
        changed = threading.Event()
        task = TaskFactory(project=self.project)
        assigning_client = self.client_for(self.manager)

        def pause(execute, sql, params, many, context):
            if 'FOR UPDATE' in sql and '"project"' in sql and not before_lock.is_set():
                before_lock.set()
                assert changed.wait(8)
            return execute(sql, params, many, context)

        def assign():
            close_old_connections()
            try:
                client = assigning_client
                with connection.execute_wrapper(pause):
                    return client.post(
                        '/api/task-assignments/', {'task': task.pk, 'assignee': self.annotator.pk}, format='json'
                    ).status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            assigning = pool.submit(assign)
            assert before_lock.wait(8)
            try:
                change()
            finally:
                changed.set()
            assert assigning.result(timeout=25) == 400
        assert not TaskAssignment.objects.filter(task=task).exists()

    def test_assignment_create_revalidates_disabled_target_account(self):
        from users.models import User

        security_admin = UserFactory(is_staff=True, is_superuser=True)
        self._create_after_target_change(
            lambda: User.objects.filter(pk=self.annotator.pk).update(is_active=False, session_actor=security_admin)
        )

    def test_assignment_create_revalidates_soft_deleted_organization_member(self):
        member = OrganizationMember.objects.get(organization=self.organization, user=self.annotator)
        self._create_after_target_change(member.soft_delete)

    def test_two_managers_cannot_create_duplicate_active_assignment(self):
        from threading import Barrier

        barrier = Barrier(2)
        other_manager = UserFactory(active_organization=self.organization)
        ProjectMember.objects.create(project=self.project, user=other_manager, role='manager')
        task = TaskFactory(project=self.project)
        clients = {manager.pk: self.client_for(manager) for manager in [self.manager, other_manager]}

        def assign(manager):
            close_old_connections()
            reached = False

            def pause(execute, sql, params, many, context):
                nonlocal reached
                if 'FOR UPDATE' in sql and '"project"' in sql and not reached:
                    reached = True
                    barrier.wait(timeout=8)
                return execute(sql, params, many, context)

            try:
                client = clients[manager.pk]
                with connection.execute_wrapper(pause):
                    return client.post(
                        '/api/task-assignments/', {'task': task.pk, 'assignee': self.annotator.pk}, format='json'
                    ).status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(assign, [self.manager, other_manager]))
        assert sorted(results) == [201, 400]
        assert TaskAssignment.objects.filter(task=task, status__in=TaskAssignment.ACTIVE_STATUSES).count() == 1
