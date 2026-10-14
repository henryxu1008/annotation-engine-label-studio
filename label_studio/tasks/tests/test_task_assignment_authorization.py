from unittest.mock import patch

from data_export.models import Export
from organizations.models import OrganizationMember
from organizations.tests.factories import OrganizationFactory
from projects.models import ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.test import APITestCase
from tasks.models import Annotation, AnnotationDraft, ReviewDecision, Submission, TaskAssignment
from tasks.submissions import create_submission
from tasks.tests.factories import TaskFactory
from users.tests.factories import UserFactory


class TestTaskAssignmentAuthorization(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = OrganizationFactory()
        cls.project = ProjectFactory(organization=cls.organization)
        cls.manager = cls.organization.created_by
        cls.annotator_a = UserFactory(active_organization=cls.organization)
        cls.annotator_b = UserFactory(active_organization=cls.organization)
        cls.reviewer = UserFactory(active_organization=cls.organization)

        ProjectMember.objects.create(
            project=cls.project,
            user=cls.annotator_a,
            role=ProjectMember.Role.ANNOTATOR,
        )
        ProjectMember.objects.create(
            project=cls.project,
            user=cls.annotator_b,
            role=ProjectMember.Role.ANNOTATOR,
        )
        ProjectMember.objects.create(
            project=cls.project,
            user=cls.reviewer,
            role=ProjectMember.Role.REVIEWER,
        )

        cls.shared_task = TaskFactory(project=cls.project)
        cls.other_task = TaskFactory(project=cls.project)

        cls.assignment_a = TaskAssignment.objects.create(
            task=cls.shared_task,
            project=cls.project,
            assignee=cls.annotator_a,
            assigned_by=cls.manager,
        )
        cls.assignment_b = TaskAssignment.objects.create(
            task=cls.shared_task,
            project=cls.project,
            assignee=cls.annotator_b,
            assigned_by=cls.manager,
        )
        cls.other_assignment = TaskAssignment.objects.create(
            task=cls.other_task,
            project=cls.project,
            assignee=cls.annotator_b,
            assigned_by=cls.manager,
        )

    def _create_annotation(self, user, task):
        assignment = TaskAssignment.objects.get(
            task=task,
            assignee=user,
            status__in=TaskAssignment.ACTIVE_STATUSES,
        )
        self.client.force_authenticate(user=user)
        response = self.client.post(
            f'/api/tasks/{task.id}/annotations/',
            data={
                'result': [],
                'assignment_id': assignment.id,
                'assignment_version': assignment.version,
            },
            format='json',
        )
        assert response.status_code == 201, response.json()
        return Annotation.objects.get(pk=response.json()['id'])

    def test_task_editor_capability_requires_the_current_actors_active_assignment(self):
        self.client.force_authenticate(user=self.manager)
        manager_view = self.client.get(f'/api/tasks/{self.shared_task.pk}/')
        assert manager_view.status_code == 200
        assert manager_view.json()['can_label'] is False
        denied = self.client.post(f'/api/tasks/{self.shared_task.pk}/annotations/', {'result': []}, format='json')
        assert denied.status_code == 403

        self.client.force_authenticate(user=self.annotator_a)
        assigned_view = self.client.get(f'/api/tasks/{self.shared_task.pk}/')
        assert assigned_view.status_code == 200
        assert assigned_view.json()['can_label'] is True
        assert assigned_view.json()['assignment_id'] == self.assignment_a.pk

        TaskAssignment.objects.filter(pk=self.assignment_a.pk).update(status=TaskAssignment.Status.CANCELLED)
        revoked = self.client.get(f'/api/tasks/{self.shared_task.pk}/')
        assert revoked.status_code == 404
        self.client.force_authenticate(user=self.manager)
        assert self.client.get(f'/api/tasks/{self.shared_task.pk}/').json()['can_label'] is False

    def _next_toolbar_task(self, user, *, project=None, selected=None, actor_fields=None):
        self.client.force_authenticate(user=user)
        data = {'selectedItems': selected or {'all': True, 'excluded': []}}
        data.update(actor_fields or {})
        return self.client.post(
            f'/api/dm/actions/?project={(project or self.project).id}&id=next_task',
            data=data,
            format='json',
        )

    def test_labeling_toolbar_next_task_uses_annotator_assignment_scope(self):
        response = self._next_toolbar_task(self.annotator_a)
        assert response.status_code == 200, response.data
        assert response.data['id'] == self.shared_task.id

    def test_labeling_toolbar_next_task_preserves_manager_access(self):
        response = self._next_toolbar_task(self.manager)
        assert response.status_code == 200, response.data
        assert response.data['id'] in (self.shared_task.id, self.other_task.id)

    def test_labeling_toolbar_empty_queue_has_explicit_reason(self):
        empty_project = ProjectFactory(organization=self.organization, created_by=self.manager)
        ProjectMember.objects.create(project=empty_project, user=self.annotator_a, role=ProjectMember.Role.ANNOTATOR)
        response = self._next_toolbar_task(self.annotator_a, project=empty_project)
        assert response.status_code == 404
        assert response.data['display_context']['reason'] == 'QUEUE_EXHAUSTED'

    def test_labeling_toolbar_invisible_project_is_not_an_empty_queue(self):
        other_project = ProjectFactory(organization=self.organization, created_by=self.annotator_b)
        response = self._next_toolbar_task(self.annotator_a, project=other_project)
        assert response.status_code == 404
        assert response.data.get('display_context', {}).get('reason') != 'QUEUE_EXHAUSTED'

    def test_labeling_toolbar_selected_ids_cannot_include_another_annotators_task(self):
        response = self._next_toolbar_task(
            self.annotator_a, selected={'all': False, 'included': [self.other_task.id]},
        )
        assert response.status_code == 404

    def test_labeling_toolbar_selected_ids_cannot_include_unassigned_task(self):
        unassigned = TaskFactory(project=self.project)
        response = self._next_toolbar_task(
            self.annotator_a, selected={'all': False, 'included': [unassigned.id]},
        )
        assert response.status_code == 404

    def test_labeling_toolbar_next_task_does_not_trust_actor_fields(self):
        response = self._next_toolbar_task(
            self.annotator_a,
            actor_fields={'user_id': self.manager.id, 'actor_id': self.annotator_b.id, 'assignee_id': self.annotator_b.id},
        )
        assert response.status_code == 200, response.data
        assert response.data['id'] == self.shared_task.id

    def test_labeling_toolbar_reviewer_cannot_enter_annotation_queue(self):
        response = self._next_toolbar_task(self.reviewer)
        assert response.status_code in (403, 404)

    def test_labeling_toolbar_next_task_rechecks_revoked_membership(self):
        response = self._next_toolbar_task(self.annotator_a)
        assert response.status_code == 200
        ProjectMember.objects.filter(project=self.project, user=self.annotator_a).update(enabled=False)
        response = self._next_toolbar_task(self.annotator_a)
        assert response.status_code in (403, 404)

    def test_labeling_toolbar_next_task_rechecks_cancelled_assignment_after_lock(self):
        response = self._next_toolbar_task(self.annotator_a)
        assert response.status_code == 200
        self.client.force_authenticate(user=self.manager)
        assert self.client.delete(f'/api/task-assignments/{self.assignment_a.id}/').status_code == 204
        response = self._next_toolbar_task(self.annotator_a)
        assert response.status_code == 404

    def test_labeling_toolbar_next_task_cannot_cross_project_scope(self):
        other_project = ProjectFactory(organization=self.organization, created_by=self.annotator_b)
        other_project_task = TaskFactory(project=other_project)
        response = self._next_toolbar_task(
            self.annotator_a, selected={'all': False, 'included': [other_project_task.id]},
        )
        assert response.status_code == 404
        response = self._next_toolbar_task(self.annotator_a, project=other_project)
        assert response.status_code in (403, 404)

    def test_labeling_toolbar_rechecks_object_access_after_sampling(self):
        def revoke_after_sampling(*args):
            ProjectMember.objects.filter(project=self.project, user=self.annotator_a).update(enabled=False)
            return self.shared_task, {}

        with patch('data_manager.actions.next_task.get_next_task', side_effect=revoke_after_sampling):
            response = self._next_toolbar_task(self.annotator_a)
        assert response.status_code in (403, 404)

    def test_labeling_toolbar_rejects_out_of_project_sampler_result(self):
        other_project = ProjectFactory(organization=self.organization, created_by=self.annotator_a)
        other_task = TaskFactory(project=other_project)
        with patch('data_manager.actions.next_task.get_next_task', return_value=(other_task, {})):
            response = self._next_toolbar_task(self.annotator_a)
        assert response.status_code in (403, 404)


    def test_cancelled_assignment_blocks_annotation_update_and_delete(self):
        annotation = self._create_annotation(self.annotator_a, self.shared_task)
        old_version = self.assignment_a.version

        self.client.force_authenticate(user=self.manager)
        cancel_response = self.client.delete(f'/api/task-assignments/{self.assignment_a.id}/')
        assert cancel_response.status_code == 204

        self.client.force_authenticate(user=self.annotator_a)
        update_response = self.client.patch(
            f'/api/annotations/{annotation.id}/',
            data={
                'result': [{'value': {'choices': ['stale']}}],
                'assignment_id': self.assignment_a.id,
                'assignment_version': old_version,
            },
            format='json',
        )
        delete_response = self.client.delete(f'/api/annotations/{annotation.id}/')

        assert update_response.status_code in (403, 404, 409)
        assert delete_response.status_code in (403, 404, 409)
        annotation.refresh_from_db()
        assert annotation.result == []

    def test_cancelled_assignment_blocks_draft_creation(self):
        task = TaskFactory(project=self.project)
        assignment = TaskAssignment.objects.create(
            task=task,
            project=self.project,
            assignee=self.annotator_a,
            assigned_by=self.manager,
        )
        old_version = assignment.version

        self.client.force_authenticate(user=self.manager)
        cancel_response = self.client.delete(f'/api/task-assignments/{assignment.id}/')
        assert cancel_response.status_code == 204

        self.client.force_authenticate(user=self.annotator_a)
        response = self.client.post(
            f'/api/tasks/{task.id}/drafts',
            data={
                'result': [],
                'lead_time': 0,
                'assignment_id': assignment.id,
                'assignment_version': old_version,
            },
            format='json',
        )

        assert response.status_code in (403, 404, 409)
        assert not AnnotationDraft.objects.filter(task=task, user=self.annotator_a).exists()

    def test_manager_cannot_convert_assignee_annotation_to_draft(self):
        annotation = self._create_annotation(self.annotator_a, self.shared_task)

        self.client.force_authenticate(user=self.manager)
        response = self.client.post(f'/api/annotations/{annotation.id}/convert-to-draft', data={}, format='json')

        assert response.status_code in (403, 409)
        assert Annotation.objects.filter(pk=annotation.id).exists()
        assert not AnnotationDraft.objects.filter(task=self.shared_task, user=self.manager).exists()

    def test_assignee_paginated_total_annotations_excludes_other_assignees(self):
        annotation_a = self._create_annotation(self.annotator_a, self.shared_task)
        annotation_b = self._create_annotation(self.annotator_b, self.shared_task)

        self.client.force_authenticate(user=self.annotator_a)
        response = self.client.get(f'/api/tasks/?project={self.project.id}&page=1&page_size=100')

        assert response.status_code == 200
        payload = response.json()
        assert payload['total_annotations'] == 1
        task_payload = next(item for item in payload['tasks'] if item['id'] == self.shared_task.id)
        assert task_payload['total_annotations'] == 1
        assert annotation_a.id != annotation_b.id

    def test_snapshot_exports_are_manager_only(self):
        snapshot = Export.objects.create(project=self.project, created_by=self.manager)

        self.client.force_authenticate(user=self.annotator_a)
        annotator_response = self.client.get(f'/api/projects/{self.project.id}/exports/')
        annotator_convert = self.client.post(
            f'/api/projects/{self.project.id}/exports/{snapshot.id}/convert',
            data={'export_type': 'JSON'},
            format='json',
        )

        self.client.force_authenticate(user=self.reviewer)
        reviewer_response = self.client.get(f'/api/projects/{self.project.id}/exports/')
        reviewer_convert = self.client.post(
            f'/api/projects/{self.project.id}/exports/{snapshot.id}/convert',
            data={'export_type': 'JSON'},
            format='json',
        )

        self.client.force_authenticate(user=self.manager)
        manager_response = self.client.get(f'/api/projects/{self.project.id}/exports/')

        assert annotator_response.status_code in (403, 404)
        assert annotator_convert.status_code in (403, 404)
        assert reviewer_response.status_code in (403, 404)
        assert reviewer_convert.status_code in (403, 404)
        assert manager_response.status_code == 200

    def test_non_manager_storage_create_is_denied_before_connection_validation(self):
        for user in (self.annotator_a, self.reviewer):
            self.client.force_authenticate(user=user)
            with (
                patch('io_storages.s3.models.S3ImportStorage.validate_connection') as import_validate,
                patch('io_storages.s3.models.S3ExportStorage.validate_connection') as export_validate,
            ):
                import_response = self.client.post(
                    '/api/storages/s3/',
                    data={
                        'project': self.project.id,
                        'bucket': 'authorization-test-import',
                        'title': 'authorization-test-import',
                    },
                    format='json',
                )
                export_response = self.client.post(
                    '/api/storages/export/s3',
                    data={
                        'project': self.project.id,
                        'bucket': 'authorization-test-export',
                        'title': 'authorization-test-export',
                    },
                    format='json',
                )

            assert import_response.status_code == 403
            assert export_response.status_code == 403
            import_validate.assert_not_called()
            export_validate.assert_not_called()


    def test_storage_validate_is_denied_before_serializer_or_connection_validation(self):
        self.client.force_authenticate(user=self.annotator_a)

        with (
            patch('io_storages.s3.serializers.S3ImportStorageSerializer.validate') as serializer_validate,
            patch('io_storages.s3.models.S3ImportStorage.validate_connection') as validate_connection,
        ):
            response = self.client.post(
                '/api/storages/s3/validate',
                data={
                    'project': self.project.id,
                    'bucket': 'authorization-test-validate',
                    'title': 'authorization-test-validate',
                },
                format='json',
            )

        assert response.status_code == 403
        serializer_validate.assert_not_called()
        validate_connection.assert_not_called()

    def test_storage_list_files_is_denied_before_serializer_or_connection_validation(self):
        self.client.force_authenticate(user=self.annotator_a)

        with (
            patch('io_storages.s3.serializers.S3ImportStorageSerializer.validate') as serializer_validate,
            patch('io_storages.s3.models.S3ImportStorage.validate_connection') as validate_connection,
        ):
            response = self.client.post(
                '/api/storages/s3/files',
                data={
                    'project': self.project.id,
                    'bucket': 'authorization-test-files',
                    'title': 'authorization-test-files',
                },
                format='json',
            )

        assert response.status_code == 403
        serializer_validate.assert_not_called()
        validate_connection.assert_not_called()


    def test_existing_storage_validate_rejects_submitted_foreign_project_before_serializer_validation(self):
        other_manager = UserFactory(active_organization=self.organization)
        self.organization.add_user(other_manager)
        other_project = ProjectFactory(
            organization=self.organization,
            created_by=other_manager,
        )
        ProjectMember.objects.create(
            project=other_project,
            user=self.annotator_a,
            role=ProjectMember.Role.ANNOTATOR,
            enabled=True,
        )

        from io_storages.s3.models import S3ImportStorage

        storage = S3ImportStorage.objects.create(
            project=self.project,
            bucket='authorized-storage',
            title='authorized-storage',
        )

        self.client.force_authenticate(user=self.manager)
        with (
            patch('io_storages.s3.serializers.S3ImportStorageSerializer.validate') as serializer_validate,
            patch('io_storages.s3.models.S3ImportStorage.validate_connection') as validate_connection,
        ):
            response = self.client.post(
                '/api/storages/s3/validate',
                data={
                    'id': storage.id,
                    'project': other_project.id,
                    'bucket': 'attacker-controlled',
                    'title': 'attacker-controlled',
                },
                format='json',
            )

        assert response.status_code in (403, 404)
        serializer_validate.assert_not_called()
        validate_connection.assert_not_called()

    def test_storage_create_foreign_project_id_is_denied_before_connection_validation(self):
        other_creator = UserFactory(active_organization=self.organization)
        self.organization.add_user(other_creator)
        other_project = ProjectFactory(
            organization=self.organization,
            created_by=other_creator,
        )

        self.client.force_authenticate(user=self.annotator_a)
        with (
            patch('io_storages.s3.models.S3ImportStorage.validate_connection') as import_validate,
            patch('io_storages.s3.models.S3ExportStorage.validate_connection') as export_validate,
        ):
            import_response = self.client.post(
                '/api/storages/s3/',
                data={
                    'project': other_project.id,
                    'bucket': 'foreign-project-import',
                    'title': 'foreign-project-import',
                },
                format='json',
            )
            export_response = self.client.post(
                '/api/storages/export/s3',
                data={
                    'project': other_project.id,
                    'bucket': 'foreign-project-export',
                    'title': 'foreign-project-export',
                },
                format='json',
            )

        assert import_response.status_code == 404
        assert export_response.status_code == 404
        import_validate.assert_not_called()
        export_validate.assert_not_called()

    def test_storage_create_rejects_malformed_project_id_before_connection_validation(self):
        self.client.force_authenticate(user=self.annotator_a)

        for malformed_project in ('abc', [], {}):
            with (
                patch('io_storages.s3.models.S3ImportStorage.validate_connection') as import_validate,
                patch('io_storages.s3.models.S3ExportStorage.validate_connection') as export_validate,
            ):
                import_response = self.client.post(
                    '/api/storages/s3/',
                    data={
                        'project': malformed_project,
                        'bucket': 'malformed-project-import',
                        'title': 'malformed-project-import',
                    },
                    format='json',
                )
                export_response = self.client.post(
                    '/api/storages/export/s3',
                    data={
                        'project': malformed_project,
                        'bucket': 'malformed-project-export',
                        'title': 'malformed-project-export',
                    },
                    format='json',
                )

            assert import_response.status_code == 400
            assert export_response.status_code == 400
            import_validate.assert_not_called()
            export_validate.assert_not_called()

    def test_storage_create_rejects_non_object_body_before_connection_validation(self):
        self.client.force_authenticate(user=self.annotator_a)

        for malformed_body in ([], 'abc', 7):
            with (
                patch('io_storages.s3.models.S3ImportStorage.validate_connection') as import_validate,
                patch('io_storages.s3.models.S3ExportStorage.validate_connection') as export_validate,
            ):
                import_response = self.client.post(
                    '/api/storages/s3/',
                    data=malformed_body,
                    format='json',
                )
                export_response = self.client.post(
                    '/api/storages/export/s3',
                    data=malformed_body,
                    format='json',
                )

            assert import_response.status_code == 400
            assert export_response.status_code == 400
            import_validate.assert_not_called()
            export_validate.assert_not_called()

    def test_assignee_can_open_assigned_task_but_not_someone_elses_task(self):
        self.client.force_authenticate(user=self.annotator_a)

        assigned = self.client.get(f'/api/tasks/{self.shared_task.id}/')
        unassigned = self.client.get(f'/api/tasks/{self.other_task.id}/')

        assert assigned.status_code == 200
        assert assigned.json()['assignment_id'] == self.assignment_a.id
        assert assigned.json()['assignment_version'] == self.assignment_a.version
        assert unassigned.status_code == 404

    def test_reviewer_can_list_only_reviewable_pending_submissions(self):
        annotation = self._create_annotation(self.annotator_a, self.shared_task)
        submission = create_submission(
            assignment=self.assignment_a,
            annotation=annotation,
            actor=self.annotator_a,
        )
        Submission.objects.create(
            assignment=self.other_assignment,
            annotation=None,
            revision=1,
            result_snapshot={'task': {'id': self.other_task.id}},
            result_hash='0' * 64,
            submitted_by=self.annotator_b,
            status=Submission.Status.APPROVED,
        )

        self.client.force_authenticate(user=self.reviewer)
        response = self.client.get(
            f'/api/submissions/?project={self.project.id}&reviewable=true'
        )

        assert response.status_code == 200
        payload = response.json()
        ids = {item['id'] for item in payload['results']}
        assert ids == {submission.id}
        item = payload['results'][0]
        assert item['revision'] == 1
        assert item['result_hash'] == submission.result_hash
        assert item['result_snapshot'] == submission.result_snapshot

    def test_non_reviewer_cannot_open_reviewable_submission_queue(self):
        annotation = self._create_annotation(self.annotator_a, self.shared_task)
        create_submission(
            assignment=self.assignment_a,
            annotation=annotation,
            actor=self.annotator_a,
        )

        for user in (self.manager, self.annotator_a):
            self.client.force_authenticate(user=user)
            response = self.client.get(
                f'/api/submissions/?project={self.project.id}&reviewable=true'
            )
            assert response.status_code == 403

    def test_reviewable_submission_queue_requires_project(self):
        self.client.force_authenticate(user=self.reviewer)

        response = self.client.get('/api/submissions/?reviewable=true')

        assert response.status_code == 400

    def test_revoked_reviewer_cannot_list_or_review_submission(self):
        annotation = self._create_annotation(self.annotator_a, self.shared_task)
        submission = create_submission(
            assignment=self.assignment_a,
            annotation=annotation,
            actor=self.annotator_a,
        )

        membership = ProjectMember.objects.get(
            project=self.project,
            user=self.reviewer,
        )

        self.client.force_authenticate(user=self.reviewer)
        before = self.client.get(
            f'/api/submissions/?project={self.project.id}&reviewable=true'
        )
        assert before.status_code == 200
        assert submission.id in {item['id'] for item in before.json()['results']}

        membership.enabled = False
        membership.save(update_fields=['enabled'])

        queue_response = self.client.get(
            f'/api/submissions/?project={self.project.id}&reviewable=true'
        )
        review_response = self.client.post(
            f'/api/submissions/{submission.id}/review/',
            data={'decision': 'approved'},
            format='json',
        )

        assert queue_response.status_code in (403, 404)
        assert review_response.status_code == 403
        submission.refresh_from_db()
        assert submission.status == Submission.Status.PENDING

    def test_reviewer_cannot_review_submission_from_other_project_by_id(self):
        other_creator = UserFactory(active_organization=self.organization)
        other_annotator = UserFactory(active_organization=self.organization)
        self.organization.add_user(other_creator)
        self.organization.add_user(other_annotator)

        other_project = ProjectFactory(
            organization=self.organization,
            created_by=other_creator,
        )
        ProjectMember.objects.create(
            project=other_project,
            user=other_annotator,
            role=ProjectMember.Role.ANNOTATOR,
        )

        other_task = TaskFactory(project=other_project)
        other_assignment = TaskAssignment.objects.create(
            task=other_task,
            project=other_project,
            assignee=other_annotator,
            assigned_by=other_creator,
        )

        self.client.force_authenticate(user=other_annotator)
        annotation_response = self.client.post(
            f'/api/tasks/{other_task.id}/annotations/',
            data={
                'result': [],
                'assignment_id': other_assignment.id,
                'assignment_version': other_assignment.version,
            },
            format='json',
        )
        assert annotation_response.status_code == 201, annotation_response.json()
        other_annotation = Annotation.objects.get(pk=annotation_response.json()['id'])
        other_submission = create_submission(
            assignment=other_assignment,
            annotation=other_annotation,
            actor=other_annotator,
        )

        self.client.force_authenticate(user=self.reviewer)
        response = self.client.post(
            f'/api/submissions/{other_submission.id}/review/',
            data={'decision': 'approved'},
            format='json',
        )

        assert response.status_code in (403, 404)
        other_submission.refresh_from_db()
        assert other_submission.status == Submission.Status.PENDING
        assert not ReviewDecision.objects.filter(submission=other_submission).exists()

    def test_reviewer_cannot_swap_project_id_to_read_other_project_queue(self):
        annotation = self._create_annotation(self.annotator_a, self.shared_task)
        create_submission(
            assignment=self.assignment_a,
            annotation=annotation,
            actor=self.annotator_a,
        )

        other_creator = UserFactory(active_organization=self.organization)
        self.organization.add_user(other_creator)
        other_project = ProjectFactory(
            organization=self.organization,
            created_by=other_creator,
        )

        self.client.force_authenticate(user=self.reviewer)
        response = self.client.get(
            f'/api/submissions/?project={other_project.id}&reviewable=true'
        )

        assert response.status_code == 404

    def test_reviewer_capability_is_reviewer_only_and_revocation_sensitive(self):
        self.client.force_authenticate(user=self.reviewer)
        allowed = self.client.get(
            f'/api/projects/{self.project.id}/review-capability/'
        )
        assert allowed.status_code == 200
        assert allowed.json()['can_review'] is True

        membership = ProjectMember.objects.get(
            project=self.project,
            user=self.reviewer,
        )
        membership.enabled = False
        membership.save(update_fields=['enabled'])

        revoked = self.client.get(
            f'/api/projects/{self.project.id}/review-capability/'
        )
        assert revoked.status_code in (403, 404)

        self.client.force_authenticate(user=self.manager)
        manager = self.client.get(
            f'/api/projects/{self.project.id}/review-capability/'
        )
        assert manager.status_code == 403

    def test_manager_releases_exact_approved_submission_snapshot(self):
        snapshot = {
            'annotation': {'id': 123, 'result': [{'value': 'approved'}]},
            'task': {'id': self.shared_task.id},
            'project': {'id': self.project.id},
        }
        submission = Submission.objects.create(
            assignment=self.assignment_a,
            annotation=None,
            revision=41,
            result_snapshot=snapshot,
            result_hash='a' * 64,
            submitted_by=self.annotator_a,
            status=Submission.Status.APPROVED,
        )

        self.client.force_authenticate(user=self.manager)
        response = self.client.get(f'/api/submissions/{submission.id}/release/')

        assert response.status_code == 200
        assert response.json() == {
            'submission_id': submission.id,
            'revision': 41,
            'result_hash': 'a' * 64,
            'result_snapshot': snapshot,
        }

    def test_manager_cannot_release_pending_or_rejected_submission(self):
        pending = Submission.objects.create(
            assignment=self.assignment_a,
            annotation=None,
            revision=42,
            result_snapshot={'revision': 'pending'},
            result_hash='b' * 64,
            submitted_by=self.annotator_a,
            status=Submission.Status.PENDING,
        )
        rejected = Submission.objects.create(
            assignment=self.assignment_a,
            annotation=None,
            revision=43,
            result_snapshot={'revision': 'rejected'},
            result_hash='c' * 64,
            submitted_by=self.annotator_a,
            status=Submission.Status.REJECTED,
        )

        self.client.force_authenticate(user=self.manager)
        pending_response = self.client.get(f'/api/submissions/{pending.id}/release/')
        rejected_response = self.client.get(f'/api/submissions/{rejected.id}/release/')

        assert pending_response.status_code == 400
        assert rejected_response.status_code == 400

    def test_non_manager_cannot_release_approved_submission(self):
        submission = Submission.objects.create(
            assignment=self.assignment_a,
            annotation=None,
            revision=44,
            result_snapshot={'revision': 'approved'},
            result_hash='d' * 64,
            submitted_by=self.annotator_a,
            status=Submission.Status.APPROVED,
        )

        for user in (self.annotator_a, self.reviewer):
            self.client.force_authenticate(user=user)
            response = self.client.get(f'/api/submissions/{submission.id}/release/')
            assert response.status_code == 403

    def test_manager_cannot_release_approved_submission_from_other_project_by_id(self):
        other_creator = UserFactory(active_organization=self.organization)
        other_annotator = UserFactory(active_organization=self.organization)
        self.organization.add_user(other_creator)
        self.organization.add_user(other_annotator)

        other_project = ProjectFactory(
            organization=self.organization,
            created_by=other_creator,
        )
        ProjectMember.objects.create(
            project=other_project,
            user=other_annotator,
            role=ProjectMember.Role.ANNOTATOR,
        )
        other_task = TaskFactory(project=other_project)
        other_assignment = TaskAssignment.objects.create(
            task=other_task,
            project=other_project,
            assignee=other_annotator,
            assigned_by=other_creator,
        )
        other_submission = Submission.objects.create(
            assignment=other_assignment,
            annotation=None,
            revision=1,
            result_snapshot={'task': {'id': other_task.id}},
            result_hash='e' * 64,
            submitted_by=other_annotator,
            status=Submission.Status.APPROVED,
        )

        self.client.force_authenticate(user=self.manager)
        response = self.client.get(f'/api/submissions/{other_submission.id}/release/')

        assert response.status_code == 403

    def test_reviewer_project_membership_does_not_grant_task_access(self):
        self.client.force_authenticate(user=self.reviewer)

        response = self.client.get(f'/api/tasks/{self.shared_task.id}/')

        assert response.status_code == 404

    def test_annotator_cannot_modify_task_metadata(self):
        self.client.force_authenticate(user=self.annotator_a)

        response = self.client.patch(
            f'/api/tasks/{self.shared_task.id}/',
            data={'data': {'text': 'should not change'}},
            format='json',
        )

        assert response.status_code == 403

    def test_each_assignee_only_sees_own_annotation_on_shared_task(self):
        annotation_a = self._create_annotation(self.annotator_a, self.shared_task)
        annotation_b = self._create_annotation(self.annotator_b, self.shared_task)

        self.client.force_authenticate(user=self.annotator_a)
        response = self.client.get(f'/api/tasks/{self.shared_task.id}/')

        assert response.status_code == 200
        annotation_ids = {item['id'] for item in response.json()['annotations']}
        assert annotation_ids == {annotation_a.id}
        assert annotation_b.id not in annotation_ids

    def test_assignee_cannot_update_another_assignees_annotation(self):
        annotation_a = self._create_annotation(self.annotator_a, self.shared_task)
        annotation_b = self._create_annotation(self.annotator_b, self.shared_task)

        self.client.force_authenticate(user=self.annotator_a)
        response = self.client.patch(
            f'/api/annotations/{annotation_b.id}/',
            data={'result': []},
            format='json',
        )

        assert response.status_code == 403
        annotation_a.refresh_from_db()
        annotation_b.refresh_from_db()
        assert annotation_a.completed_by_id == self.annotator_a.id
        assert annotation_b.completed_by_id == self.annotator_b.id

    def test_manager_can_view_but_cannot_edit_assignees_annotation_without_assignment(self):
        annotation_a = self._create_annotation(self.annotator_a, self.shared_task)

        self.client.force_authenticate(user=self.manager)
        get_response = self.client.get(f'/api/annotations/{annotation_a.id}/')
        patch_response = self.client.patch(
            f'/api/annotations/{annotation_a.id}/',
            data={'result': []},
            format='json',
        )

        assert get_response.status_code == 200
        assert patch_response.status_code == 403

    def test_cancelling_assignment_invalidates_old_page_write(self):
        self.client.force_authenticate(user=self.manager)
        cancel_response = self.client.delete(f'/api/task-assignments/{self.assignment_a.id}/')
        assert cancel_response.status_code == 204

        self.assignment_a.refresh_from_db()
        assert self.assignment_a.status == TaskAssignment.Status.CANCELLED
        assert self.assignment_a.version == 2

        self.client.force_authenticate(user=self.annotator_a)
        stale_write = self.client.post(
            f'/api/tasks/{self.shared_task.id}/annotations/',
            data={
                'result': [],
                'assignment_id': self.assignment_a.id,
                'assignment_version': 1,
            },
            format='json',
        )

        assert stale_write.status_code in (403, 404)
        assert not Annotation.objects.filter(task=self.shared_task, completed_by=self.annotator_a).exists()

    def test_old_assignment_token_is_rejected_after_same_user_is_reassigned(self):
        old_assignment = self.assignment_a

        self.client.force_authenticate(user=self.manager)
        cancel_response = self.client.delete(f'/api/task-assignments/{old_assignment.id}/')
        assert cancel_response.status_code == 204

        new_assignment = TaskAssignment.objects.create(
            task=self.shared_task,
            project=self.project,
            assignee=self.annotator_a,
            assigned_by=self.manager,
        )

        self.client.force_authenticate(user=self.annotator_a)
        stale_response = self.client.post(
            f'/api/tasks/{self.shared_task.id}/annotations/',
            data={
                'result': [],
                'assignment_id': old_assignment.id,
                'assignment_version': 1,
            },
            format='json',
        )
        assert stale_response.status_code == 409

        fresh_response = self.client.post(
            f'/api/tasks/{self.shared_task.id}/annotations/',
            data={
                'result': [],
                'assignment_id': new_assignment.id,
                'assignment_version': new_assignment.version,
            },
            format='json',
        )
        assert fresh_response.status_code == 201

    def test_manager_can_list_eligible_assignment_assignees(self):
        disabled = UserFactory(active_organization=self.organization)
        removed = UserFactory(active_organization=self.organization)
        self.organization.add_user(disabled)
        self.organization.add_user(removed)

        ProjectMember.objects.create(
            project=self.project,
            user=disabled,
            role=ProjectMember.Role.ANNOTATOR,
            enabled=False,
        )
        ProjectMember.objects.create(
            project=self.project,
            user=removed,
            role=ProjectMember.Role.ANNOTATOR,
            enabled=True,
        )
        removed_org_membership = OrganizationMember.objects.get(
            organization=self.organization,
            user=removed,
        )
        removed_org_membership.deleted_at = removed_org_membership.created_at
        removed_org_membership.save(update_fields=['deleted_at'])

        self.client.force_authenticate(user=self.manager)

        response = self.client.get(
            f'/api/task-assignments/eligible-assignees/?project={self.project.id}'
        )

        assert response.status_code == 200
        user_ids = {item['id'] for item in response.json()['results']}
        assert self.annotator_a.id in user_ids
        assert self.annotator_b.id in user_ids
        assert self.manager.id in user_ids
        assert self.reviewer.id not in user_ids
        assert disabled.id not in user_ids
        assert removed.id not in user_ids

    def test_eligible_assignees_paginate_across_page_boundary(self):
        bulk_users = []
        for _ in range(55):
            user = UserFactory(active_organization=self.organization)
            self.organization.add_user(user)
            ProjectMember.objects.create(
                project=self.project,
                user=user,
                role=ProjectMember.Role.ANNOTATOR,
                enabled=True,
            )
            bulk_users.append(user)

        self.client.force_authenticate(user=self.manager)
        first_response = self.client.get(
            f'/api/task-assignments/eligible-assignees/?project={self.project.id}&page_size=50&page=1'
        )
        second_response = self.client.get(
            f'/api/task-assignments/eligible-assignees/?project={self.project.id}&page_size=50&page=2'
        )

        assert first_response.status_code == 200
        assert second_response.status_code == 200
        first_page = first_response.json()
        second_page = second_response.json()
        assert first_page['next'] is not None
        assert second_page['previous'] is not None

        returned_ids = {
            item['id']
            for item in first_page['results'] + second_page['results']
        }
        assert {user.id for user in bulk_users}.issubset(returned_ids)

    def test_manager_cannot_swap_project_id_to_read_other_project_assignees(self):
        other_creator = UserFactory(active_organization=self.organization)
        self.organization.add_user(other_creator)
        other_project = ProjectFactory(
            organization=self.organization,
            created_by=other_creator,
        )

        self.client.force_authenticate(user=self.manager)
        response = self.client.get(
            f'/api/task-assignments/eligible-assignees/?project={other_project.id}'
        )

        assert response.status_code == 404

    def test_annotator_cannot_list_eligible_assignment_assignees(self):
        self.client.force_authenticate(user=self.annotator_a)

        response = self.client.get(
            f'/api/task-assignments/eligible-assignees/?project={self.project.id}'
        )

        assert response.status_code == 403

    def test_assignment_list_can_filter_active_only(self):
        self.client.force_authenticate(user=self.manager)
        self.assignment_a.cancel()

        response = self.client.get(
            f'/api/task-assignments/?project={self.project.id}&task={self.shared_task.id}&active=true'
        )

        assert response.status_code == 200
        assignment_ids = {item['id'] for item in response.json()}
        assert self.assignment_a.id not in assignment_ids
        assert self.assignment_b.id in assignment_ids

    def test_assignment_list_keeps_identity_for_ineligible_assignee(self):
        revoked = UserFactory(active_organization=self.organization)
        self.organization.add_user(revoked)
        ProjectMember.objects.create(
            project=self.project,
            user=revoked,
            role=ProjectMember.Role.ANNOTATOR,
            enabled=True,
        )
        task = TaskFactory(project=self.project)
        assignment = TaskAssignment.objects.create(
            task=task,
            project=self.project,
            assignee=revoked,
            assigned_by=self.manager,
        )

        project_membership = ProjectMember.objects.get(
            project=self.project,
            user=revoked,
        )
        project_membership.enabled = False
        project_membership.save(update_fields=['enabled'])
        revoked.is_active = False
        revoked.save(update_fields=['is_active'], session_actor=UserFactory(is_staff=True, is_superuser=True))

        self.client.force_authenticate(user=self.manager)
        response = self.client.get(
            f'/api/task-assignments/?project={self.project.id}&task={task.id}&active=true'
        )

        assert response.status_code == 200
        item = next(item for item in response.json() if item['id'] == assignment.id)
        assert item['assignee'] == revoked.id
        assert item['assignee_identity']['id'] == revoked.id
        assert item['assignee_identity']['email'] == revoked.email

    def test_manager_can_assign_active_annotator(self):
        new_task = TaskFactory(project=self.project)
        self.client.force_authenticate(user=self.manager)

        response = self.client.post(
            '/api/task-assignments/',
            data={'task': new_task.id, 'assignee': self.annotator_a.id},
            format='json',
        )

        assert response.status_code == 201, response.json()
        assignment = TaskAssignment.objects.get(pk=response.json()['id'])
        assert assignment.project_id == self.project.id
        assert assignment.assignee_id == self.annotator_a.id
        assert assignment.assigned_by_id == self.manager.id
        assert assignment.status == TaskAssignment.Status.ASSIGNED

    def test_manager_cannot_assign_inactive_user(self):
        inactive = UserFactory(active_organization=self.organization, is_active=False)
        self.organization.add_user(inactive)
        ProjectMember.objects.create(
            project=self.project,
            user=inactive,
            role=ProjectMember.Role.ANNOTATOR,
            enabled=True,
        )

        self.client.force_authenticate(user=self.manager)

        eligible = self.client.get(
            f'/api/task-assignments/eligible-assignees/?project={self.project.id}'
        )
        assert eligible.status_code == 200
        assert inactive.id not in {item['id'] for item in eligible.json()['results']}

        new_task = TaskFactory(project=self.project)
        response = self.client.post(
            '/api/task-assignments/',
            data={'task': new_task.id, 'assignee': inactive.id},
            format='json',
        )

        assert response.status_code == 400
        assert not TaskAssignment.objects.filter(task=new_task, assignee=inactive).exists()

    def test_manager_cannot_assign_soft_deleted_organization_member(self):
        revoked = UserFactory(active_organization=self.organization)
        self.organization.add_user(revoked)
        ProjectMember.objects.create(
            project=self.project,
            user=revoked,
            role=ProjectMember.Role.ANNOTATOR,
            enabled=True,
        )
        org_membership = OrganizationMember.objects.get(
            organization=self.organization,
            user=revoked,
        )
        org_membership.deleted_at = org_membership.created_at
        org_membership.save(update_fields=['deleted_at'])

        new_task = TaskFactory(project=self.project)
        self.client.force_authenticate(user=self.manager)
        response = self.client.post(
            '/api/task-assignments/',
            data={'task': new_task.id, 'assignee': revoked.id},
            format='json',
        )

        assert response.status_code == 400
        assert not TaskAssignment.objects.filter(task=new_task, assignee=revoked).exists()

    def test_manager_cannot_assign_reviewer_as_annotator(self):
        new_task = TaskFactory(project=self.project)
        self.client.force_authenticate(user=self.manager)

        response = self.client.post(
            '/api/task-assignments/',
            data={'task': new_task.id, 'assignee': self.reviewer.id},
            format='json',
        )

        assert response.status_code == 400
        assert not TaskAssignment.objects.filter(task=new_task, assignee=self.reviewer).exists()

    def test_annotator_cannot_create_assignments(self):
        new_task = TaskFactory(project=self.project)
        self.client.force_authenticate(user=self.annotator_a)

        response = self.client.post(
            '/api/task-assignments/',
            data={'task': new_task.id, 'assignee': self.annotator_a.id},
            format='json',
        )

        assert response.status_code == 403
