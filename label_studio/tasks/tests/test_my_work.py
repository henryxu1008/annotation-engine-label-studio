import base64
import json
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from access_control.identity import Principal
from data_import.models import FileUpload
from data_manager.models import View
from django.core.files.uploadedfile import SimpleUploadedFile
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

    def mapped_provider(self, actor, session_user=None):
        # Exercise the real identity seam for every endpoint, not a mock of the
        # work projection alone. The authenticated session must remain intact.
        session_user = session_user or UserFactory(active_organization=OrganizationFactory())
        self.client.force_authenticate(user=session_user)

        class MappedProvider:
            def resolve(self, request):
                assert request.user.pk == session_user.pk
                return Principal(f'mapped:{actor.pk}', 'test-mapped', actor.email, actor.pk)

        return patch('access_control.identity.get_identity_provider', return_value=MappedProvider())

    def test_mapping_does_not_expand_unmigrated_user_profile_permissions(self):
        session_user = UserFactory(active_organization=None)
        unrelated = UserFactory(active_organization=None, first_name='Unrelated')
        with self.mapped_provider(self.annotator, session_user=session_user):
            assert self.client.get(f'/api/users/{unrelated.pk}/').status_code == 403
            assert self.client.patch(
                f'/api/users/{unrelated.pk}/', {'first_name': 'Changed'}, format='json'
            ).status_code == 403
        unrelated.refresh_from_db()
        assert unrelated.first_name == 'Unrelated'

    def test_mapped_work_opens_editor_with_only_actor_annotation_and_draft(self):
        own = self.assignment(result=[])
        draft = self.draft(own, [])
        other = AnnotationFactory(task=own.task, completed_by=self.other, result=[{'private': 'other answer'}])
        TaskAssignment.objects.create(
            task=own.task, project=self.project, assignee=self.other, assigned_by=self.manager, annotation=other
        )
        with self.mapped_provider(self.annotator):
            row = self.get()['results'][0]
            assert row['assignment_id'] == own.pk
            assert row['allowed_actions'] == ['open_editor']
            task_id = parse_qs(urlparse(row['editor_url']).query)['task'][0]
            project = self.client.get(f'/api/projects/{self.project.pk}/')
            assert project.status_code == 200, project.content
            editor = self.client.get(f'/api/tasks/{task_id}/')
            assert editor.status_code == 200, editor.content
            data = editor.json()
            assert data['assignment_id'] == own.pk
            assert data['assignment_version'] == own.version
            assert data['can_label'] is True
            assert {item['id'] for item in data['annotations']} == {own.annotation_id}
            assert {item['id'] for item in data['drafts']} == {draft.pk}
            assert 'other answer' not in json.dumps(data)
            for endpoint in ('/api/dm/project/', '/api/dm/columns/'):
                response = self.client.get(endpoint, {'project': self.project.pk})
                assert response.status_code == 200, response.content
            saved = self.client.patch(
                f'/api/annotations/{own.annotation_id}/',
                {
                    'result': [], 'assignment_id': own.pk, 'assignment_version': own.version,
                    'draft_id': draft.pk, 'draft_revision': draft.revision,
                },
                format='json',
            )
            assert saved.status_code == 200, saved.content
            own.annotation.refresh_from_db()
            assert own.annotation.updated_by_id == self.annotator.pk

    def test_mapped_editor_initialization_uses_actor_views_and_creation_owner(self):
        own = self.assignment()
        view = View.objects.create(project=self.project, user=self.manager, data={'title': 'Project tasks'})
        foreign = ProjectFactory()
        foreign_view = View.objects.create(project=foreign, user=foreign.created_by)
        with self.mapped_provider(self.annotator):
            assert self.get()['results'][0]['assignment_id'] == own.pk
            tabs = self.client.get('/api/dm/views/', {'project': self.project.pk})
            assert tabs.status_code == 200, tabs.content
            assert [item['id'] for item in tabs.json()] == [view.pk]
            assert self.client.get(f'/api/dm/views/{view.pk}/').status_code == 200
            assert self.client.get(f'/api/dm/views/{foreign_view.pk}/').status_code == 404
            created = self.client.post(
                '/api/dm/views/',
                {'project': self.project.pk, 'data': {'title': 'My tab'}, 'user': self.manager.pk},
                format='json',
            )
            assert created.status_code == 201, created.content
            assert View.objects.get(pk=created.json()['id']).user_id == self.annotator.pk
            saved = self.client.patch(
                f'/api/dm/views/{created.json()["id"]}/',
                {'data': {'title': 'Saved tab'}, 'user': self.manager.pk}, format='json',
            )
            assert saved.status_code == 200, saved.content
            assert View.objects.get(pk=created.json()['id']).user_id == self.annotator.pk
            assert self.client.patch(
                f'/api/dm/views/{view.pk}/', {'project': foreign.pk}, format='json'
            ).status_code == 404
            assert self.client.post(
                '/api/dm/views/', {'project': foreign.pk}, format='json'
            ).status_code == 404
            ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=False)
            assert self.client.get('/api/dm/views/', {'project': self.project.pk}).json() == []
            assert self.client.get(f'/api/dm/views/{view.pk}/').status_code == 404
            assert self.client.post(
                '/api/dm/views/', {'project': self.project.pk, 'data': {}}, format='json'
            ).status_code == 404

    def test_mapped_ml_metadata_does_not_grant_backend_configuration(self):
        self.assignment()
        foreign = ProjectFactory(organization=self.organization)
        with self.mapped_provider(self.annotator, session_user=self.manager):
            assert self.get()['results']
            response = self.client.get('/api/ml/', {'project': self.project.pk})
            assert response.status_code == 200, response.content
            assert response.json() == []
            assert self.client.get('/api/ml/', {'project': foreign.pk}).status_code == 404
            with patch('ml.serializers.MLBackendSerializer.validate') as validate:
                response = self.client.post(
                    '/api/ml/', {'project': self.project.pk, 'url': 'https://example.invalid'}, format='json'
                )
                assert response.status_code == 403
                validate.assert_not_called()
            ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=False)
            assert self.client.get('/api/ml/', {'project': self.project.pk}).status_code == 404

    def test_mapped_work_opens_review_queue_and_snapshot_with_actor_scope(self):
        assignment = self.assignment(result=[])
        pending = self.submission(assignment)
        own = self.assignment(owner=self.reviewer, result=[])
        self.submission(own)
        assignment.cancel()  # Review consumes immutable snapshots, not Editor access.
        with self.mapped_provider(self.reviewer):
            data = self.get()
            assert data['results'][0]['submission_id'] == pending.pk
            assert data['results'][0]['allowed_actions'] == ['open_review_queue']
            capability = self.client.get(f'/api/projects/{self.project.pk}/review-capability/')
            assert capability.status_code == 200, capability.content
            queue = self.client.get('/api/submissions/', {'project': self.project.pk, 'reviewable': 'true'})
            assert queue.status_code == 200, queue.content
            assert [item['id'] for item in queue.json()['results']] == [pending.pk]
            snapshot = self.client.get(f'/api/submissions/{pending.pk}/')
            assert snapshot.status_code == 200, snapshot.content
            assert snapshot.json()['id'] == pending.pk
            assert self.client.get(f'/api/tasks/{assignment.task_id}/').status_code == 404
            reviewed = self.client.post(
                f'/api/submissions/{pending.pk}/review/',
                {'decision': 'approved', 'reviewer': self.manager.pk},
                format='json',
            )
            assert reviewed.status_code == 200, reviewed.content
            assert ReviewDecision.objects.get(submission=pending).reviewer_id == self.reviewer.pk
            history = self.client.get('/api/submissions/', {'project': self.project.pk, 'history': 'true'})
            assert history.status_code == 200, history.content
            assert [item['id'] for item in history.json()['results']] == [pending.pk]

    def test_mapped_new_work_can_submit_first_annotation_and_read_only_its_collection(self):
        own = self.assignment()
        other = AnnotationFactory(task=own.task, completed_by=self.other, result=[{'private': 'other answer'}])
        TaskAssignment.objects.create(
            task=own.task, project=self.project, assignee=self.other, assigned_by=self.manager, annotation=other
        )
        with self.mapped_provider(self.annotator):
            assert self.get()['results'][0]['assignment_id'] == own.pk
            assert self.client.get(f'/api/tasks/{own.task_id}/').status_code == 200
            submitted = self.client.post(
                f'/api/tasks/{own.task_id}/annotations/',
                {
                    'result': [], 'assignment_id': own.pk, 'assignment_version': own.version,
                    'submit_for_review': True, 'completed_by': self.manager.pk,
                }, format='json',
            )
            assert submitted.status_code == 201, submitted.content
            result = self.client.get(f'/api/tasks/{own.task_id}/annotations/')
            assert result.status_code == 200, result.content
            assert [item['id'] for item in result.json()] == [submitted.json()['id']]
            snapshot = Submission.objects.get(assignment=own)
            assert snapshot.submitted_by_id == self.annotator.pk
            assert self.client.get(f'/api/submissions/{snapshot.pk}/').status_code == 200
            assert self.get()['results'][0]['state'] == 'submitted'

    def test_mapped_annotation_and_draft_reads_recheck_project_and_organization(self):
        own = self.assignment(result=[])
        draft = self.draft(own, [])
        with self.mapped_provider(self.annotator, session_user=self.manager):
            urls = (f'/api/annotations/{own.annotation_id}/', f'/api/drafts/{draft.pk}/')
            for url in urls:
                assert self.client.get(url).status_code == 200
            ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=False)
            for url in urls:
                assert self.client.get(url).status_code == 403
            ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=True)
            OrganizationMember.objects.filter(organization=self.organization, user=self.annotator).update(
                deleted_at='2026-10-20T00:00Z'
            )
            for url in urls:
                assert self.client.get(url).status_code == 403

    def test_mapped_editor_uploaded_image_uses_actor_and_rechecks_revocation(self):
        image = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jV5kAAAAASUVORK5CYII='
        )
        with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media, USE_NGINX_FOR_UPLOADS=False):
            upload = FileUpload.objects.create(
                project=self.project, user=self.manager, file=SimpleUploadedFile('mapped.png', image)
            )
            foreign_upload = FileUpload.objects.create(
                project=ProjectFactory(organization=self.organization),
                user=self.manager, file=SimpleUploadedFile('other.png', image),
            )
            own = self.assignment()
            own.task.data = {'image': upload.url}
            own.task.file_upload = upload
            own.task.save(update_fields=['data', 'file_upload'])
            image_url = urlparse(upload.url).path
            for actor, session_user in ((self.annotator, None), (self.other, self.manager)):
                with self.subTest(actor=actor.pk), self.mapped_provider(actor, session_user=session_user):
                    # Media retains the existing project-member permission;
                    # this does not grant Editor access to unassigned tasks.
                    if actor == self.annotator:
                        assert self.get()['results'][0]['assignment_id'] == own.pk
                        task = self.client.get(f'/api/tasks/{own.task_id}/')
                        assert task.status_code == 200, task.content
                        assert urlparse(task.json()['data']['image']).path == image_url
                    urls = (image_url, f'/storage-data/uploaded/?filepath={upload.file.name}')
                    assert self.client.get(urlparse(foreign_upload.url).path).status_code == 403
                    for url in urls:
                        response = self.client.get(url)
                        assert response.status_code == 200, response.content if not response.streaming else ''
                        assert b''.join(response.streaming_content) == image
                        # Django's test-client iterator already closes the
                        # response with its connection-signal guard. Closing it
                        # again kills PostgreSQL's enclosing TestCase transaction.
                        assert response.closed
                    ProjectMember.objects.filter(project=self.project, user=actor).update(enabled=False)
                    for url in urls:
                        assert self.client.get(url).status_code == 403
            with self.mapped_provider(self.reviewer, session_user=self.manager):
                OrganizationMember.objects.filter(organization=self.organization, user=self.reviewer).update(
                    deleted_at='2026-10-20T00:00Z'
                )
                for url in urls:
                    assert self.client.get(url).status_code == 403

    def test_mapped_work_links_do_not_borrow_session_manager_permissions(self):
        own = self.assignment(result=[])
        other = self.assignment(owner=self.other, result=[])
        foreign = ProjectFactory()
        foreign_task = TaskFactory(project=foreign)
        with self.mapped_provider(self.annotator, session_user=self.manager):
            assert self.get()['results'][0]['assignment_id'] == own.pk
            assert self.client.get(f'/api/tasks/{other.task_id}/').status_code == 404
            assert self.client.get(f'/api/tasks/{foreign_task.pk}/').status_code == 404
            assert self.client.get(f'/api/projects/{foreign.pk}/').status_code == 404
            assert self.client.get(
                '/api/submissions/', {'project': self.project.pk, 'reviewable': 'true'}
            ).status_code == 403
            assert self.client.patch(
                f'/api/tasks/{own.task_id}/', {'data': {'text': 'spoofed'}}, format='json'
            ).status_code == 403

    def test_mapped_editor_rechecks_project_revocation_after_work_was_read(self):
        own = self.assignment(result=[])
        with self.mapped_provider(self.annotator, session_user=self.manager):
            assert self.get()['results'][0]['assignment_id'] == own.pk
            assert self.client.get(f'/api/tasks/{own.task_id}/').status_code == 200
            ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=False)
            assert self.client.get(self.url).status_code == 404
            assert self.client.get(f'/api/tasks/{own.task_id}/').status_code == 404
            response = self.client.patch(
                f'/api/annotations/{own.annotation_id}/',
                {'result': [], 'assignment_id': own.pk, 'assignment_version': own.version},
                format='json',
            )
            assert response.status_code == 403

    def test_mapped_editor_rechecks_assignment_after_reassignment(self):
        own = self.assignment(result=[])
        with self.mapped_provider(self.annotator, session_user=self.manager):
            assert self.get()['results'][0]['assignment_id'] == own.pk
            draft_url = f'/api/tasks/{own.task_id}/annotations/{own.annotation_id}/drafts'
            created = self.client.post(
                draft_url, {'result': [], 'assignment_id': own.pk, 'assignment_version': own.version}, format='json'
            )
            assert created.status_code == 201, created.content
            own.cancel()
            TaskAssignment.objects.create(
                task=own.task, project=self.project, assignee=self.other, assigned_by=self.manager
            )
            assert self.get()['results'] == []
            assert self.client.get(f'/api/tasks/{own.task_id}/').status_code == 404
            response = self.client.post(
                draft_url,
                {'result': [], 'assignment_id': own.pk, 'assignment_version': own.version},
                format='json',
            )
            assert response.status_code == 404
            assert AnnotationDraft.objects.filter(assignment=own).count() == 1

    def test_mapped_inactive_actor_cannot_use_session_manager_or_cached_actions(self):
        pending = self.submission(self.assignment(result=[]))
        administrator = UserFactory(is_staff=True, is_superuser=True)
        for actor in (self.annotator, self.reviewer):
            with self.subTest(actor=actor.pk), self.mapped_provider(actor, session_user=self.manager):
                assert self.get()['results']
                type(actor).objects.filter(pk=actor.pk).update(is_active=False, session_actor=administrator)
                for endpoint in (
                    self.url,
                    f'/api/projects/{self.project.pk}/',
                    f'/api/tasks/{pending.assignment.task_id}/',
                    '/api/submissions/',
                    f'/api/submissions/{pending.pk}/',
                ):
                    response = self.client.get(endpoint)
                    assert response.status_code == 401, response.content

    def test_mapped_review_rechecks_revocation_on_queue_detail_and_write(self):
        pending = self.submission(self.assignment(result=[]))
        with self.mapped_provider(self.reviewer, session_user=self.manager):
            assert self.get()['results'][0]['submission_id'] == pending.pk
            ProjectMember.objects.filter(project=self.project, user=self.reviewer).update(enabled=False)
            assert self.client.get(self.url).status_code == 404
            assert self.client.get(
                '/api/submissions/', {'project': self.project.pk, 'reviewable': 'true'}
            ).status_code == 404
            assert self.client.get(f'/api/submissions/{pending.pk}/').status_code == 404
            assert self.client.post(
                f'/api/submissions/{pending.pk}/review/', {'decision': 'approved'}, format='json'
            ).status_code == 403
        pending.refresh_from_db()
        assert pending.status == 'pending'
        assert not ReviewDecision.objects.filter(submission=pending).exists()

    def test_mapped_organization_revocation_blocks_work_actions_and_snapshots(self):
        pending = self.submission(self.assignment(result=[]))
        for actor in (self.annotator, self.reviewer):
            with self.subTest(actor=actor.pk), self.mapped_provider(actor, session_user=self.manager):
                assert self.get()['results']
                OrganizationMember.objects.filter(organization=self.organization, user=actor).update(
                    deleted_at='2026-10-20T00:00Z'
                )
                assert self.client.get(self.url).status_code == 403
                assert self.client.get(f'/api/projects/{self.project.pk}/').status_code == 404
                assert self.client.get(f'/api/tasks/{pending.assignment.task_id}/').status_code in (403, 404)
                for endpoint in ('/api/dm/project/', '/api/dm/columns/'):
                    assert self.client.get(endpoint, {'project': self.project.pk}).status_code == 404
                assert self.client.get('/api/dm/views/', {'project': self.project.pk}).json() == []
                tasks = self.client.get('/api/tasks/', {'project': self.project.pk})
                assert tasks.status_code == 200, tasks.content
                assert tasks.json()['tasks'] == []
                assert tasks.json()['total'] == 0
                assert self.client.get(f'/api/submissions/{pending.pk}/').status_code == 404
                assert self.client.get('/api/submissions/', {'project': self.project.pk}).json() == []
                if actor == self.reviewer:
                    assert self.client.get(
                        '/api/submissions/', {'project': self.project.pk, 'reviewable': 'true'}
                    ).status_code == 403

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
