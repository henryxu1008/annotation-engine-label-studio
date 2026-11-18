from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from task_packages.models import TaskPackage, TaskPackageBatch, TaskPackageItem
from task_packages.tests.test_packages import PackageFixture
from tasks.models import Task, TaskAssignment
from tasks.tests.factories import TaskFactory


@skipUnless(connection.vendor == 'postgresql', 'Batch creation requires PostgreSQL row locks.')
@override_settings(ANNOTATION_FIXED_PACKAGES_ENABLED=True)
class TestPackageBatch(PackageFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.batch_url = self.url + 'batches/'
        self.payload = {
            'name': 'batch',
            'source_note': 'source',
            'task_ids': [x.pk for x in self.tasks],
            'batch_size': 2,
        }

    def post_batch(self, payload=None, key=None, user=None):
        client = APIClient()
        client.force_authenticate(user or self.manager)
        return client.post(
            self.batch_url, payload or self.payload, format='json', HTTP_IDEMPOTENCY_KEY=str(key or uuid4())
        )

    def test_thousand_tasks_and_lost_response_replay(self):
        tasks = Task.objects.bulk_create([Task(project=self.project, data={'text': 'synthetic'}) for _ in range(997)])
        ids = [t.pk for t in self.tasks + tasks]
        payload = {**self.payload, 'task_ids': list(reversed(ids)), 'batch_size': 128}
        key = uuid4()
        first = self.post_batch(payload, key)
        assert first.status_code == 201, first.content
        receipt = first.json()
        assert [len(p['task_ids']) for p in receipt['packages']] == [128] * 7 + [104]
        assert [i for p in receipt['packages'] for i in p['task_ids']] == sorted(ids)
        replay = self.post_batch({**payload, 'task_ids': ids}, key)
        assert replay.status_code == 200 and replay.json() == {**receipt, 'replayed': True}
        assert self.post_batch({**payload, 'batch_size': 127}, key).status_code == 409
        assert TaskPackage.objects.count() == 8 and TaskPackageItem.objects.count() == 1000
        assert TaskPackageBatch.objects.count() == 1 and not TaskAssignment.objects.exists()

    def test_response_failure_after_commit_recovers_original_receipt(self):
        key = uuid4()
        with patch('task_packages.api.Response', side_effect=RuntimeError('lost response')):
            assert self.post_batch(key=key).status_code == 500
        assert TaskPackageBatch.objects.count() == 1 and TaskPackage.objects.count() == 2
        replay = self.post_batch(key=key)
        assert replay.status_code == 200 and replay.json()['replayed'] is True
        assert TaskPackageBatch.objects.count() == 1 and TaskPackage.objects.count() == 2

    def imported_tasks(self):
        from projects.models import ProjectImport

        job = ProjectImport.objects.create(project=self.project, commit_to_project=True, return_task_ids=True)
        tasks = [
            TaskFactory(project=self.project),
            TaskFactory(project=self.project),
            TaskFactory(project=self.project),
        ]
        job.task_ids = [task.pk for task in tasks]
        job.task_count = len(tasks)
        job.status = ProjectImport.Status.COMPLETED
        job.finished_at = timezone.now()
        job.save()
        return job, tasks

    def test_completed_background_import_supplies_verifiable_task_ids(self):
        from data_import.functions import async_import_background
        from projects.models import ProjectImport

        job = ProjectImport.objects.create(project=self.project, commit_to_project=True, return_task_ids=True)
        imported = [{'data': {'text': 'imported one'}}, {'data': {'text': 'imported two'}}]
        with patch('data_import.functions.flag_set', return_value=False), patch(
            'data_import.functions.load_tasks_for_async_import', return_value=(imported, [], {}, [])
        ), patch('data_import.functions.emit_webhooks_for_instance'):
            async_import_background(job.pk, self.manager.pk)
        job.refresh_from_db()
        assert job.status == ProjectImport.Status.COMPLETED and len(job.task_ids) == 2
        result = self.post_batch({**self.payload, 'project_import_id': job.pk, 'task_ids': job.task_ids})
        assert result.status_code == 201, result.content
        assert result.json()['source']['id'] == job.pk

    def test_completed_streaming_import_supplies_verifiable_task_ids(self):
        from data_import.functions import _async_import_background_streaming
        from projects.models import ProjectImport

        job = ProjectImport.objects.create(project=self.project, commit_to_project=True, return_task_ids=True)
        batches = [([{'data': {'text': 'one'}}], [], {}, ['text']), ([{'data': {'text': 'two'}}], [], {}, ['text'])]
        with patch('data_import.functions.flag_set', return_value=False), patch(
            'data_import.functions.load_tasks_for_async_import_streaming', return_value=iter(batches)
        ), patch('data_import.functions.emit_webhooks_for_instance'):
            _async_import_background_streaming(job, self.manager)
        job.refresh_from_db()
        assert job.status == ProjectImport.Status.COMPLETED and len(job.task_ids) == 2
        result = self.post_batch({**self.payload, 'project_import_id': job.pk, 'task_ids': job.task_ids})
        assert result.status_code == 201, result.content
        assert result.json()['source']['id'] == job.pk

    def test_import_provenance_is_verified_and_survives_original_record_deletion(self):
        job, tasks = self.imported_tasks()
        key = uuid4()
        payload = {**self.payload, 'project_import_id': job.pk, 'task_ids': [tasks[2].pk, tasks[0].pk]}
        first = self.post_batch(payload, key)
        assert first.status_code == 201, first.content
        source = first.json()['source']
        assert source['type'] == 'project_import_task_ids_v1' and source['id'] == job.pk
        assert source['task_count'] == 3 and len(source['task_ids_hash']) == 64
        assert [i for package in first.json()['packages'] for i in package['task_ids']] == sorted(payload['task_ids'])
        job.delete()
        replay = self.post_batch(payload, key)
        assert replay.status_code == 200 and replay.json()['source'] == source
        assert self.post_batch({**payload, 'project_import_id': 999999}, key).status_code == 409

    def test_import_provenance_rejects_unverifiable_or_wrong_scope(self):
        from projects.models import ProjectImport

        job, tasks = self.imported_tasks()
        payload = {**self.payload, 'project_import_id': job.pk, 'task_ids': [tasks[0].pk]}
        for changes in (
            {'status': ProjectImport.Status.IN_PROGRESS},
            {'commit_to_project': False},
            {'return_task_ids': False},
            {'finished_at': None},
            {'task_ids': []},
            {'task_ids': [tasks[0].pk, tasks[0].pk]},
            {'task_count': 4},
        ):
            original = {key: getattr(job, key) for key in changes}
            ProjectImport.objects.filter(pk=job.pk).update(**changes)
            result = self.post_batch(payload)
            assert result.status_code == 400, result.content
            ProjectImport.objects.filter(pk=job.pk).update(**original)
        result = self.post_batch({**payload, 'task_ids': [self.tasks[0].pk]})
        assert result.status_code == 400
        other_job = ProjectImport.objects.create(project=TaskFactory().project, status=ProjectImport.Status.COMPLETED)
        assert self.post_batch({**payload, 'project_import_id': other_job.pk}).status_code == 400
        assert not TaskPackageBatch.objects.exists() and not TaskPackage.objects.exists()

    def test_oversized_import_receipt_is_rejected_without_creating_packages(self):
        from projects.models import ProjectImport

        job, tasks = self.imported_tasks()
        ids = [tasks[0].pk] + list(range(100000000, 100010000))
        ProjectImport.objects.filter(pk=job.pk).update(task_ids=ids, task_count=len(ids))
        result = self.post_batch({**self.payload, 'project_import_id': job.pk, 'task_ids': [tasks[0].pk]})
        assert result.status_code == 400, result.content
        assert not TaskPackageBatch.objects.exists() and not TaskPackage.objects.exists()

    def test_import_state_is_rechecked_after_concurrent_update(self):
        from projects.models import ProjectImport

        job, tasks = self.imported_tasks()
        payload = {**self.payload, 'project_import_id': job.pk, 'task_ids': [tasks[0].pk]}
        from django.db.models.query import QuerySet

        entered = Event()
        original_first = QuerySet.first

        def traced_first(queryset):
            if queryset.model is ProjectImport:
                entered.set()
            return original_first(queryset)

        def worker():
            close_old_connections()
            try:
                return self.post_batch(payload).status_code
            finally:
                close_old_connections()

        with patch.object(QuerySet, 'first', traced_first), ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                locked = ProjectImport.objects.select_for_update().get(pk=job.pk)
                future = executor.submit(worker)
                assert entered.wait(10)
                assert not future.done()
                locked.status = ProjectImport.Status.FAILED
                locked.save(update_fields=['status', 'updated_at'])
            assert future.result(timeout=20) == 400
        assert not TaskPackageBatch.objects.exists()

    def test_reused_task_id_after_import_does_not_inherit_provenance(self):
        job, tasks = self.imported_tasks()
        old_id = tasks[0].pk
        Task.objects.filter(pk=old_id).delete()
        Task.objects.create(pk=old_id, project=self.project, data={'text': 'replacement'})
        result = self.post_batch({**self.payload, 'project_import_id': job.pk, 'task_ids': [old_id]})
        assert result.status_code == 400 and not TaskPackageBatch.objects.exists()

    def test_import_record_cannot_claim_a_task_created_before_import(self):
        from projects.models import ProjectImport

        job = ProjectImport.objects.create(
            project=self.project,
            commit_to_project=True,
            return_task_ids=True,
            status=ProjectImport.Status.COMPLETED,
            task_ids=[self.tasks[0].pk],
            task_count=1,
            finished_at=timezone.now(),
        )
        result = self.post_batch({**self.payload, 'project_import_id': job.pk, 'task_ids': [self.tasks[0].pk]})
        assert result.status_code == 400 and not TaskPackageBatch.objects.exists()

    def test_invalid_and_unauthorized_inputs_do_not_write(self):
        foreign = TaskFactory()
        for ids in ([self.tasks[0].pk] * 2, [self.tasks[0].pk, foreign.pk], []):
            assert self.post_batch({**self.payload, 'task_ids': ids}).status_code == 400
        assert self.post_batch(user=self.annotator).status_code in (403, 404)
        assert not TaskPackageBatch.objects.exists() and not TaskPackage.objects.exists()

    def test_failure_rolls_back_all_packages_and_retry_succeeds(self):
        key = uuid4()
        from task_packages import api

        real_create = api.TaskPackageItem.objects.bulk_create
        calls = 0

        def injected(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('injected failure')
            return real_create(*args, **kwargs)

        with patch.object(api.TaskPackageItem.objects, 'bulk_create', side_effect=injected):
            assert self.post_batch(key=key).status_code == 500
        assert not TaskPackage.objects.exists() and not TaskPackageBatch.objects.exists()
        assert self.post_batch(key=key).status_code == 201

    def test_concurrent_same_key_creates_only_one_complete_batch(self):
        key = uuid4()
        barrier = Barrier(2)

        def run(_):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = self.post_batch(key=key)
                return response.status_code, response.json()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(run, range(2)))
        assert sorted(status for status, _ in results) == [200, 201], results
        assert results[0][1]['batch_id'] == results[1][1]['batch_id']
        assert TaskPackageBatch.objects.count() == 1 and TaskPackage.objects.count() == 2
        assert TaskPackageItem.objects.count() == 3

    def test_another_manager_cannot_replay_or_learn_the_receipt(self):
        from projects.models import ProjectMember
        from users.tests.factories import UserFactory

        other = UserFactory(active_organization=self.organization)
        self.organization.add_user(other)
        ProjectMember.objects.create(project=self.project, user=other, role='manager')
        key = uuid4()
        assert self.post_batch(key=key).status_code == 201
        response = self.post_batch(key=key, user=other)
        assert response.status_code == 409
        assert 'packages' not in response.json()
        assert TaskPackageBatch.objects.count() == 1

    def test_reverse_migration_rejects_nonempty_receipt_ledger(self):
        from importlib import import_module
        from types import SimpleNamespace
        from django.apps import apps

        guard = import_module('task_packages.migrations.0008_package_batch').preserve_receipts
        schema_editor = SimpleNamespace(connection=connection)
        guard(apps, schema_editor)
        assert self.post_batch().status_code == 201
        with self.assertRaises(RuntimeError):
            guard(apps, schema_editor)
        from django.db.migrations.executor import MigrationExecutor

        with self.assertRaisesRegex(RuntimeError, 'batch receipts exist'):
            MigrationExecutor(connection).migrate([('task_packages', '0007_pool_allocation_notices')])
        assert TaskPackageBatch.objects.count() == 1

    def test_replay_requires_current_authorization(self):
        key = uuid4()
        assert self.post_batch(key=key).status_code == 201
        assert self.post_batch(key=key, user=self.annotator).status_code in (403, 404)
        with override_settings(
            ANNOTATION_PROJECT_OPERATION_PROVIDER='task_packages.tests.test_packages.ReadOnlyPolicy'
        ):
            response = self.post_batch(key=key)
            assert response.status_code in (403, 409), response.content
