from unittest.mock import patch

from access_control.identity import Principal
from django.test import override_settings
from projects.models import LabelStreamHistory, ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.test import APITestCase
from task_packages.models import TaskPackageItem
from tasks.models import AnnotationDraft, ReviewDecision, Submission, TaskAssignment
from tasks.serializers import NextTaskSerializer, SubmissionSerializer
from tasks.tests.factories import AnnotationFactory

from .test_packages import PackageFixture


@override_settings(ANNOTATION_FIXED_PACKAGES_ENABLED=True)
class TestPackageContinuity(PackageFixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.package_a = self.create(name='Package A', task_ids=[self.tasks[0].pk, self.tasks[2].pk]).json()['id']
        self.package_b = self.create(name='Package B', task_ids=[self.tasks[1].pk]).json()['id']
        self.assignment_a = self.assignment(task=self.tasks[0])
        self.assignment_b = self.assignment(task=self.tasks[1])

    def complete_a(self):
        annotation = AnnotationFactory(task=self.tasks[0], completed_by=self.annotator, result=[])
        self.assignment_a.annotation = annotation
        self.assignment_a.save(update_fields=['annotation'])
        self.submission(self.assignment_a)

    def test_two_packages_editor_link_retains_current_package(self):
        self.client.force_authenticate(self.annotator)
        response = self.detail(self.package_a)
        assert response.status_code == 200, response.content
        assert f'task_package={self.package_a}' in response.json()['results'][0]['editor_url']

    def test_mapped_actor_does_not_sample_with_session_managers_completion_state(self):
        self.complete_a()
        LabelStreamHistory.objects.create(
            user=self.annotator,
            project=self.project,
            data=[
                {'taskId': self.tasks[0].pk, 'annotationId': None},
            ],
        )
        self.client.force_authenticate(self.manager)
        principal = Principal(f'test:{self.annotator.pk}', 'test', 'mapped', self.annotator.pk)
        with patch('access_control.identity.get_identity_provider') as provider:
            provider.return_value.resolve.return_value = principal
            assert self.toolbar(self.package_a).status_code == 404
            assert (
                self.client.get(f'/api/projects/{self.project.pk}/next/', {'task_package': self.package_a}).status_code
                == 404
            )
            history = self.client.get(
                f'/api/projects/{self.project.pk}/label-stream-history/', {'task_package': self.package_a}
            )
            assert [row['taskId'] for row in history.json()] == [self.tasks[0].pk]

    def test_toolbar_stops_at_empty_a_instead_of_returning_b(self):
        self.complete_a()
        self.client.force_authenticate(self.annotator)
        response = self.client.post(
            f'/api/dm/actions/?project={self.project.pk}&id=next_task&task_package={self.package_a}',
            {'selectedItems': {'all': True, 'excluded': []}},
            format='json',
        )
        assert response.status_code == 404, response.content
        assert response.json()['display_context']['reason'] == 'QUEUE_EXHAUSTED'

    def test_rejected_a_returns_to_package_continuation_while_b_is_available(self):
        self.complete_a()
        Submission.objects.filter(assignment=self.assignment_a).update(status='rejected')
        self.client.force_authenticate(self.annotator)
        response = self.toolbar(self.package_a)
        assert response.status_code == 200, response.content
        assert response.json()['id'] == self.tasks[0].pk
        assert response.json()['default_selected_annotation'] == self.assignment_a.annotation_id
        assert [row['id'] for row in response.json()['annotations']] == [self.assignment_a.annotation_id]

    def test_mapped_actor_resumes_own_annotation_without_borrowing_session_managers_answer(self):
        self.complete_a()
        Submission.objects.filter(assignment=self.assignment_a).update(status='rejected')
        AnnotationFactory(task=self.tasks[0], completed_by=self.manager, result=[{'private': 'manager answer'}])
        principal = Principal(f'test:{self.annotator.pk}', 'test', 'mapped', self.annotator.pk)
        self.client.force_authenticate(self.manager)
        with patch('access_control.identity.get_identity_provider') as provider:
            provider.return_value.resolve.return_value = principal
            response = self.toolbar(self.package_a)
        assert response.status_code == 200, response.content
        data = response.json()
        assert data['assignment_id'] == self.assignment_a.pk
        assert data['default_selected_annotation'] == self.assignment_a.annotation_id
        assert [row['id'] for row in data['annotations']] == [self.assignment_a.annotation_id]
        assert b'manager answer' not in response.content

    def test_resumed_draft_uses_existing_work_state_without_repeating_saved_submission(self):
        self.complete_a()
        latest = Submission.objects.get(assignment=self.assignment_a)
        draft = AnnotationDraft.objects.create(
            task=self.tasks[0],
            user=self.annotator,
            assignment=self.assignment_a,
            annotation=self.assignment_a.annotation,
            result=latest.result_snapshot['annotation']['result'],
        )
        self.client.force_authenticate(self.annotator)
        assert self.toolbar(self.package_a).status_code == 404
        draft.result = [{'id': 'changed', 'value': {'text': ['resumed edit']}}]
        draft.save(update_fields=['result'])
        response = self.toolbar(self.package_a)
        assert response.status_code == 200, response.content
        assert response.json()['id'] == self.tasks[0].pk
        # A newer pending revision ends the returned-for-changes state.
        Submission.objects.filter(pk=latest.pk).update(status='rejected')
        newer = self.submission(self.assignment_a, revision=2)
        newer.result_snapshot = {'annotation': {'result': draft.result}}
        newer.save(update_fields=['result_snapshot'])
        assert self.toolbar(self.package_a).status_code == 404

    def test_second_next_api_stops_at_empty_a_instead_of_returning_b(self):
        self.complete_a()
        self.client.force_authenticate(self.annotator)
        response = self.client.get(f'/api/projects/{self.project.pk}/next/', {'task_package': self.package_a})
        assert response.status_code == 404, response.content

    def test_direct_task_read_does_not_ignore_explicit_package(self):
        self.client.force_authenticate(self.annotator)
        response = self.client.get(f'/api/tasks/{self.tasks[1].pk}/', {'task_package': self.package_a})
        assert response.status_code == 404, response.content

    def test_review_queue_returns_only_a_when_b_has_newer_pending_work(self):
        expected = self.submission(self.assignment_a)
        other = self.submission(self.assignment_b)
        self.client.force_authenticate(self.reviewer)
        response = self.client.get(
            '/api/submissions/',
            {'project': self.project.pk, 'reviewable': 'true', 'task_package': self.package_a},
        )
        assert response.status_code == 200, response.content
        rows = response.json()['results']
        assert [row['id'] for row in rows] == [expected.pk], (expected.pk, other.pk, rows)

    def toolbar(self, package=None, suffix=''):
        query = '' if package is None else f'&task_package={package}'
        return self.client.post(
            f'/api/dm/actions/?project={self.project.pk}&id=next_task{query}{suffix}',
            {'selectedItems': {'all': True, 'excluded': []}},
            format='json',
        )

    def review_queue(self, package=None, **changes):
        params = {'project': self.project.pk, 'reviewable': 'true', **changes}
        if package is not None:
            params['task_package'] = package
        return self.client.get('/api/submissions/', params)

    def test_both_next_apis_return_only_a_and_preserve_unscoped_queue(self):
        self.client.force_authenticate(self.annotator)
        assert self.toolbar(self.package_a).json()['id'] == self.tasks[0].pk
        response = self.client.get(f'/api/projects/{self.project.pk}/next/', {'task_package': self.package_a})
        assert response.status_code == 200 and response.json()['id'] == self.tasks[0].pk
        self.complete_a()
        assert self.toolbar().json()['id'] == self.tasks[1].pk

    def test_package_history_does_not_offer_another_packages_task(self):
        LabelStreamHistory.objects.create(
            user=self.annotator,
            project=self.project,
            data=[
                {'taskId': self.tasks[1].pk, 'annotationId': None},
                {'taskId': self.tasks[0].pk, 'annotationId': None},
            ],
        )
        self.client.force_authenticate(self.annotator)
        url = f'/api/projects/{self.project.pk}/label-stream-history/'
        response = self.client.get(url, {'task_package': self.package_a})
        assert response.status_code == 200
        assert [row['taskId'] for row in response.json()] == [self.tasks[0].pk]
        # Filtering a package response must not prune the global history.
        assert len(self.client.get(url).json()) == 2

    def test_history_rechecks_access_after_reading_project_history(self):
        self.assignment(task=self.tasks[2])
        self.client.force_authenticate(self.annotator)

        def revoked_history(actor, project):
            TaskAssignment.objects.filter(pk=self.assignment_a.pk).update(status='cancelled')
            return [{'taskId': self.tasks[0].pk, 'annotationId': None}]

        with patch('projects.api.get_label_stream_history', revoked_history):
            response = self.client.get(
                f'/api/projects/{self.project.pk}/label-stream-history/', {'task_package': self.package_a}
            )
            assert response.status_code == 200
            assert response.json() == []

    def test_invalid_repeated_missing_and_cross_project_context_never_falls_back(self):
        self.client.force_authenticate(self.annotator)
        for invalid in ['', '0', '-1', '1.0', 'abc', '9223372036854775808']:
            assert self.toolbar(invalid).status_code == 400
        assert self.toolbar(self.package_a, f'&task_package={self.package_b}').status_code == 400
        assert self.toolbar(999999).status_code == 404
        self.client.force_authenticate(self.manager)
        project_b = ProjectFactory(organization=self.organization, created_by=self.manager)
        response = self.client.get(f'/api/projects/{project_b.pk}/next/', {'task_package': self.package_a})
        assert response.status_code == 404

    @override_settings(ANNOTATION_FIXED_PACKAGES_ENABLED=False)
    def test_disabled_feature_does_not_turn_scoped_calls_into_project_queue(self):
        self.client.force_authenticate(self.annotator)
        assert self.toolbar(self.package_a).status_code == 404
        assert self.toolbar().status_code == 200

    def test_live_assignment_revocation_and_unavailable_membership_fail_closed(self):
        self.client.force_authenticate(self.annotator)
        self.assignment_a.status = TaskAssignment.Status.CANCELLED
        self.assignment_a.save(update_fields=['status'])
        assert self.toolbar(self.package_a).status_code == 404
        TaskPackageItem.objects.filter(package_id=self.package_b).update(original_created_at=self.project.created_at)
        assert self.toolbar(self.package_b).status_code == 404

    def test_review_scope_cannot_be_used_as_history_or_widened_by_body(self):
        a, b = self.submission(self.assignment_a), self.submission(self.assignment_b)
        self.client.force_authenticate(self.reviewer)
        assert self.review_queue(self.package_a, history='true').status_code == 400
        assert self.review_queue(self.package_a, reviewable='false').status_code == 400
        response = self.client.post(
            f'/api/submissions/{b.pk}/review/?task_package={self.package_a}',
            {'decision': 'approved'},
            format='json',
        )
        assert response.status_code == 404
        assert not ReviewDecision.objects.exists()
        assert len(self.review_queue().json()['results']) == 2
        response = self.client.post(
            f'/api/submissions/{a.pk}/review/?task_package={self.package_a}',
            {'decision': 'approved'},
            format='json',
        )
        assert response.status_code == 200
        assert self.review_queue(self.package_a).status_code == 404
        assert [row['id'] for row in self.review_queue().json()['results']] == [b.pk]

    def test_cancelled_source_stays_reviewable_but_self_review_does_not(self):
        a = self.submission(self.assignment_a)
        self.assignment_a.status = TaskAssignment.Status.CANCELLED
        self.assignment_a.save(update_fields=['status'])
        self.client.force_authenticate(self.reviewer)
        assert self.review_queue(self.package_a).json()['results'][0]['id'] == a.pk
        ProjectMember.objects.filter(project=self.project, user=self.annotator).update(role='reviewer')
        self.client.force_authenticate(self.annotator)
        assert self.review_queue(self.package_a).status_code == 404

    @override_settings(ANNOTATION_PROJECT_OPERATION_PROVIDER='task_packages.tests.test_packages.ReadOnlyPolicy')
    def test_read_only_project_permits_scoped_reads_but_not_next_or_review(self):
        self.client.force_authenticate(self.annotator)
        assert self.client.get(f'/api/tasks/{self.tasks[0].pk}/', {'task_package': self.package_a}).status_code == 200
        assert self.toolbar(self.package_a).status_code == 403
        self.submission(self.assignment_a)
        self.client.force_authenticate(self.reviewer)
        assert self.review_queue(self.package_a).status_code == 403

    def test_next_rechecks_reassignment_after_materializing_task(self):
        self.assignment(task=self.tasks[2])  # Another row keeps A visible.
        self.client.force_authenticate(self.annotator)
        original = NextTaskSerializer.to_representation

        def revoke(serializer, task):
            data = original(serializer, task)
            TaskAssignment.objects.filter(task=task, assignee=self.annotator).update(status='cancelled')
            return data

        with patch.object(NextTaskSerializer, 'to_representation', revoke):
            assert self.toolbar(self.package_a).status_code in (403, 404)

    def test_review_rechecks_partial_row_departure_after_materialization(self):
        a = self.submission(self.assignment_a)
        self.submission(self.assignment(task=self.tasks[2]))  # Keep A visible.
        self.client.force_authenticate(self.reviewer)
        original = SubmissionSerializer.to_representation

        def approve_elsewhere(serializer, submission):
            data = original(serializer, submission)
            if submission.pk == a.pk:
                Submission.objects.filter(pk=a.pk).update(status='approved')
            return data

        with patch.object(SubmissionSerializer, 'to_representation', approve_elsewhere):
            assert self.review_queue(self.package_a).status_code == 403

    def test_project_and_organization_revocation_override_package_membership(self):
        self.client.force_authenticate(self.annotator)
        ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=False)
        assert self.toolbar(self.package_a).status_code in (403, 404)
        self.submission(self.assignment_a)
        self.client.force_authenticate(self.reviewer)
        self.reviewer.om_through.filter(organization=self.organization).update(deleted_at=self.project.created_at)
        assert self.review_queue(self.package_a).status_code in (403, 404)
