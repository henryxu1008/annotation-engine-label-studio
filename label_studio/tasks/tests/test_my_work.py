import json
from unittest.mock import patch

from access_control.identity import Principal
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils.translation import override as locale_override
from organizations.models import OrganizationMember
from organizations.tests.factories import OrganizationFactory
from projects.models import ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.test import APITestCase
from tasks.models import AnnotationDraft, ReviewDecision, Submission, TaskAssignment
from tasks.tests.factories import AnnotationFactory, TaskFactory
from users.tests.factories import UserFactory


class TestMyWork(APITestCase):
    def setUp(self):
        self.organization = OrganizationFactory()
        self.project = ProjectFactory(organization=self.organization)
        self.manager = self.organization.created_by
        self.annotator = UserFactory(active_organization=self.organization)
        self.other = UserFactory(active_organization=self.organization)
        self.reviewer = UserFactory(active_organization=self.organization)
        for user, role in [(self.annotator, 'annotator'), (self.other, 'annotator'), (self.reviewer, 'reviewer')]:
            ProjectMember.objects.create(project=self.project, user=user, role=role)
        self.url = f'/api/projects/{self.project.pk}/my-work/'
        self.client.force_authenticate(user=self.annotator)

    def assignment(self, *, owner=None, result=None, status='assigned'):
        task = TaskFactory(project=self.project, data={'text': 'sensitive task text not for this projection'})
        owner = owner or self.annotator
        annotation = None if result is None else AnnotationFactory(task=task, completed_by=owner, result=result)
        return TaskAssignment.objects.create(
            task=task,
            project=self.project,
            assignee=owner,
            assigned_by=self.manager,
            annotation=annotation,
            status=status,
        )

    def submission(self, assignment, *, status='pending', revision=1, result=None, reason=''):
        submission = Submission.objects.create(
            assignment=assignment,
            annotation=assignment.annotation,
            revision=revision,
            status=status,
            submitted_by=assignment.assignee,
            result_hash='hash',
            result_snapshot={'annotation': {'result': result or []}},
        )
        if status in ('approved', 'rejected'):
            ReviewDecision.objects.create(
                submission=submission, reviewer=self.reviewer, decision=status, reason=reason
            )
        return submission

    def draft(self, assignment, result, **kwargs):
        return AnnotationDraft.objects.create(
            task=assignment.task,
            annotation=assignment.annotation,
            user=assignment.assignee,
            assignment=assignment,
            result=result,
            **kwargs,
        )

    def get(self, **params):
        response = self.client.get(self.url, params)
        assert response.status_code == 200, response.content
        assert response['Cache-Control'] == 'private, no-store'
        return response.json()

    def test_work_states_are_derived_from_own_latest_revision_and_draft_content(self):
        assigned = self.assignment()
        empty_draft = self.assignment()
        self.draft(empty_draft, [])
        in_progress = self.assignment(status='in_progress')
        submitted = self.assignment(result=[])
        self.submission(submitted)
        same_draft = self.assignment(result=[])
        self.submission(same_draft)
        self.draft(same_draft, [])
        approved = self.assignment(result=[])
        self.submission(approved, status='approved')
        changed = self.assignment(result=[])
        self.submission(changed, status='approved', result=[{'id': 'a', 'value': 1}])
        self.draft(changed, [{'value': 2, 'id': 'a'}])
        rejected = self.assignment(result=[])
        old = self.submission(rejected, status='approved')
        latest = self.submission(rejected, status='rejected', revision=2, reason='边界包含了英文空格，请修正。')
        Submission.objects.filter(pk=old.pk).update(submitted_at='2030-01-01T00:00Z')
        self.draft(rejected, [{'id': 'new'}])
        superseded = self.assignment(result=[])
        self.submission(superseded, status='superseded')
        cancelled = self.assignment(status='cancelled')
        self.assignment(owner=self.other, result=[{'private': 'another answer'}])
        before = list(TaskAssignment.objects.values('pk', 'status', 'version', 'updated_at'))

        data = self.get()
        rows = {item['assignment_id']: item for item in data['results']}
        assert {pk: item['state'] for pk, item in rows.items()} == {
            assigned.pk: 'assigned',
            empty_draft.pk: 'in_progress',
            in_progress.pk: 'in_progress',
            submitted.pk: 'submitted',
            same_draft.pk: 'submitted',
            approved.pk: 'approved',
            changed.pk: 'in_progress',
            rejected.pk: 'rework',
            superseded.pk: 'conflict',
        }
        assert data['counts'] == {
            'assigned': 1,
            'in_progress': 3,
            'submitted': 2,
            'approved': 1,
            'rework': 1,
            'conflict': 1,
        }
        assert data['count'] == sum(data['counts'].values()) == 9
        assert rows[rejected.pk]['latest_submission_id'] == latest.pk
        assert rows[rejected.pk]['rejection_reason'] == '边界包含了英文空格，请修正。'
        assert rows[changed.pk]['latest_status'] == 'approved'
        assert rows[superseded.pk]['allowed_actions'] == []
        assert rows[superseded.pk]['editor_url'] is None
        assert cancelled.pk not in rows
        assert 'another answer' not in json.dumps(data)
        assert 'sensitive task text' not in json.dumps(data)
        assert 'result_snapshot' not in json.dumps(data)
        assert list(TaskAssignment.objects.values('pk', 'status', 'version', 'updated_at')) == before

    def test_json_object_key_order_and_lead_time_do_not_invent_new_work(self):
        assignment = self.assignment(result=[])
        result = [{'id': 'region', 'value': {'start': 2, 'end': 4}}]
        self.submission(assignment, result=result)
        self.draft(assignment, [{'value': {'end': 4, 'start': 2}, 'id': 'region'}], lead_time=999)
        row = self.get()['results'][0]
        assert row['state'] == 'submitted'
        assert row['has_draft'] is True

    def test_array_order_and_region_ids_are_part_of_draft_content(self):
        assignment = self.assignment(result=[])
        self.submission(assignment, result=[{'id': 'first'}, {'id': 'second'}])
        draft = self.draft(assignment, [{'id': 'second'}, {'id': 'first'}])
        assert self.get()['results'][0]['state'] == 'in_progress'
        draft.result = [{'id': 'renamed'}, {'id': 'second'}]
        draft.save()
        assert self.get()['results'][0]['state'] == 'in_progress'

    def test_mismatched_and_duplicate_drafts_are_blocked_not_repaired(self):
        assignment = self.assignment(result=[])
        self.draft(assignment, [])
        self.draft(assignment, [])
        other_annotation = AnnotationFactory(task=assignment.task, completed_by=self.annotator)
        AnnotationDraft.objects.create(
            task=assignment.task, annotation=other_annotation, assignment=assignment, user=self.annotator, result=[]
        )
        row = self.get()['results'][0]
        assert row['state'] == 'conflict'
        assert row['blocked_reason'] == 'WORK_SOURCE_CONFLICT'
        assert AnnotationDraft.objects.count() == 3

    def test_review_queue_uses_pending_snapshots_without_editor_or_answer_access(self):
        assignment = self.assignment(result=[])
        pending = self.submission(assignment)
        self.submission(assignment, revision=2, status='approved')
        own = self.assignment(owner=self.reviewer, result=[])
        self.submission(own)
        assignment.cancel()
        self.client.force_authenticate(user=self.reviewer)
        data = self.get()
        assert data['kind'] == 'review'
        assert data['counts'] == {'pending': 1}
        assert data['results'][0]['submission_id'] == pending.pk
        assert data['results'][0]['source_cancelled'] is True
        assert 'editor_url' not in data['results'][0]
        assert 'result_snapshot' not in json.dumps(data)
        assert self.client.get(f'/api/tasks/{assignment.task.pk}/').status_code == 404
        assert self.client.get(self.url, {'kind': 'annotation'}).status_code == 403
        self.client.force_authenticate(user=self.manager)
        assert self.client.get(self.url, {'kind': 'review'}).status_code == 403

    def test_no_other_user_filter_or_mutation_endpoints(self):
        self.assignment()
        assert self.client.get(self.url, {'user_id': self.other.pk}).status_code == 400
        assert self.client.get(self.url, {'kind': 'invalid'}).status_code == 400
        assert self.client.get(self.url, {'state': 'invalid'}).status_code == 400
        assert self.client.get(self.url, {'kind': 'review'}).status_code == 403
        for method in ('post', 'put', 'patch', 'delete'):
            assert getattr(self.client, method)(self.url, {}, format='json').status_code == 405

    def test_out_of_range_code_is_locale_independent_and_distinct_from_revocation(self):
        from tasks.my_work import MyWorkPagination

        self.assignment()
        for locale in ('en-us', 'zh-hans'):
            # Exercise the actual DRF paginator/translated exception through the
            # API handler without coupling this assertion to translated text.
            original = MyWorkPagination.paginate_queryset

            def localized(paginator, *args, **kwargs):
                with locale_override(locale):
                    return original(paginator, *args, **kwargs)

            with patch.object(MyWorkPagination, 'paginate_queryset', localized):
                response = self.client.get(self.url, {'page': 2})
            assert response.status_code == 404
            assert response.json()['display_context']['reason'] == 'WORK_PAGE_OUT_OF_RANGE'
            assert response['Cache-Control'] == 'private, no-store'
        ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=False)
        response = self.client.get(self.url, {'page': 2})
        assert response.status_code == 404
        assert response.json().get('display_context', {}).get('reason') != 'WORK_PAGE_OUT_OF_RANGE'

    def test_revocation_and_cross_scope_hide_rows_and_counts(self):
        self.assignment()
        outside = ProjectFactory()
        assert self.client.get(f'/api/projects/{outside.pk}/my-work/').status_code == 404
        membership = ProjectMember.objects.get(project=self.project, user=self.annotator)
        membership.enabled = False
        membership.save()
        assert self.client.get(self.url).status_code == 404
        membership.enabled = True
        membership.save()
        OrganizationMember.objects.filter(organization=self.organization, user=self.annotator).update(
            deleted_at='2026-10-20T00:00Z'
        )
        assert self.client.get(self.url).status_code == 403

    def test_provider_mapped_actor_determines_scope_and_rows(self):
        own = self.assignment()
        self.assignment(owner=self.other)
        principal = Principal('mapped:annotator', 'mapped', self.annotator.email, self.annotator.pk)
        outside = UserFactory(active_organization=OrganizationFactory())
        self.client.force_authenticate(user=outside)
        with patch('tasks.my_work.resolve_actor', return_value=(principal, self.annotator)):
            data = self.get()
        assert [row['assignment_id'] for row in data['results']] == [own.pk]

    def test_actual_list_and_count_queries_recheck_membership_after_preflight(self):
        from tasks.my_work import work_projects

        assignment = self.assignment(result=[])
        self.submission(assignment)
        for user in [self.annotator, self.reviewer]:
            self.client.force_authenticate(user=user)

            def revoked_projects(actor, roles):
                ProjectMember.objects.filter(project=self.project, user=actor).update(enabled=False)
                return work_projects(actor, roles)

            with patch('tasks.my_work.work_projects', side_effect=revoked_projects):
                data = self.get()
            assert data['results'] == []
            assert data['count'] == 0
            assert sum(data['counts'].values()) == 0

    @override_settings(
        ANNOTATION_PROJECT_OPERATION_PROVIDER='tasks.tests.test_project_operation_policy.PausedProvider'
    )
    def test_paused_policy_keeps_read_only_rows_without_work_actions(self):
        self.assignment()
        data = self.get()
        assert data['policy']['state'] == 'paused'
        assert data['results'][0]['allowed_actions'] == []
        assert data['results'][0]['blocked_reason'] == 'PROJECT_PAUSED'
        assert data['results'][0]['can_submit'] is False

    @override_settings(ANNOTATION_PROJECT_OPERATION_PROVIDER=None)
    def test_unavailable_policy_fails_closed(self):
        response = self.client.get(self.url)
        assert response.status_code == 403
        assert response['Cache-Control'] == 'private, no-store'

    def test_pagination_counts_and_query_count_do_not_depend_on_page_length(self):
        for _ in range(6):
            assignment = self.assignment(result=[])
            self.submission(assignment)
            self.draft(assignment, [])
        with CaptureQueriesContext(connection) as first:
            small = self.get(page_size=1)
        with CaptureQueriesContext(connection) as second:
            large = self.get(page_size=100)
        assert small['count'] == large['count'] == 6
        assert small['counts'] == large['counts']
        assert len(small['results']) == 1
        assert len(large['results']) == 6
        assert len(first) == len(second)
        filtered = self.get(state='submitted', page_size=2)
        assert filtered['count'] == 6
        assert len(filtered['results']) == 2
        assert filtered['next'] is not None
