from unittest.mock import patch
from uuid import uuid4

from access_control.project_operations import ORGANIZE_SECOND_REVIEW, SECOND_REVIEW
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from organizations.models import OrganizationMember
from organizations.tests.factories import OrganizationFactory
from projects.models import ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.test import APITestCase
from review_batches.models import SecondReviewTask
from users.models import User
from users.tests.factories import UserFactory

from .second_review_fixtures import OPERATIONS, SecondReviewFixtures


class TestSecondReviewAuthority(SecondReviewFixtures, APITestCase):
    def setUp(self):
        self.second_setup()
        self.source, self.batch = self.prepared()
        self.body = self.dispatch_body(self.source, self.batch)
        result, _ = self.dispatch(self.source, self.batch, self.body)
        assert result.status_code == 201, result.data
        self.pk = result.data['receipt']['task_id']

    def post(self, suffix, body, user=None, key=None):
        self.client.force_authenticate(user or self.manager)
        return self.client.post(
            self.second_base + suffix, body, format='json', HTTP_IDEMPOTENCY_KEY=str(key or uuid4())
        )

    def test_B01_every_route_denies_outside_roles_and_other_reviewer(self):
        org_admin = UserFactory(active_organization=self.org, is_superuser=True)
        self.org.add_user(org_admin)
        for user in (self.worker, self.second_worker, org_admin, self.reviewer):
            with self.subTest(user=user.pk):
                self.client.force_authenticate(user)
                for suffix in ('', 'capability/', f'{self.pk}/', f'{self.pk}/source/'):
                    result = self.client.get(self.second_base + suffix)
                    if user == self.reviewer and suffix in ('', 'capability/'):
                        assert result.status_code == 200, result.data
                        if suffix == '':
                            assert result.data['count'] == 0
                    else:
                        assert result.status_code in (403, 404), (suffix, result.data)
                for suffix in (f'{self.pk}/decision/', f'{self.pk}/cancel/', ''):
                    result = self.post(suffix, self.body, user=user)
                    assert result.status_code in (400, 403, 404), (suffix, result.data)
                result = self.client.get(self.base + f'{self.batch}/sources/{self.source.pk}/second-review-assignees/')
                assert result.status_code in (403, 404)

    def test_B02_old_operations_and_manager_cannot_replace_second_rights(self):
        with self.settings(
            CONFIDENCE_TEST_OPERATIONS=[o for o in OPERATIONS if o not in (ORGANIZE_SECOND_REVIEW, SECOND_REVIEW)]
        ):
            assert self.context(self.source, self.batch).status_code == 403

            result = self.post('', self.body)
            assert result.status_code == 403
            self.client.force_authenticate(self.supervisor)
            read = self.client.get(self.second_base + self.pk + '/source/')
            assert read.status_code == 200, read.data
            result = self.post(
                self.pk + '/decision/',
                dict(
                    expected_version=1,
                    expected_source_hash=self.body['expected_source_hash'],
                    outcome='confirmed',
                    reason='',
                    expected_viewer_hash=read.data['viewer_hash'],
                ),
                self.supervisor,
            )
            assert result.status_code == 403
        assert self.post(self.pk + '/decision/', {}, self.manager).status_code == 403
        with self.settings(
            ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LegacyProjectOperationProvider'
        ):
            assert self.context(self.source, self.batch).status_code == 403

    def test_B01_other_reviewer_valid_direct_decision_body_still_denied(self):
        self.client.force_authenticate(self.reviewer)
        cap = self.client.get(self.second_base + 'capability/')
        assert cap.status_code == 200
        body = dict(
            expected_version=1,
            expected_source_hash=self.body['expected_source_hash'],
            outcome='confirmed',
            reason='',
            expected_viewer_hash=cap.data['viewer_hash'],
        )
        response = self.post(self.pk + '/decision/', body, user=self.reviewer)
        assert response.status_code == 404, response.data

    def test_B03_assignee_filter_precedes_count_and_direct_id_rechecked(self):
        ProjectMember.objects.filter(project=self.project, user=self.worker).update(role='reviewer')
        self.client.force_authenticate(self.manager)
        path = self.base + f'{self.batch}/sources/{self.source.pk}/second-review-assignees/'
        with CaptureQueriesContext(connection) as queries:
            result = self.client.get(path)
        assert result.status_code == 200, result.data
        assert result.data['count'] == 1 and result.data['results'] == [
            {'id': self.supervisor.pk, 'display_name': 'Independent 复核员'}
        ]
        assert 'users_user"."custom' not in '\n'.join(q['sql'] for q in queries.captured_queries)
        assert all(set(row) == {'id', 'display_name'} for row in result.data['results'])
        for excluded in (self.worker, self.reviewer, self.manager):
            # A second exact source makes this an eligibility test, not a subject conflict.
            source, batch = self.prepared()
            body = self.dispatch_body(source, batch)
            result = self.post('', {**body, 'assignee_id': excluded.pk})
            assert result.status_code == 403, result.data
        ProjectMember.objects.filter(project=self.project, user=self.supervisor).update(enabled=False)
        source, batch = self.prepared()
        result = self.client.get(self.base + f'{batch}/sources/{source.pk}/second-review-assignees/')
        assert result.status_code == 200 and result.data['count'] == 0

    def test_B04_foreign_ids_and_cursor_cannot_cross_scope(self):
        foreign = ProjectFactory(organization=OrganizationFactory())
        self.client.force_authenticate(self.manager)
        for suffix in ('', 'capability/', self.pk + '/', self.pk + '/source/'):
            result = self.client.get(f'/api/projects/{foreign.pk}/second-reviews/' + suffix)
            assert result.status_code in (403, 404), result.data
        result = self.client.get(self.base + f'{self.batch + 100000}/sources/{self.source.pk}/second-review-context/')
        assert result.status_code in (404, 409)
        result = self.client.get(self.second_base + str(uuid4()) + '/')
        assert result.status_code == 404

    def test_B05_revoke_account_membership_role_and_stale_user(self):
        for change in ('account', 'org', 'project', 'role'):
            with self.subTest(change=change), self.settings():
                if change == 'account':
                    administrator = UserFactory(is_staff=True, is_superuser=True)
                    User.objects.filter(pk=self.supervisor.pk).update(is_active=False, session_actor=administrator)
                elif change == 'org':
                    OrganizationMember.objects.filter(organization=self.org, user=self.supervisor).update(
                        deleted_at=timezone.now()
                    )
                elif change == 'project':
                    ProjectMember.objects.filter(project=self.project, user=self.supervisor).update(enabled=False)
                else:
                    ProjectMember.objects.filter(project=self.project, user=self.supervisor).update(role='annotator')
                self.client.force_authenticate(self.supervisor)
                assert self.client.get(self.second_base + self.pk + '/source/').status_code in (401, 403, 404)
                if change == 'account':
                    from users.session_security import reactivate_accounts

                    reactivate_accounts(User.objects.filter(pk=self.supervisor.pk), actor=administrator)
                OrganizationMember.objects.filter(organization=self.org, user=self.supervisor).update(deleted_at=None)
                ProjectMember.objects.filter(project=self.project, user=self.supervisor).update(
                    enabled=True, role='reviewer'
                )

    def test_B06_cancel_without_read_or_organize_does_not_load_answers(self):
        sql, key = [], uuid4()
        body = dict(expected_version=1, reason='Safe stop', expected_viewer_hash=self.body['expected_viewer_hash'])

        def trace(execute, statement, params, many, context):
            sql.append(statement)
            return execute(statement, params, many, context)

        with self.settings(CONFIDENCE_TEST_OPERATIONS=[]), patch(
            'review_batches.second_review_origin.load', side_effect=AssertionError('no answers')
        ), connection.execute_wrapper(trace):
            result = self.post(self.pk + '/cancel/', body, key=key)
            assert result.status_code == 201, result.data
            repeated = self.post(self.pk + '/cancel/', body, key=key)
            assert repeated.status_code == 200 and repeated.data['receipt'] == result.data['receipt']
        assert not any(
            any(
                table in query
                for table in (
                    'tasks_submission',
                    'tasks_reviewdecision',
                    'review_batches_reviewbatch',
                    'result_snapshot',
                    '"proof"',
                )
            )
            for query in sql
        )
        assert SecondReviewTask.objects.get().status == 'cancelled'

    def test_B06_completed_cancel_without_read_does_not_hydrate_opinion(self):
        body = self.opinion_body(self.pk, outcome='concern', reason='Private supervisory opinion')
        result = self.decide(self.pk, body=body)
        assert result.status_code == 201, result.data
        cancel = dict(expected_version=1, reason='Safe stop', expected_viewer_hash=self.body['expected_viewer_hash'])
        with self.settings(CONFIDENCE_TEST_OPERATIONS=[]), patch(
            'review_batches.second_review_origin.load', side_effect=AssertionError('no answers')
        ), CaptureQueriesContext(connection) as queries:
            result = self.post(self.pk + '/cancel/', cancel)
        assert result.status_code == 409, result.data
        opinions = [q['sql'] for q in queries.captured_queries if 'review_batches_secondreviewdecision' in q['sql']]
        assert opinions and all('"reason"' not in q and '"receipt"' not in q for q in opinions), opinions
        assert SecondReviewTask.objects.get(pk=self.pk).status == 'completed'

    def test_B07_new_flag_mode_instance_vendor_and_original_source_gate(self):
        for options in (
            {'ANNOTATION_SECOND_REVIEW_ENABLED': False},
            {'ANNOTATION_SECOND_REVIEW_MODES': {}},
            {'ANNOTATION_SECOND_REVIEW_MODES': {f'{self.org.pk}:{self.project.pk}': 'core_only_internal'}},
            {'ANNOTATION_SECOND_REVIEW_INSTANCE_ID': ''},
        ):
            with self.settings(**options):
                self.client.force_authenticate(self.manager)
                assert self.client.get(self.second_base).status_code == 403
                assert (
                    self.post(
                        self.pk + '/cancel/',
                        dict(expected_version=1, reason='x', expected_viewer_hash=self.body['expected_viewer_hash']),
                    ).status_code
                    == 403
                )
        with patch.object(connection, 'vendor', 'mysql'):
            assert self.client.get(self.second_base).status_code == 403
        with self.settings(ANNOTATION_REVIEW_BATCHES_ENABLED=False):
            assert self.client.get(self.second_base + self.pk + '/').status_code == 200
            assert self.client.get(self.second_base + self.pk + '/source/').status_code in (403, 404)

    def test_B08_strict_client_identity_and_type_fields(self):
        for key in ('actor', 'reviewer', 'status', 'effect', 'result', 'approved', 'host_id', 'source_proof'):
            result = self.post('', {**self.body, key: 'forged'})
            assert result.status_code == 400, (key, result.data)
        for key in ('batch_id', 'submission_id', 'assignee_id'):
            for value in (True, 1.0, str(self.body[key]), None, -1):
                result = self.post('', {**self.body, key: value})
                assert result.status_code == 400, (key, value, result.data)

    def test_B09_first_reviewer_role_loss_does_not_rewrite_history(self):
        ProjectMember.objects.filter(project=self.project, user=self.reviewer).update(role='annotator')
        result = self.decide(self.pk)
        assert result.status_code == 201, result.data

    def test_B10_null_assignee_does_not_rebind(self):
        SecondReviewTask.objects.filter(pk=self.pk).update(assignee=None)
        self.client.force_authenticate(self.supervisor)
        result = self.client.get(self.second_base)
        assert result.status_code == 200 and result.data['count'] == 0
        assert self.client.get(self.second_base + self.pk + '/').status_code == 404
        self.client.force_authenticate(self.manager)
        assert self.client.get(self.second_base + self.pk + '/').status_code == 200
        assert self.client.get(self.second_base + self.pk + '/source/').status_code == 409
