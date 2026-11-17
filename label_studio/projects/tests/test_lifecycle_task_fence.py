"""Project task endpoints must serialize with a future lifecycle transition."""
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from access_control.project_operations import EDIT, MANAGE_TASKS, READ_WORK, SYNC_STORAGE, ProjectOperationPolicy
from data_export.models import ConvertedFormat, DataExport, Export
from data_import import api as import_api
from data_import import functions as import_functions
from data_import.models import FileUpload
from data_manager import api as data_manager_api
from django.core.files.base import ContentFile
from django.db import close_old_connections, connection, transaction
from django.http import HttpResponse
from django.test import TransactionTestCase, override_settings
from io_storages.localfiles.models import LocalFilesExportStorage, LocalFilesImportStorage
from ml.models import MLBackend
from organizations.tests.factories import OrganizationFactory
from projects import api as project_api
from projects.models import Project, ProjectImport, ProjectMember, ProjectReimport, ProjectSummary
from projects.tests.factories import ProjectFactory
from rest_framework.test import APIClient
from tasks import api as task_api
from tasks.models import Task
from tasks.tests.factories import AnnotationFactory, TaskFactory
from users.tests.factories import UserFactory


class TitlePolicy:
    """Test-only stand-in; title is not a product lifecycle state."""

    def evaluate(self, project):
        paused = project.title == 'test-paused'
        return ProjectOperationPolicy(
            'test-v1',
            'paused' if paused else 'active',
            frozenset({READ_WORK} if paused else {READ_WORK, EDIT, MANAGE_TASKS, SYNC_STORAGE}),
            'PROJECT_PAUSED' if paused else '',
        )


