"""Real PostgreSQL sessions: transaction fences, not mocked lock assertions."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import MagicMock, patch
from uuid import uuid4

from access_control.project_operations import local_principal
from data_export import audit_contract as c
from data_export import review_audit as audit
from data_export.audit_models import AuditActorBinding, AuditPackage
from django.db import close_old_connections, connection, connections
from rest_framework.test import APITransactionTestCase
from tasks.models import Task
from users.models import User

from .test_review_audit import AuditFixtures


@skipUnless(connection.vendor == 'postgresql', 'Real PostgreSQL lock/concurrency acceptance')
class TestAuditConcurrency(AuditFixtures, APITransactionTestCase):
    def setUp(self):
        self.__class__.setUpTestData()
        self.audit_setup()

    def threaded(self, work):
        close_old_connections()
        try:
            return work()
        finally:
            connections.close_all()

    def command(self, body, key, actor_id=None):
        actor = User.objects.get(pk=actor_id or self.manager.pk)
        ctx = audit.scope(None, self.project.pk, resolved=(local_principal(actor), actor))
        return str(audit.capture(None, ctx, dict(body), str(key)).pk)

    def test_F03_two_same_key_commands_one_generation_and_package(self):
        source, batch = self.prepared()
        body = self.audit_body(batch)
        key = uuid4()
        barrier = threading.Barrier(2)

        def work():
            barrier.wait(timeout=10)
            return self.command(body, key)

        with ThreadPoolExecutor(2) as pool:
            tasks = [pool.submit(self.threaded, work) for _ in range(2)]
            values = [task.result(timeout=30) for task in tasks]
        assert values[0] == values[1]
        assert AuditPackage.objects.count() == 1 and AuditActorBinding.objects.count() == 1

    def race_delete(self, *, user=False, queryset=False, unlink=False, project=False, source_kind=None):
        source, batch = self.prepared()
        body = self.audit_body(batch)
        captured = threading.Event()
        release = threading.Event()
        delete_started = threading.Event()
        deleted = threading.Event()
        original = audit.audit_source.load
        pids = {}

        def loader(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get('lock') and not captured.is_set():
                with connections['default'].cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pids['capture'] = cursor.fetchone()[0]
                captured.set()
                assert release.wait(15)
            return result

        actor_id = self.producer.pk if user else self.manager.pk

        def remove():
            assert captured.wait(15)
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_backend_pid()')
                pids['delete'] = cursor.fetchone()[0]
            delete_started.set()
            if project:
                from projects.models import Project

                if queryset:
                    Project.all_objects.filter(pk=self.project.pk).delete()
                else:
                    Project.all_objects.get(pk=self.project.pk).delete()
            elif source_kind == 'assignment':
                source.assignment.delete()
            elif source_kind == 'submission':
                source.delete()
            elif user:
                if queryset:
                    User.objects.filter(pk=actor_id).delete()
                else:
                    User.objects.get(pk=actor_id).delete()
            elif unlink:
                Task.objects.filter(pk=source.assignment.task_id).update(project=None)
            elif queryset:
                Task.objects.filter(pk=source.assignment.task_id).delete()
            else:
                Task.objects.get(pk=source.assignment.task_id).delete()
            deleted.set()

        with patch.object(audit.audit_source, 'load', side_effect=loader), ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.threaded, lambda: self.command(body, uuid4(), actor_id))
            second = pool.submit(self.threaded, remove)
            try:
                assert captured.wait(15) and delete_started.wait(15)
                deadline = time.monotonic() + 5
                blocked = False
                while time.monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT pg_blocking_pids(%s)', [pids['delete']])
                        if pids['capture'] in cursor.fetchone()[0]:
                            blocked = True
                            break
                    time.sleep(0.01)
                assert (
                    blocked and not deleted.is_set()
                ), 'deletion/unlink must wait for the capture fence or locked source'
            finally:
                release.set()
            package_id = first.result(timeout=30)
            second.result(timeout=30)
        return AuditPackage.objects.filter(pk=package_id).first(), actor_id

    def test_F08_task_instance_delete_waits_before_collector(self):
        package, _ = self.race_delete()
        assert package.state == 'dispatch_pending' and not Task.objects.exists()

    def test_F08_task_queryset_delete_waits_before_collector(self):
        package, _ = self.race_delete(queryset=True)
        assert package.state == 'dispatch_pending' and not Task.objects.exists()

    def test_F08_dm_project_unlink_waits_before_scope_changes(self):
        package, _ = self.race_delete(unlink=True)
        assert package.state == 'dispatch_pending' and Task.objects.get().project_id is None

    def test_B06_F08_creator_delete_tombstones_original_generation(self):
        package, old_id = self.race_delete(user=True, queryset=True)
        who = AuditActorBinding.objects.get(pk=package.creator_id)
        assert who.tombstone and who.live_user_id is None
        from projects.models import ProjectMember
        from users.tests.factories import UserFactory

        replacement = UserFactory(id=old_id, active_organization=self.org)
        self.org.add_user(replacement)
        ProjectMember.objects.create(project=self.project, user=replacement, role='manager')
        self.client.force_authenticate(self.manager)
        response = self.client.get(self.audit_base + f'{package.pk}/')
        assert response.status_code in (403, 404), response.data
        assert AuditActorBinding.objects.get(pk=who.pk).tombstone

    def test_F08_project_instance_delete_waits_and_cleans_business_rows(self):
        package, _ = self.race_delete(project=True)
        assert package is None and AuditActorBinding.objects.count() == 1

    def test_F08_project_queryset_delete_waits_and_cleans_business_rows(self):
        package, _ = self.race_delete(project=True, queryset=True)
        assert package is None and AuditActorBinding.objects.count() == 1

    def test_F08_user_instance_delete_waits_and_tombstones(self):
        package, _ = self.race_delete(user=True)
        assert package is not None
        binding = AuditActorBinding.objects.get(pk=package.creator_id)
        assert binding.tombstone and binding.live_user_id is None

    def test_F08_internal_assignment_delete_waits_for_locked_source(self):
        package, _ = self.race_delete(source_kind='assignment')
        assert package is not None and c.strict(package.summary_record)['counts']['revision'] == 1

    def test_F08_internal_submission_delete_waits_for_locked_source(self):
        package, _ = self.race_delete(source_kind='submission')
        assert package is not None and c.strict(package.summary_record)['counts']['revision'] == 1

    def test_PC13_postcommit_enqueue_exception_is_unknown_and_exactly_recoverable(self):
        from rest_framework.exceptions import APIException

        source, batch = self.prepared()
        body, key = self.audit_body(batch), str(uuid4())
        with patch.object(audit, 'enqueue', side_effect=APIException('PII_POSTCOMMIT_CANARY')):
            response = self.capture(batch, body=body, key=key)
        assert response.status_code == 500 and 'X-Audit-Command-State' not in response
        assert 'PII_POSTCOMMIT_CANARY' not in str(response.data)
        package = AuditPackage.objects.get()
        with patch.object(audit.audit_source, 'load', side_effect=AssertionError('No live source after commit')):
            response = self.capture(batch, body=body, key=key)
        assert response.status_code == 202 and response.data['package_id'] == str(package.pk)
        assert AuditPackage.objects.count() == 1 and AuditActorBinding.objects.count() == 1

    def test_C06_preflight_queue_and_format_io_are_outside_write_transaction(self):
        source, batch = self.prepared()
        body = self.audit_body(batch)
        original = audit.c.formatter
        observed = []

        def fmt():
            observed.append(connection.in_atomic_block)
            assert not connection.in_atomic_block
            return original()

        with patch.object(audit.c, 'formatter', side_effect=fmt), patch.object(
            audit, 'queue_available', side_effect=lambda: not connection.in_atomic_block
        ):
            self.command(body, uuid4())
        assert observed == [False]

    def test_F07_three_attempts_keep_ready_bytes_inside_two_scratch_slots(self):
        from data_export import audit_files, audit_format

        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        queue = MagicMock()
        paused, release, writing, finish = ({}, threading.Event(), threading.Event(), threading.Event())
        original_lease, original_write = audit_files.ScratchLease, audit_format.write

        def claim():
            with patch.object(audit.approved, 'api_queue', return_value=queue):
                self.real_enqueue(str(package.pk))
            package.refresh_from_db()
            return str(package.token)

        def recover():
            package.refresh_from_db()
            actor = User.objects.get(pk=self.manager.pk)
            ctx = audit.scope(None, self.project.pk, resolved=(local_principal(actor), actor))
            with patch.object(audit.timezone, 'now', return_value=package.started_at + timedelta(minutes=32)):
                audit.retry(None, ctx, package, str(uuid4()))
            return claim()

        first = claim()
        paused[first] = threading.Event()

        def lease(row, token):
            if token in paused:
                paused[token].set()
                assert release.wait(20)
                if token != first:
                    assert writing.wait(20)
            return original_lease(row, token)

        def write(row, *args, **kwargs):
            if str(row.token) == first:
                writing.set()
                assert finish.wait(20)
            return original_write(row, *args, **kwargs)

        with patch.object(audit_files, 'ScratchLease', side_effect=lease), patch.object(
            audit_format, 'write', side_effect=write
        ), ThreadPoolExecutor(2) as pool:
            a = pool.submit(self.threaded, lambda: audit.generate(str(package.pk), first))
            assert paused[first].wait(15)
            second = recover()
            paused[second] = threading.Event()
            b = pool.submit(self.threaded, lambda: audit.generate(str(package.pk), second))
            assert paused[second].wait(15)
            third = recover()
            audit.generate(str(package.pk), third)
            package.refresh_from_db()
            assert package.state == 'ready'
            try:
                release.set()
                assert writing.wait(15)
                b.result(timeout=15)
                root = audit_files.directory(package).parent
                assert len(list(root.glob('.scratch-*'))) == 2
                assert sum(p.stat().st_size for p in root.rglob('*') if p.is_file()) <= c.limits()['scratch']
            finally:
                finish.set()
                release.set()
            a.result(timeout=20)
        package.refresh_from_db()
        assert package.state == 'ready' and str(package.token) == third
        assert len(list(audit_files.directory(package).parent.glob('.scratch-*'))) == 1
