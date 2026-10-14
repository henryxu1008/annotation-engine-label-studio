from organizations.tests.factories import OrganizationFactory
from projects.models import ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.test import APIRequestFactory, APITestCase, force_authenticate
from data_export.api import async_convert
from data_export.models import ConvertedFormat, Export
from io_storages.localfiles.models import LocalFilesExportStorage, _export_annotation_to_local_files
from io_storages.redis.models import RedisExportStorage, _export_annotation_to_redis_storages
from io_storages.s3.models import S3ExportStorage, S3ExportStorageLink, _dispatch_s3_annotation_export
from io_storages.api import ExportStorageListAPI, _run_export_storage_auto_sync
from io_storages.tests.factories import AzureBlobExportStorageFactory
from rest_framework.exceptions import ValidationError
from tasks.models import Annotation, ReviewDecision, Submission, TaskAssignment
from tasks.tests.factories import TaskFactory
from users.tests.factories import UserFactory


class TestSubmissionReviewWorkflow(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = OrganizationFactory()
        cls.project = ProjectFactory(organization=cls.organization)
        cls.manager = cls.organization.created_by
        cls.annotator = UserFactory(active_organization=cls.organization)
        cls.reviewer = UserFactory(active_organization=cls.organization)
        cls.other_user = UserFactory(active_organization=cls.organization)

        ProjectMember.objects.create(
            project=cls.project,
            user=cls.annotator,
            role=ProjectMember.Role.ANNOTATOR,
        )
        ProjectMember.objects.create(
            project=cls.project,
            user=cls.reviewer,
            role=ProjectMember.Role.REVIEWER,
        )

    def setUp(self):
        self.task = TaskFactory(project=self.project)
        self.assignment = TaskAssignment.objects.create(
            task=self.task,
            project=self.project,
            assignee=self.annotator,
            assigned_by=self.manager,
        )

    def _submit_new_annotation(self, result=None):
        self.client.force_authenticate(user=self.annotator)
        response = self.client.post(
            f'/api/tasks/{self.task.id}/annotations/',
            data={
                'result': result or [],
                'assignment_id': self.assignment.id,
                'assignment_version': self.assignment.version,
                'submit_for_review': True,
            },
            format='json',
        )
        assert response.status_code == 201, response.json()
        self.assignment.refresh_from_db()
        annotation = Annotation.objects.get(pk=response.json()['id'])
        submission = Submission.objects.get(assignment=self.assignment, revision=1)
        return annotation, submission

    def _review(self, submission, decision='approved', reason=''):
        self.client.force_authenticate(user=self.reviewer)
        return self.client.post(
            f'/api/submissions/{submission.id}/review/',
            data={'decision': decision, 'reason': reason},
            format='json',
        )

    def test_submit_creates_immutable_revision_and_bumps_assignment_version(self):
        original_result = [
            {
                'id': 'r1',
                'from_name': 'label',
                'to_name': 'image',
                'type': 'rectanglelabels',
                'value': {'rectanglelabels': ['Car']},
            }
        ]
        annotation, submission = self._submit_new_annotation(original_result)

        assert submission.status == Submission.Status.PENDING
        assert submission.revision == 1
        assert submission.submitted_by_id == self.annotator.id
        assert submission.result_snapshot['annotation']['result'] == original_result
        assert len(submission.result_hash) == 64
        assert self.assignment.version == 2

        self.client.force_authenticate(user=self.annotator)
        edit_response = self.client.patch(
            f'/api/annotations/{annotation.id}/',
            data={
                'result': [],
                'assignment_id': self.assignment.id,
                'assignment_version': self.assignment.version,
                'submit_for_review': False,
            },
            format='json',
        )
        assert edit_response.status_code == 200

        submission.refresh_from_db()
        assert submission.result_snapshot['annotation']['result'] == original_result
        assert submission.status == Submission.Status.PENDING


    def test_multipart_false_submit_flag_does_not_create_revision(self):
        annotation, first = self._submit_new_annotation()
        self.assignment.refresh_from_db()

        self.client.force_authenticate(user=self.annotator)
        response = self.client.patch(
            f'/api/annotations/{annotation.id}/',
            data={
                'result': '[]',
                'assignment_id': str(self.assignment.id),
                'assignment_version': str(self.assignment.version),
                'submit_for_review': 'false',
            },
            format='multipart',
        )

        assert response.status_code == 200, response.json()
        assert Submission.objects.filter(assignment=self.assignment).count() == 1
        first.refresh_from_db()
        assert first.status == Submission.Status.PENDING

    def test_submission_snapshot_preserves_exact_label_config(self):
        original_label_config = self.project.label_config
        _, submission = self._submit_new_annotation()

        assert submission.result_snapshot['project']['label_config'] == original_label_config
        assert submission.result_snapshot['project']['label_config_hash'] == self.project.label_config_hash

        self.project.label_config = '<View><Text name="changed" value="$text"/></View>'
        self.project.save(update_fields=['label_config'])

        submission.refresh_from_db()
        assert submission.result_snapshot['project']['label_config'] == original_label_config

    def test_formal_submission_disables_mutable_export_paths(self):
        snapshot = Export.objects.create(project=self.project, created_by=self.manager)
        _, submission = self._submit_new_annotation()

        self.client.force_authenticate(user=self.manager)

        sync_export = self.client.get(f'/api/projects/{self.project.id}/export')
        formats = self.client.get(f'/api/projects/{self.project.id}/export/formats')
        snapshot_list = self.client.get(f'/api/projects/{self.project.id}/exports/')
        snapshot_detail = self.client.get(f'/api/projects/{self.project.id}/exports/{snapshot.id}')
        snapshot_convert = self.client.post(
            f'/api/projects/{self.project.id}/exports/{snapshot.id}/convert',
            data={'export_type': 'JSON'},
            format='json',
        )
        legacy_files = self.client.get(f'/api/projects/{self.project.id}/export/files')
        legacy_auth = self.client.get(
            '/api/auth/export/',
            HTTP_X_ORIGINAL_URI=f'/export/{self.project.id}-legacy.json',
        )

        assert sync_export.status_code == 400
        assert formats.status_code == 400
        assert formats.json()['display_context']['reason'] == 'IMMUTABLE_RELEASE_REQUIRED'
        assert sync_export.json()['display_context']['reason'] == 'IMMUTABLE_RELEASE_REQUIRED'
        assert snapshot_list.status_code == 400
        assert snapshot_detail.status_code == 400
        assert snapshot_convert.status_code == 400
        assert legacy_files.status_code == 400
        assert legacy_auth.status_code == 400

        release_pending = self.client.get(f'/api/submissions/{submission.id}/release/')
        assert release_pending.status_code == 400

    def test_formats_rejection_does_not_disclose_formal_submissions_to_non_managers(self):
        self._submit_new_annotation()
        for user in (self.other_user, self.annotator, self.reviewer):
            with self.subTest(user=user.pk):
                self.client.force_authenticate(user=user)
                response = self.client.get(f'/api/projects/{self.project.id}/export/formats')
                assert response.status_code in (403, 404)
                assert 'IMMUTABLE_RELEASE_REQUIRED' not in response.content.decode()


    def test_submission_rejects_annotation_relation_swap_and_rolls_back(self):
        annotation, first = self._submit_new_annotation()
        other_project = ProjectFactory(organization=self.organization)
        other_task = TaskFactory(project=other_project)
        self.assignment.refresh_from_db()

        self.client.force_authenticate(user=self.annotator)
        response = self.client.patch(
            f'/api/annotations/{annotation.id}/',
            data={
                'task': other_task.id,
                'project': other_project.id,
                'result': [],
                'assignment_id': self.assignment.id,
                'assignment_version': self.assignment.version,
                'submit_for_review': True,
            },
            format='json',
        )

        assert response.status_code == 400
        annotation.refresh_from_db()
        assert annotation.task_id == self.task.id
        assert annotation.project_id == self.project.id
        assert Submission.objects.filter(assignment=self.assignment).count() == 1
        first.refresh_from_db()
        assert first.status == Submission.Status.PENDING

    def test_export_storage_sync_fails_closed_after_formal_submission(self):
        AzureBlobExportStorageFactory(project=self.project)
        self._submit_new_annotation()
        storage = self.project.io_storages_azureblobexportstorages.get()

        with self.assertRaises(ValidationError):
            storage.sync()


    def test_local_files_post_commit_failure_is_isolated(self):
        annotation = Annotation.objects.create(
            task=self.task,
            project=self.project,
            completed_by=self.annotator,
            updated_by=self.annotator,
            result=[],
        )
        storage = LocalFilesExportStorage.objects.create(
            project=self.project,
            path='/tmp/label-studio-missing-export-dir',
        )

        from unittest.mock import patch

        with patch.object(storage.__class__, 'save_annotation', side_effect=OSError('disk full')):
            _export_annotation_to_local_files(annotation.id)

        assert Annotation.objects.filter(pk=annotation.id).exists()


    def test_snapshot_worker_rechecks_release_boundary_after_queueing(self):
        snapshot = Export.objects.create(project=self.project, created_by=self.manager)
        self._submit_new_annotation()

        from unittest.mock import patch

        with patch.object(snapshot, 'get_export_data') as get_export_data:
            snapshot.export_to_file()

        snapshot.refresh_from_db()
        assert snapshot.status == Export.Status.FAILED
        get_export_data.assert_not_called()

    def test_redis_post_commit_failure_is_isolated(self):
        annotation = Annotation.objects.create(
            task=self.task,
            project=self.project,
            completed_by=self.annotator,
            updated_by=self.annotator,
            result=[],
        )
        storage = RedisExportStorage.objects.create(project=self.project)

        from unittest.mock import patch

        with patch.object(storage.__class__, 'save_annotation', side_effect=OSError('redis down')):
            _export_annotation_to_redis_storages(annotation.id)

        assert Annotation.objects.filter(pk=annotation.id).exists()


    def test_cloud_post_commit_dispatch_failure_is_isolated(self):
        from unittest.mock import patch

        with patch(
            'io_storages.s3.models.start_job_async_or_sync',
            side_effect=RuntimeError('queue unavailable'),
        ):
            _dispatch_s3_annotation_export(123456)


    def test_blocked_export_storage_create_does_not_persist(self):
        self._submit_new_annotation()

        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        model = AzureBlobExportStorageFactory._meta.model
        serializer = SimpleNamespace(
            validated_data={'project': self.project},
            Meta=SimpleNamespace(model=model),
            save=Mock(),
        )
        raw_request = APIRequestFactory().post('/api/storages/export/test/', {}, format='json')
        force_authenticate(raw_request, user=self.manager)
        view = ExportStorageListAPI()
        view.request = view.initialize_request(raw_request)

        with patch.object(model, 'validate_connection') as validate_connection:
            with self.assertRaises(ValidationError):
                view.perform_create(serializer)

        serializer.save.assert_not_called()
        validate_connection.assert_not_called()

    def test_s3_annotation_delete_cleanup_receiver_is_preserved(self):
        annotation = Annotation.objects.create(
            task=self.task,
            project=self.project,
            completed_by=self.annotator,
            updated_by=self.annotator,
            result=[],
        )
        storage = S3ExportStorage.objects.create(project=self.project, can_delete_objects=True)
        S3ExportStorageLink.objects.create(storage=storage, annotation=annotation)

        from unittest.mock import patch

        with patch.object(S3ExportStorage, 'delete_annotation') as delete_annotation:
            annotation.delete()

        delete_annotation.assert_called_once()


    def test_export_conversion_rechecks_release_boundary(self):
        snapshot = Export.objects.create(project=self.project, created_by=self.manager)
        self._submit_new_annotation()

        from unittest.mock import patch

        with patch('data_export.mixins.Converter') as converter:
            with self.assertRaises(ValueError):
                snapshot.convert_file('JSON')

        converter.assert_not_called()


    def test_export_storage_auto_sync_runs_after_commit(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        model = AzureBlobExportStorageFactory._meta.model
        storage = model(project=self.project)
        storage.id = 987654
        serializer = SimpleNamespace(
            validated_data={'project': self.project},
            Meta=SimpleNamespace(model=model),
            save=Mock(return_value=storage),
        )
        raw_request = APIRequestFactory().post('/api/storages/export/test/', {}, format='json')
        force_authenticate(raw_request, user=self.manager)
        view = ExportStorageListAPI()
        view.request = view.initialize_request(raw_request)

        with (
            patch.object(model, 'validate_connection'),
            patch.object(model.objects, 'get', return_value=storage) as get_storage,
            patch.object(storage, 'sync') as sync,
            self.captureOnCommitCallbacks(execute=True),
        ):
            view.perform_create(serializer)

        serializer.save.assert_called_once()
        get_storage.assert_called_once_with(pk=storage.id)
        sync.assert_called_once()


    def test_export_storage_auto_sync_failure_is_isolated_and_marked_failed(self):
        from unittest.mock import Mock, patch

        storage = AzureBlobExportStorageFactory(project=self.project)
        storage.info_set_failed = Mock()

        with (
            patch.object(storage.__class__.objects, 'get', return_value=storage),
            patch.object(storage.__class__.objects, 'filter') as storage_filter,
            patch.object(storage, 'sync', side_effect=RuntimeError('queue unavailable')),
        ):
            storage_filter.return_value.first.return_value = storage
            _run_export_storage_auto_sync(storage.__class__, storage.id)

        storage.info_set_failed.assert_called_once()


    def test_export_storage_batch_failure_does_not_report_completed(self):
        storage = AzureBlobExportStorageFactory(project=self.project)
        storage.status = storage.Status.QUEUED
        storage.save(update_fields=['status'])
        annotations = Annotation.objects.filter(pk__in=[])

        from unittest.mock import Mock, patch

        failed_future = Mock()
        failed_future.result.side_effect = RuntimeError('provider failed')
        executor = Mock()
        executor.__enter__ = Mock(return_value=executor)
        executor.__exit__ = Mock(return_value=False)
        executor.submit.return_value = failed_future

        annotation = Annotation.objects.create(
            task=self.task,
            project=self.project,
            completed_by=self.annotator,
            updated_by=self.annotator,
            result=[],
        )
        annotations = Annotation.objects.filter(pk=annotation.pk)

        with (
            patch('io_storages.base_models.ThreadPoolExecutor', return_value=executor),
            patch('io_storages.base_models.concurrent.futures.as_completed', return_value=[failed_future]),
            patch.object(storage, 'info_set_completed') as completed,
        ):
            with self.assertRaises(RuntimeError):
                storage.save_annotations(annotations)

        completed.assert_not_called()

    def test_sync_conversion_boundary_rejection_marks_failed(self):
        snapshot = Export.objects.create(project=self.project, created_by=self.manager)
        converted = ConvertedFormat.objects.create(
            export=snapshot,
            project=self.project,
            organization=self.organization,
            export_type='JSON',
        )
        self._submit_new_annotation()

        async_convert(
            converted.id,
            'JSON',
            self.project,
            'http://testserver/',
        )

        converted.refresh_from_db()
        assert converted.status == ConvertedFormat.Status.FAILED
        assert 'disabled after formal submissions exist' in converted.traceback

    def test_resubmit_supersedes_old_pending_revision(self):
        annotation, first = self._submit_new_annotation()

        self.client.force_authenticate(user=self.annotator)
        response = self.client.patch(
            f'/api/annotations/{annotation.id}/',
            data={
                'result': [],
                'assignment_id': self.assignment.id,
                'assignment_version': self.assignment.version,
                'submit_for_review': True,
            },
            format='json',
        )

        assert response.status_code == 200
        first.refresh_from_db()
        second = Submission.objects.get(assignment=self.assignment, revision=2)
        assert first.status == Submission.Status.SUPERSEDED
        assert second.status == Submission.Status.PENDING
        assert second.submitted_by_id == self.annotator.id

    def test_reviewer_identity_is_server_controlled(self):
        _, submission = self._submit_new_annotation()

        self.client.force_authenticate(user=self.reviewer)
        response = self.client.post(
            f'/api/submissions/{submission.id}/review/',
            data={
                'decision': 'approved',
                'reviewer': self.other_user.id,
                'created_at': '2000-01-01T00:00:00Z',
            },
            format='json',
        )

        assert response.status_code == 200, response.json()
        review = ReviewDecision.objects.get(submission=submission)
        assert review.reviewer_id == self.reviewer.id
        submission.refresh_from_db()
        assert submission.status == Submission.Status.APPROVED

    def test_rejection_requires_reason(self):
        _, submission = self._submit_new_annotation()

        response = self._review(submission, decision='rejected', reason='')

        assert response.status_code == 400
        assert not ReviewDecision.objects.filter(submission=submission).exists()

    def test_annotator_cannot_review_submission(self):
        _, submission = self._submit_new_annotation()
        self.client.force_authenticate(user=self.annotator)

        response = self.client.post(
            f'/api/submissions/{submission.id}/review/',
            data={'decision': 'approved'},
            format='json',
        )

        assert response.status_code == 403

    def test_self_review_is_rejected_even_for_reviewer_role(self):
        submission = Submission.objects.create(
            assignment=self.assignment,
            annotation=None,
            revision=1,
            result_snapshot={'annotation': {'result': []}},
            result_hash='a' * 64,
            submitted_by=self.reviewer,
        )
        self.client.force_authenticate(user=self.reviewer)

        response = self.client.post(
            f'/api/submissions/{submission.id}/review/',
            data={'decision': 'approved'},
            format='json',
        )

        assert response.status_code == 400
        assert not ReviewDecision.objects.filter(submission=submission).exists()

    def test_only_approved_submission_can_be_released(self):
        _, submission = self._submit_new_annotation()
        self.client.force_authenticate(user=self.manager)

        pending_response = self.client.get(f'/api/submissions/{submission.id}/release/')
        assert pending_response.status_code == 400

        review_response = self._review(submission)
        assert review_response.status_code == 200

        self.client.force_authenticate(user=self.manager)
        release_response = self.client.get(f'/api/submissions/{submission.id}/release/')
        assert release_response.status_code == 200
        assert release_response.json()['submission_id'] == submission.id
        assert release_response.json()['result_hash'] == submission.result_hash

    def test_edit_after_approval_creates_new_unapproved_revision(self):
        annotation, first = self._submit_new_annotation()
        review_response = self._review(first)
        assert review_response.status_code == 200

        first.refresh_from_db()
        assert first.status == Submission.Status.APPROVED

        self.client.force_authenticate(user=self.annotator)
        self.assignment.refresh_from_db()
        response = self.client.patch(
            f'/api/annotations/{annotation.id}/',
            data={
                'result': [
                    {
                        'id': 'new',
                        'from_name': 'label',
                        'to_name': 'image',
                        'type': 'rectanglelabels',
                        'value': {'rectanglelabels': ['Person']},
                    }
                ],
                'assignment_id': self.assignment.id,
                'assignment_version': self.assignment.version,
                'submit_for_review': True,
            },
            format='json',
        )
        assert response.status_code == 200

        first.refresh_from_db()
        second = Submission.objects.get(assignment=self.assignment, revision=2)
        assert first.status == Submission.Status.APPROVED
        assert second.status == Submission.Status.PENDING
        assert not hasattr(second, 'review')

        self.client.force_authenticate(user=self.manager)
        release_latest = self.client.get(f'/api/submissions/{second.id}/release/')
        assert release_latest.status_code == 400

    def test_superseded_submission_cannot_be_reviewed(self):
        annotation, first = self._submit_new_annotation()
        self.client.force_authenticate(user=self.annotator)
        self.client.patch(
            f'/api/annotations/{annotation.id}/',
            data={
                'result': [],
                'assignment_id': self.assignment.id,
                'assignment_version': self.assignment.version,
                'submit_for_review': True,
            },
            format='json',
        )
        first.refresh_from_db()
        assert first.status == Submission.Status.SUPERSEDED

        response = self._review(first)

        assert response.status_code == 400
        assert not ReviewDecision.objects.filter(submission=first).exists()