@override_settings(ANNOTATION_PROJECT_OPERATION_PROVIDER='projects.tests.test_lifecycle_task_fence.TitlePolicy')
class ProjectTaskFenceTests(TransactionTestCase):
    def setUp(self):
        self.org = OrganizationFactory()
        self.manager = self.org.created_by
        self.project = ProjectFactory(organization=self.org, title='test-active')
        self.task = TaskFactory(project=self.project)
        self.url = f'/api/projects/{self.project.pk}/tasks/'

    def request(self, method, actor=None, pid=None):
        close_old_connections()
        try:
            if pid is not None:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pid.append(cursor.fetchone()[0])
            client = APIClient()
            client.force_authenticate(actor or self.manager)
            if method == 'post':
                return client.post(self.url, {'data': {'text': 'new'}}, format='json')
            return client.delete(self.url)
        finally:
            connection.close()

    def test_denied_states_do_not_create_or_delete(self):
        Project.objects.filter(pk=self.project.pk).update(title='test-paused')
        for method in ('post', 'delete'):
            response = self.request(method)
            self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(list(Task.objects.filter(project=self.project).values_list('pk', flat=True)), [self.task.pk])

    def test_paused_project_rejects_project_update_delete_and_sync_import(self):
        Project.objects.filter(pk=self.project.pk).update(title='test-paused')
        client = APIClient()
        client.force_authenticate(self.manager)
        update = client.patch(f'/api/projects/{self.project.pk}', {'description': 'changed'}, format='json')
        delete = client.delete(f'/api/projects/{self.project.pk}')
        imported = client.post(f'/api/projects/{self.project.pk}/import', [{'data': {'text': 'new'}}], format='json')
        predictions = client.post(
            f'/api/projects/{self.project.pk}/import/predictions',
            [{'task': self.task.pk, 'result': []}],
            format='json',
        )
        prediction = client.post('/api/predictions/', {'task': self.task.pk, 'result': []}, format='json')
        for response in (update, delete, imported, predictions, prediction):
            self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(Task.objects.filter(project=self.project).count(), 1)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())

    def test_project_update_waiting_for_pause_is_denied(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires PostgreSQL row locks')
        pid = []

        def update():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pid.append(cursor.fetchone()[0])
                client = APIClient()
                client.force_authenticate(self.manager)
                return client.patch(f'/api/projects/{self.project.pk}', {'description': 'changed'}, format='json')
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                project = Project.objects.select_for_update().get(pk=self.project.pk)
                project.title = 'test-paused'
                project.save(update_fields=['title'])
                future = pool.submit(update)
                self.assert_waiting(pid)
            response = future.result(30)
        self.assertEqual(response.status_code, 403, response.content)
        self.project.refresh_from_db()
        self.assertNotEqual(self.project.description, 'changed')

    def test_prediction_changes_require_manager_and_consistent_project(self):
        client = APIClient()
        client.force_authenticate(self.manager)
        created = client.post('/api/predictions/', {'task': self.task.pk, 'result': []}, format='json')
        self.assertEqual(created.status_code, 201, created.content)
        prediction_id = created.data['id']
        other = ProjectFactory(organization=self.org, title='test-active')
        moved = client.patch(f'/api/predictions/{prediction_id}/', {'project': other.pk}, format='json')
        self.assertEqual(moved.status_code, 400, moved.content)
        actor = UserFactory(active_organization=self.org)
        self.org.add_user(actor)
        ProjectMember.objects.create(project=self.project, user=actor, role='annotator')
        client.force_authenticate(actor)
        denied = client.delete(f'/api/predictions/{prediction_id}/')
        self.assertIn(denied.status_code, (403, 404), denied.content)
        client.force_authenticate(self.manager)
        Project.objects.filter(pk=self.project.pk).update(title='test-paused')
        for method in ('patch', 'delete'):
            response = getattr(client, method)(f'/api/predictions/{prediction_id}/', {'score': 0.1}, format='json')
            self.assertEqual(response.status_code, 403, response.content)

    def test_sync_import_rechecks_policy_after_parsing(self):
        entered, release = threading.Event(), threading.Event()
        original = import_api.load_tasks

        def staged(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            if not release.wait(15):
                raise TimeoutError('test did not resume import')
            return result

        def worker():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(self.manager)
                return client.post(
                    f'/api/projects/{self.project.pk}/import', [{'data': {'text': 'new'}}], format='json'
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(import_api, 'load_tasks', staged):
            future = pool.submit(worker)
            try:
                self.assertTrue(entered.wait(15))
                Project.objects.filter(pk=self.project.pk).update(title='test-paused')
            finally:
                release.set()
            response = future.result(30)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(Task.objects.filter(project=self.project).count(), 1)

    def test_task_create_webhook_does_not_run_for_rolled_back_write(self):
        client = APIClient()
        client.force_authenticate(self.manager)
        with patch.object(project_api, 'emit_webhooks_for_instance') as webhook:
            with self.assertRaisesRegex(RuntimeError, 'rollback'):
                with transaction.atomic():
                    response = client.post(self.url, {'data': {'text': 'rolled back'}}, format='json')
                    self.assertEqual(response.status_code, 201, response.content)
                    webhook.assert_not_called()
                    raise RuntimeError('rollback')
            webhook.assert_not_called()
        self.assertEqual(Task.objects.filter(project=self.project).count(), 1)

    def test_reimport_rejects_missing_file_without_removing_tasks(self):
        client = APIClient()
        client.force_authenticate(self.manager)
        response = client.post(
            f'/api/projects/{self.project.pk}/reimport', {'file_upload_ids': [999999999]}, format='json'
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertTrue(Task.objects.filter(pk=self.task.pk).exists())

    def test_paused_project_blocks_upload_delete_and_storage_creation(self):
        upload = FileUpload.objects.create(
            user=self.manager,
            project=self.project,
            file=ContentFile(b'[]', name='tasks.json'),
        )
        Project.objects.filter(pk=self.project.pk).update(title='test-paused')
        client = APIClient()
        client.force_authenticate(self.manager)
        with tempfile.TemporaryDirectory() as directory, patch.object(LocalFilesImportStorage, 'validate_connection'):
            responses = [
                client.delete(f'/api/import/file-upload/{upload.pk}'),
                client.delete(
                    f'/api/projects/{self.project.pk}/file-uploads', {'file_upload_ids': [upload.pk]}, format='json'
                ),
                client.post(
                    '/api/storages/localfiles/', {'project': self.project.pk, 'path': directory}, format='json'
                ),
            ]
        for response in responses:
            self.assertEqual(response.status_code, 403, response.content)
        self.assertTrue(FileUpload.objects.filter(pk=upload.pk).exists())

    @override_settings(ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True)
    def test_unenrolled_bulk_actions_are_denied_in_lifecycle_mode(self):
        client = APIClient()
        client.force_authenticate(self.manager)
        with patch.object(data_manager_api, 'perform_action') as perform:
            response = client.post(f'/api/dm/actions/?project={self.project.pk}&id=delete_tasks', {}, format='json')
        self.assertEqual(response.status_code, 403, response.content)
        perform.assert_not_called()
        self.assertTrue(Task.objects.filter(pk=self.task.pk).exists())

    @override_settings(ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True)
    def test_next_task_is_denied_after_pause(self):
        Project.objects.filter(pk=self.project.pk).update(title='test-paused')
        client = APIClient()
        client.force_authenticate(self.manager)
        response = client.post(f'/api/dm/actions/?project={self.project.pk}&id=next_task', {}, format='json')
        self.assertEqual(response.status_code, 403, response.content)

    @override_settings(ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True)
    def test_project_next_task_is_denied_after_pause(self):
        Project.objects.filter(pk=self.project.pk).update(title='test-paused')
        client = APIClient()
        client.force_authenticate(self.manager)
        response = client.get(f'/api/projects/{self.project.pk}/next/')
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(self.task.locks.exists())

    @override_settings(ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True)
    def test_project_next_task_is_available_when_active(self):
        client = APIClient()
        client.force_authenticate(self.manager)
        with patch.object(project_api, 'get_next_task', return_value=(self.task, 'test')) as sampler:
            response = client.get(f'/api/projects/{self.project.pk}/next/')
        self.assertEqual(response.status_code, 200, response.content)
        sampler.assert_called_once()

    @override_settings(ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True)
    def test_project_next_task_waiting_on_pause_cannot_create_task_lock(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires PostgreSQL row locks')
        pid = []

        def next_task():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pid.append(cursor.fetchone()[0])
                client = APIClient()
                client.force_authenticate(self.manager)
                return client.get(f'/api/projects/{self.project.pk}/next/')
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                project = Project.objects.select_for_update().get(pk=self.project.pk)
                project.title = 'test-paused'
                project.save(update_fields=['title'])
                future = pool.submit(next_task)
                self.assert_waiting(pid)
            response = future.result(30)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(self.task.locks.exists())

    @override_settings(ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True)
    def test_next_task_waiting_on_pause_cannot_create_task_lock(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires PostgreSQL row locks')
        pid = []

        def next_task():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pid.append(cursor.fetchone()[0])
                client = APIClient()
                client.force_authenticate(self.manager)
                return client.post(f'/api/dm/actions/?project={self.project.pk}&id=next_task', {}, format='json')
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                project = Project.objects.select_for_update().get(pk=self.project.pk)
                project.title = 'test-paused'
                project.save(update_fields=['title'])
                future = pool.submit(next_task)
                self.assert_waiting(pid)
            response = future.result(30)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(self.task.locks.exists())

    @override_settings(ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True)
    def test_read_does_not_start_unenrolled_prediction_work(self):
        Project.objects.filter(pk=self.project.pk).update(evaluate_predictions_automatically=True)
        client = APIClient()
        client.force_authenticate(self.manager)
        with patch.object(task_api, 'evaluate_predictions') as task_predict, patch.object(
            data_manager_api, 'evaluate_predictions'
        ) as list_predict:
            detail = client.get(f'/api/tasks/{self.task.pk}/')
            task_list = client.get(f'/api/tasks/?project={self.project.pk}')
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(task_list.status_code, 200, task_list.content)
        task_predict.assert_not_called()
        list_predict.assert_not_called()

    def test_legacy_read_still_starts_configured_prediction_work(self):
        Project.objects.filter(pk=self.project.pk).update(evaluate_predictions_automatically=True)
        client = APIClient()
        client.force_authenticate(self.manager)
        with patch.object(task_api, 'evaluate_predictions') as task_predict:
            response = client.get(f'/api/tasks/{self.task.pk}/')
        self.assertEqual(response.status_code, 200, response.content)
        task_predict.assert_called_once()

    @override_settings(ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True)
    def test_live_prelabeling_does_not_start_unenrolled_prediction_work(self):
        self.project.show_collab_predictions = True
        self.project.model_version = 'live-model'
        self.task.project = self.project
        with patch.object(Project, 'has_ml_backend', return_value=True), patch(
            'data_manager.functions.evaluate_predictions'
        ) as evaluate:
            self.assertEqual(self.task.get_predictions_for_prelabeling(), [])
        evaluate.assert_not_called()

    @override_settings(ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True)
    def test_unenrolled_summary_recalculation_is_denied(self):
        client = APIClient()
        client.force_authenticate(self.manager)
        with patch.object(project_api, 'start_job_async_or_sync') as enqueue:
            response = client.post(f'/api/projects/{self.project.pk}/summary/reset/', {}, format='json')
        self.assertEqual(response.status_code, 400, response.content)
        enqueue.assert_not_called()

    @override_settings(ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True)
    def test_unenrolled_export_generation_and_deletion_are_blocked(self):
        export = Export.objects.create(project=self.project, status=Export.Status.COMPLETED, file='synthetic.json')
        client = APIClient()
        client.force_authenticate(self.manager)
        root = f'/api/projects/{self.project.pk}'
        with patch('data_export.api.flag_set', return_value=False), patch.object(
            DataExport, 'generate_export_file'
        ) as generate, patch.object(Export, 'run_file_exporting') as start, patch.object(
            Export, 'convert_file'
        ) as convert, patch(
            'data_export.api.start_job_async_or_sync'
        ) as enqueue, patch(
            'data_export.api.RangedFileResponse', return_value=HttpResponse()
        ) as download:
            responses = (
                client.get(f'{root}/export'),
                client.post(f'{root}/exports/', {}, format='json'),
                client.delete(f'{root}/exports/{export.pk}'),
                client.post(f'{root}/exports/{export.pk}/convert', {'export_type': 'CSV'}, format='json'),
                client.get(f'{root}/exports/{export.pk}/download?exportType=CSV'),
            )
            for response in responses:
                self.assertEqual(response.status_code, 403, response.content)
            listing = client.get(f'{root}/exports/')
            detail = client.get(f'{root}/exports/{export.pk}')
            existing = client.get(f'{root}/exports/{export.pk}/download')
            self.assertEqual(listing.status_code, 200, listing.content)
            self.assertEqual(detail.status_code, 200, detail.content)
            self.assertEqual(existing.status_code, 200, existing.content)
            download.assert_called_once()
        for effect in (generate, start, convert, enqueue):
            effect.assert_not_called()
        self.assertTrue(Export.objects.filter(pk=export.pk).exists())
        self.assertFalse(ConvertedFormat.objects.filter(export=export).exists())

    @override_settings(ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True)
    def test_unenrolled_ml_requests_and_configuration_are_blocked(self):
        backend = MLBackend.objects.create(project=self.project, url='https://synthetic.invalid', title='synthetic')
        client = APIClient()
        client.force_authenticate(self.manager)
        with patch.object(MLBackend, 'update_state') as update, patch.object(
            MLBackend, 'healthcheck_'
        ) as health, patch.object(MLBackend, 'setup_') as setup, patch.object(
            MLBackend, 'train'
        ) as train, patch.object(
            MLBackend, '_predict'
        ) as predict, patch.object(
            MLBackend, 'interactive_annotating'
        ) as interactive, patch.object(
            MLBackend, 'get_versions'
        ) as get_versions:
            listing = client.get(f'/api/ml/?project={self.project.pk}')
            detail = client.get(f'/api/ml/{backend.pk}')
            create = client.post(
                '/api/ml/', {'project': self.project.pk, 'url': 'https://synthetic.invalid'}, format='json'
            )
            change = client.patch(f'/api/ml/{backend.pk}', {'title': 'changed'}, format='json')
            delete = client.delete(f'/api/ml/{backend.pk}')
            training = client.post(f'/api/ml/{backend.pk}/train', {}, format='json')
            test = client.post(f'/api/ml/{backend.pk}/predict/test?random=true', {}, format='json')
            labeling = client.post(
                f'/api/ml/{backend.pk}/interactive-annotating', {'task': self.task.pk}, format='json'
            )
            versions = client.get(f'/api/ml/{backend.pk}/versions')
        self.assertEqual(listing.status_code, 200, listing.content)
        self.assertEqual(detail.status_code, 200, detail.content)
        for response in (create, change, delete, training, test, labeling, versions):
            self.assertEqual(response.status_code, 403, response.content)
        for method in (update, health, setup, train, predict, interactive, get_versions):
            method.assert_not_called()

    @override_settings(ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True)
    def test_unenrolled_label_writes_are_blocked(self):
        client = APIClient()
        client.force_authenticate(self.manager)
        self.assertEqual(client.get('/api/labels/').status_code, 200)
        for path in ('/api/labels/', '/api/label_links/', '/api/labels/bulk'):
            response = client.post(path, {}, format='json')
            self.assertEqual(response.status_code, 403, response.content)
        for path in ('/api/labels/1/', '/api/label_links/1/'):
            for response in (client.patch(path, {}, format='json'), client.delete(path)):
                self.assertEqual(response.status_code, 403, response.content)

    @override_settings(ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True)
    def test_signal_suppressing_project_and_bulk_delete_are_denied(self):
        client = APIClient()
        client.force_authenticate(self.manager)
        bulk = client.delete(self.url)
        project = client.delete(f'/api/projects/{self.project.pk}')
        self.assertEqual(bulk.status_code, 400, bulk.content)
        self.assertEqual(project.status_code, 400, project.content)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())
        self.assertTrue(Task.objects.filter(pk=self.task.pk).exists())

    def test_paused_project_cannot_delete_model_version_predictions(self):
        Project.objects.filter(pk=self.project.pk).update(title='test-paused')
        client = APIClient()
        client.force_authenticate(self.manager)
        response = client.delete(
            f'/api/projects/{self.project.pk}/model-versions/', {'model_version': 'v1'}, format='json'
        )
        self.assertEqual(response.status_code, 403, response.content)

    def test_sync_reimport_replaces_tasks_atomically(self):
        upload = FileUpload.objects.create(
            user=self.manager,
            project=self.project,
            file=ContentFile(b'[{"data":{"text":"replacement"}}]', name='tasks.json'),
        )
        self.task.file_upload = upload
        self.task.save(update_fields=['file_upload'])
        client = APIClient()
        client.force_authenticate(self.manager)
        response = client.post(
            f'/api/projects/{self.project.pk}/reimport', {'file_upload_ids': [upload.pk]}, format='json'
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertFalse(Task.objects.filter(pk=self.task.pk).exists())
        self.assertEqual(
            list(Task.objects.filter(project=self.project).values_list('data__text', flat=True)), ['replacement']
        )

    def test_sync_reimport_failure_restores_previous_tasks(self):
        upload = FileUpload.objects.create(
            user=self.manager,
            project=self.project,
            file=ContentFile(b'[{"data":{"text":"replacement"}}]', name='tasks.json'),
        )
        self.task.file_upload = upload
        self.task.save(update_fields=['file_upload'])
        client = APIClient()
        client.force_authenticate(self.manager)
        with patch.object(ProjectSummary, 'update_data_columns', side_effect=RuntimeError('summary failed')):
            response = client.post(
                f'/api/projects/{self.project.pk}/reimport', {'file_upload_ids': [upload.pk]}, format='json'
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(list(Task.objects.filter(project=self.project).values_list('pk', flat=True)), [self.task.pk])

    def test_non_manager_cannot_create_or_delete(self):
        actor = UserFactory(active_organization=self.org)
        ProjectMember.objects.create(project=self.project, user=actor, role='annotator')
        for method in ('post', 'delete'):
            response = self.request(method, actor)
            self.assertIn(response.status_code, (403, 404), response.content)
        self.assertTrue(Task.objects.filter(pk=self.task.pk).exists())

    def test_active_manager_can_create_and_delete(self):
        created = self.request('post')
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(Task.objects.filter(project=self.project).count(), 2)
        deleted = self.request('delete')
        self.assertEqual(deleted.status_code, 204, deleted.content)
        self.assertFalse(Task.objects.filter(project=self.project).exists())

    def test_other_project_manager_cannot_write(self):
        other = OrganizationFactory()
        for method in ('post', 'delete'):
            response = self.request(method, other.created_by)
            self.assertIn(response.status_code, (403, 404), response.content)
        self.assertTrue(Task.objects.filter(pk=self.task.pk).exists())

    def assert_waiting(self, pid):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if pid:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s', [pid[0]])
                    row = cursor.fetchone()
                if row and row[0] == 'Lock':
                    return
            time.sleep(0.02)
        self.fail('connection did not wait on a PostgreSQL lock')

    def test_transition_first_blocks_waiting_writes(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires PostgreSQL row locks')
        for method in ('post', 'delete'):
            pid = []
            with ThreadPoolExecutor(max_workers=1) as pool:
                with transaction.atomic():
                    project = Project.objects.select_for_update().get(pk=self.project.pk)
                    project.title = 'test-paused'
                    project.save(update_fields=['title'])
                    future = pool.submit(self.request, method, pid=pid)
                    self.assert_waiting(pid)
                response = future.result(30)
            self.assertEqual(response.status_code, 403, response.content)
            self.assertTrue(Task.objects.filter(pk=self.task.pk).exists())
            Project.objects.filter(pk=self.project.pk).update(title='test-active')

    def test_write_first_completes_before_transition(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires PostgreSQL row locks')
        for method in ('post', 'delete'):
            entered, release = threading.Event(), threading.Event()
            original = project_api.lock_project_operation

            def hold(*args, **kwargs):
                result = original(*args, **kwargs)
                entered.set()
                if not release.wait(15):
                    raise TimeoutError('test did not release the write')
                return result

            pid = []

            def transition():
                close_old_connections()
                try:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT pg_backend_pid()')
                        pid.append(cursor.fetchone()[0])
                    with transaction.atomic():
                        project = Project.objects.select_for_update().get(pk=self.project.pk)
                        project.title = 'test-paused'
                        project.save(update_fields=['title'])
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=2) as pool, patch.object(project_api, 'lock_project_operation', hold):
                write = pool.submit(self.request, method)
                try:
                    self.assertTrue(entered.wait(15))
                    change = pool.submit(transition)
                    self.assert_waiting(pid)
                finally:
                    release.set()
                response = write.result(30)
                change.result(30)
            self.assertEqual(response.status_code, 201 if method == 'post' else 204, response.content)
            self.assertEqual(Project.objects.get(pk=self.project.pk).title, 'test-paused')
            Project.objects.filter(pk=self.project.pk).update(title='test-active')

    def test_queued_import_waits_for_transition_and_denies_stale_work(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires PostgreSQL row locks')
        job = ProjectImport.objects.create(project=self.project, commit_to_project=True)
        pid = []

        def worker():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pid.append(cursor.fetchone()[0])
                import_functions.async_import_background(job.pk, self.manager.pk)
            finally:
                connection.close()

        with (
            patch.object(import_functions, 'flag_set', return_value=False),
            patch.object(
                import_functions, 'load_tasks_for_async_import', return_value=([{'data': {'text': 'x'}}], [], {}, [])
            ),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            with transaction.atomic():
                project = Project.objects.select_for_update().get(pk=self.project.pk)
                project.title = 'test-paused'
                project.save(update_fields=['title'])
                future = pool.submit(worker)
                self.assert_waiting(pid)
            future.result(30)
        job.refresh_from_db()
        self.assertEqual(job.status, ProjectImport.Status.FAILED)
        self.assertEqual(Task.objects.filter(project=self.project).count(), 1)

    def test_streaming_jobs_wait_for_transition_and_leave_tasks_unchanged(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires PostgreSQL row locks')
        upload = FileUpload.objects.create(
            user=self.manager,
            project=self.project,
            file=ContentFile(b'[{"data":{"text":"new"}}]', name='stream.json'),
        )
        self.task.file_upload = upload
        self.task.save(update_fields=['file_upload'])

        for reimport in (False, True):
            if reimport:
                job = ProjectReimport.objects.create(project=self.project, file_upload_ids=[upload.pk])
            else:
                job = ProjectImport.objects.create(
                    project=self.project, file_upload_ids=[upload.pk], commit_to_project=True
                )
            pid = []

            def worker():
                close_old_connections()
                try:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT pg_backend_pid()')
                        pid.append(cursor.fetchone()[0])
                    if reimport:
                        import_functions._async_reimport_background_streaming(
                            job, self.project, self.org.pk, self.manager
                        )
                    else:
                        import_functions._async_import_background_streaming(job, self.manager)
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=1) as pool:
                with transaction.atomic():
                    project = Project.objects.select_for_update().get(pk=self.project.pk)
                    project.title = 'test-paused'
                    project.save(update_fields=['title'])
                    future = pool.submit(worker)
                    self.assert_waiting(pid)
                with self.assertRaises(Exception):
                    future.result(30)
            job.refresh_from_db()
            self.assertEqual(job.status, 'failed')
            self.assertEqual(
                list(Task.objects.filter(project=self.project).values_list('pk', flat=True)), [self.task.pk]
            )
            Project.objects.filter(pk=self.project.pk).update(title='test-active')

    def test_streaming_import_finishes_before_waiting_transition(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires PostgreSQL row locks')
        job = ProjectImport.objects.create(
            project=self.project,
            tasks=[{'data': {'text': 'new'}}],
            commit_to_project=True,
        )
        entered, release = threading.Event(), threading.Event()
        original = import_functions.lock_project_operation
        pid = []
        calls = []

        def hold(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(1)
            if len(calls) == 2:
                entered.set()
                if not release.wait(15):
                    raise TimeoutError('test did not release the import')
            return result

        def worker():
            close_old_connections()
            try:
                import_functions._async_import_background_streaming(job, self.manager)
            finally:
                connection.close()

        def transition():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pid.append(cursor.fetchone()[0])
                with transaction.atomic():
                    project = Project.objects.select_for_update().get(pk=self.project.pk)
                    project.title = 'test-paused'
                    project.save(update_fields=['title'])
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(import_functions, 'lock_project_operation', hold):
            write = pool.submit(worker)
            try:
                self.assertTrue(entered.wait(15))
                change = pool.submit(transition)
                self.assert_waiting(pid)
            finally:
                release.set()
            write.result(30)
            change.result(30)
        job.refresh_from_db()
        self.assertEqual(job.status, ProjectImport.Status.COMPLETED)
        self.assertEqual(Task.objects.filter(project=self.project).count(), 2)
        self.assertEqual(Project.objects.get(pk=self.project.pk).title, 'test-paused')

    def test_direct_task_patch_waits_for_pause_and_is_denied(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires PostgreSQL row locks')
        pid = []

        def worker():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pid.append(cursor.fetchone()[0])
                client = APIClient()
                client.force_authenticate(self.manager)
                return client.patch(f'/api/tasks/{self.task.pk}/', {'data': {'text': 'changed'}}, format='json')
            finally:
                connection.close()

        original = self.task.data
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                project = Project.objects.select_for_update().get(pk=self.project.pk)
                project.title = 'test-paused'
                project.save(update_fields=['title'])
                future = pool.submit(worker)
                self.assert_waiting(pid)
            response = future.result(30)
        self.assertEqual(response.status_code, 403, response.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.data, original)

    def test_export_waits_for_transition_and_does_not_open_file(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires PostgreSQL row locks')
        annotation = AnnotationFactory(task=self.task, project=self.project)
        storage = LocalFilesExportStorage.objects.create(project=self.project, path='/tmp/issue86-test')
        pid = []

        def worker():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pid.append(cursor.fetchone()[0])
                storage.save_annotation(annotation)
            finally:
                connection.close()

        with patch('io_storages.localfiles.models.open_local_file_write') as output:
            with ThreadPoolExecutor(max_workers=1) as pool:
                with transaction.atomic():
                    project = Project.objects.select_for_update().get(pk=self.project.pk)
                    project.title = 'test-paused'
                    project.save(update_fields=['title'])
                    future = pool.submit(worker)
                    self.assert_waiting(pid)
                with self.assertRaises(Exception):
                    future.result(30)
            output.assert_not_called()
