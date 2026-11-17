"""Regression coverage for the non-streaming background import commit boundary."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from data_import.functions import (
    _async_import_background_streaming,
    _async_reimport_background_streaming,
    async_import_background,
    async_reimport_background,
)
from data_import.models import FileUpload
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from io_storages.localfiles.models import LocalFilesExportStorage, LocalFilesExportStorageLink
from organizations.tests.factories import OrganizationFactory
from projects.models import ProjectImport, ProjectMember, ProjectReimport, ProjectSummary
from projects.tests.factories import ProjectFactory
from rest_framework.exceptions import PermissionDenied
from tasks.models import Task
from tasks.tests.factories import AnnotationFactory, TaskFactory
from users.models import User
from users.tests.factories import UserFactory


class BackgroundImportFenceTests(TestCase):
    def setUp(self):
        self.org = OrganizationFactory()
        self.actor = self.org.created_by
        self.project = ProjectFactory(organization=self.org)
        self.job = ProjectImport.objects.create(project=self.project, commit_to_project=True)

    def run_job(self):
        with (
            patch('data_import.functions.flag_set', return_value=False),
            patch(
                'data_import.functions.load_tasks_for_async_import',
                return_value=([{'data': {'text': 'x'}}], [], {}, []),
            ),
        ):
            async_import_background(self.job.pk, self.actor.pk)
        self.job.refresh_from_db()

    def test_error_after_task_write_rolls_back_and_records_failure(self):
        with (
            patch('projects.models.ProjectSummary.update_data_columns', side_effect=RuntimeError('injected failure')),
            patch('data_import.functions.emit_webhooks_for_instance') as webhook,
        ):
            self.run_job()
        self.assertEqual(self.job.status, ProjectImport.Status.FAILED)
        self.assertIn('injected failure', self.job.error)
        self.assertFalse(Task.objects.filter(project=self.project).exists())
        webhook.assert_not_called()

    @override_settings(
        ANNOTATION_PROJECT_OPERATION_PROVIDER='tasks.tests.test_project_operation_policy.PausedProvider'
    )
    def test_queued_job_rechecks_paused_policy_before_writing(self):
        self.run_job()
        self.assertEqual(self.job.status, ProjectImport.Status.FAILED)
        self.assertFalse(Task.objects.filter(project=self.project).exists())

    def test_queued_job_rechecks_actor_activity(self):
        ProjectMember.objects.filter(project=self.project, user=self.actor).delete()
        # Project creator may have implicit management rights; disable the actor
        # to exercise the authority recheck independent of that fallback.
        admin = UserFactory(is_staff=True, is_superuser=True)
        User.objects.filter(pk=self.actor.pk).update(is_active=False, session_actor=admin)
        self.run_job()
        self.assertEqual(self.job.status, ProjectImport.Status.FAILED)
        self.assertFalse(Task.objects.filter(project=self.project).exists())

    def test_queued_job_rechecks_revoked_manager_role(self):
        self.actor = UserFactory(active_organization=self.org)
        self.org.add_user(self.actor)
        member = ProjectMember.objects.create(project=self.project, user=self.actor, role='manager')
        member.role = 'annotator'
        member.save(update_fields=['role'])
        self.run_job()
        self.assertEqual(self.job.status, ProjectImport.Status.FAILED)
        self.assertFalse(Task.objects.filter(project=self.project).exists())

    def test_active_manager_can_import(self):
        self.run_job()
        self.assertEqual(self.job.status, ProjectImport.Status.COMPLETED)
        self.assertEqual(Task.objects.filter(project=self.project).count(), 1)

    def test_changed_label_config_during_parsing_aborts_commit(self):
        def parse(*args):
            type(self.project).objects.filter(pk=self.project.pk).update(label_config=self.project.label_config + '\n')
            return [{'data': {'text': 'x'}}], [], {}, []

        with patch('data_import.functions.flag_set', return_value=False), patch(
            'data_import.functions.load_tasks_for_async_import', side_effect=parse
        ):
            async_import_background(self.job.pk, self.actor.pk)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ProjectImport.Status.FAILED)
        self.assertIn('configuration changed', self.job.error)
        self.assertFalse(Task.objects.filter(project=self.project).exists())


@override_settings(REIMPORT_BATCH_SIZE=1)
class StreamingReimportAtomicTests(TestCase):
    def setUp(self):
        self.org = OrganizationFactory()
        self.actor = self.org.created_by
        self.project = ProjectFactory(organization=self.org)
        self.upload = FileUpload.objects.create(
            user=self.actor,
            project=self.project,
            file=ContentFile(b'[{"data":{"text":"one"}},{"data":{"text":"two"}}]', name='tasks.json'),
        )
        self.old_task = TaskFactory(project=self.project, file_upload=self.upload)
        self.job = ProjectReimport.objects.create(
            project=self.project,
            file_upload_ids=[self.upload.pk],
            files_as_tasks_list=True,
        )

    def run_job(self):
        _async_reimport_background_streaming(self.job, self.project, self.org.pk, self.actor)

    def test_legacy_nonstream_worker_rolls_back_failed_commit(self):
        with patch('data_import.functions.flag_set', return_value=False), patch.object(
            ProjectSummary, 'update_data_columns', side_effect=RuntimeError('summary failed')
        ):
            with self.assertRaisesRegex(RuntimeError, 'summary failed'):
                async_reimport_background(self.job.pk, self.org.pk, self.actor.pk)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ProjectReimport.Status.FAILED)
        self.assertEqual(
            list(Task.objects.filter(project=self.project).values_list('pk', flat=True)), [self.old_task.pk]
        )

    def test_legacy_nonstream_worker_rechecks_revoked_manager(self):
        actor = UserFactory(active_organization=self.org)
        self.org.add_user(actor)
        member = ProjectMember.objects.create(project=self.project, user=actor, role='annotator')
        with patch('data_import.functions.flag_set', return_value=False):
            with self.assertRaises(PermissionDenied):
                async_reimport_background(self.job.pk, self.org.pk, actor.pk)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ProjectReimport.Status.FAILED)
        self.assertTrue(Task.objects.filter(pk=self.old_task.pk).exists())
        self.assertEqual(member.role, 'annotator')

    def test_second_batch_failure_rolls_back_deletion_and_first_batch(self):
        original = ProjectSummary.update_data_columns
        calls = []

        def fail_second(summary, tasks):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('second batch failed')
            return original(summary, tasks)

        with (
            patch.object(ProjectSummary, 'update_data_columns', fail_second),
            patch('data_import.functions.emit_webhooks_for_instance') as webhook,
        ):
            with self.assertRaisesRegex(RuntimeError, 'second batch failed'):
                self.run_job()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ProjectReimport.Status.FAILED)
        self.assertEqual(
            list(Task.objects.filter(project=self.project).values_list('pk', flat=True)), [self.old_task.pk]
        )
        webhook.assert_not_called()

    def test_success_replaces_all_batches_with_one_receipt(self):
        self.run_job()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ProjectReimport.Status.COMPLETED)
        self.assertEqual(self.job.task_count, 2)
        self.assertEqual(
            set(Task.objects.filter(project=self.project).values_list('data__text', flat=True)), {'one', 'two'}
        )

    def test_empty_legacy_file_list_does_not_import_all_files(self):
        self.job.file_upload_ids = []
        self.job.save(update_fields=['file_upload_ids'])
        self.run_job()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ProjectReimport.Status.COMPLETED)
        self.assertEqual(
            list(Task.objects.filter(project=self.project).values_list('pk', flat=True)), [self.old_task.pk]
        )

    def test_changed_file_binding_during_staging_is_rejected(self):
        def changed_source(*args, **kwargs):
            yield [{'data': {'text': 'new'}, 'file_upload_id': self.upload.pk}], {}, {'text'}
            FileUpload.objects.filter(pk=self.upload.pk).update(file='changed.json')

        with patch('data_import.functions.FileUpload.load_tasks_from_uploaded_files_streaming', changed_source):
            with self.assertRaisesRegex(Exception, 'changed during staging'):
                self.run_job()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ProjectReimport.Status.FAILED)
        self.assertTrue(Task.objects.filter(pk=self.old_task.pk).exists())

    def test_existing_export_link_prevents_irreversible_delete(self):
        annotation = AnnotationFactory(task=self.old_task, project=self.project)
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalFilesExportStorage.objects.create(
                project=self.project,
                path=directory,
                can_delete_objects=True,
            )
            link = LocalFilesExportStorageLink.objects.create(storage=storage, annotation=annotation)
            artifact = Path(directory) / str(link.get_key(annotation))
            artifact.write_text('retained', encoding='utf-8')
            with self.assertRaisesRegex(Exception, 'external export links'):
                self.run_job()
            self.assertTrue(artifact.exists())
            self.assertTrue(LocalFilesExportStorageLink.objects.filter(pk=link.pk).exists())
            self.assertTrue(Task.objects.filter(pk=self.old_task.pk).exists())
            self.job.refresh_from_db()
            self.assertEqual(self.job.status, ProjectReimport.Status.FAILED)

    @override_settings(
        ANNOTATION_PROJECT_OPERATION_PROVIDER='tasks.tests.test_project_operation_policy.PausedProvider'
    )
    def test_paused_reimport_keeps_old_tasks(self):
        with self.assertRaises(Exception):
            self.run_job()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ProjectReimport.Status.FAILED)
        self.assertTrue(Task.objects.filter(pk=self.old_task.pk).exists())


class StreamingImportAtomicTests(TestCase):
    def setUp(self):
        self.org = OrganizationFactory()
        self.actor = self.org.created_by
        self.project = ProjectFactory(organization=self.org)
        self.job = ProjectImport.objects.create(project=self.project, commit_to_project=True)

    def run_job(self):
        batches = [
            ([{'data': {'text': 'one'}}], [], {}, ['text']),
            ([{'data': {'text': 'two'}}], [], {}, ['text']),
        ]
        with (
            patch('data_import.functions.load_tasks_for_async_import_streaming', return_value=iter(batches)),
            patch('data_import.functions.flag_set', return_value=False),
        ):
            _async_import_background_streaming(self.job, self.actor)

    def test_second_batch_failure_rolls_back_first_batch(self):
        original = ProjectSummary.update_data_columns
        calls = []

        def fail_second(summary, tasks):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('second batch failed')
            return original(summary, tasks)

        with patch.object(ProjectSummary, 'update_data_columns', fail_second):
            with self.assertRaisesRegex(RuntimeError, 'second batch failed'):
                self.run_job()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ProjectImport.Status.FAILED)
        self.assertFalse(Task.objects.filter(project=self.project).exists())

    def test_success_commits_all_batches_and_receipt(self):
        self.run_job()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ProjectImport.Status.COMPLETED)
        self.assertEqual(self.job.task_count, 2)
        self.assertEqual(Task.objects.filter(project=self.project).count(), 2)

    def test_changed_label_config_during_staging_aborts_commit(self):
        def batches(*args):
            yield [{'data': {'text': 'one'}}], [], {}, ['text']
            type(self.project).objects.filter(pk=self.project.pk).update(label_config=self.project.label_config + '\n')
            yield [{'data': {'text': 'two'}}], [], {}, ['text']

        with patch('data_import.functions.load_tasks_for_async_import_streaming', side_effect=batches):
            with self.assertRaisesRegex(Exception, 'configuration changed'):
                _async_import_background_streaming(self.job, self.actor)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ProjectImport.Status.FAILED)
        self.assertFalse(Task.objects.filter(project=self.project).exists())

    @override_settings(
        ANNOTATION_PROJECT_OPERATION_PROVIDER='tasks.tests.test_project_operation_policy.PausedProvider'
    )
    def test_paused_import_has_no_tasks(self):
        with patch('data_import.functions.load_tasks_for_async_import_streaming') as loader:
            with self.assertRaises(Exception):
                _async_import_background_streaming(self.job, self.actor)
            loader.assert_not_called()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ProjectImport.Status.FAILED)
        self.assertFalse(Task.objects.filter(project=self.project).exists())
