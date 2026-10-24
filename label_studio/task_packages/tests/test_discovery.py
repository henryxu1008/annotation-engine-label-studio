from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from access_control.identity import Principal
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from projects.models import ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.test import APITestCase
from task_packages.access import current_read_policy
from tasks.models import TaskAssignment

from .test_packages import PackageFixture


@override_settings(ANNOTATION_FIXED_PACKAGES_ENABLED=True)
class TestPackageDiscovery(PackageFixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.a = self.create(name='Package A', task_ids=[self.tasks[0].pk, self.tasks[1].pk]).json()['id']
        self.b = self.create(name='Package B', task_ids=[self.tasks[1].pk, self.tasks[2].pk]).json()['id']
        self.first = self.assignment(task=self.tasks[0])
        self.shared = self.assignment(task=self.tasks[1])
        self.last = self.assignment(task=self.tasks[2], user=self.other)
        self.submission(self.shared, status='rejected')
        self.work_url = f'/api/projects/{self.project.pk}/my-work/'

    def packages(self):
        response = self.client.get(self.url)
        assert response.status_code == 200, response.content
        assert response['Cache-Control'] == 'private, no-store'
        return {row['id']: row for row in response.json()['results']}

    def scoped_work(self, **params):
        return self.client.get(self.work_url, {'task_package': self.a, **params})

    def test_batched_summaries_match_details_without_adding_overlapping_packages(self):
        for actor in [self.manager, self.annotator, self.reviewer]:
            self.client.force_authenticate(actor)
            rows = self.packages()
            for package_id, row in rows.items():
                assert row['counts'] == self.detail(package_id).json()['counts']
        self.client.force_authenticate(self.annotator)
        rows = self.packages()
        assert rows[self.a]['counts']['assignments'] == 2
        assert rows[self.b]['counts']['assignments'] == 1
        ordinary = self.client.get(self.work_url).json()
        assert ordinary['counts']['assigned'] + ordinary['counts']['rework'] == 2
        assert 'fixed_tasks' not in rows[self.a]['counts']
        assert 'source_note' not in rows[self.a]
        assert b'PRIVATE' not in self.client.get(self.url).content

    def test_manager_samples_and_assignment_units_remain_separate(self):
        self.assignment(task=self.tasks[1], user=self.other)
        rows = self.packages()
        assert rows[self.a]['counts']['fixed_tasks'] == 2
        assert rows[self.a]['counts']['assignments'] == 3
        assert rows[self.b]['counts']['fixed_tasks'] == 2
        assert rows[self.b]['counts']['assignments'] == 3
        ProjectMember.objects.filter(project=self.project, user=self.other).update(enabled=False)
        assert self.packages()[self.b]['counts']['blocked_assignments'] == 2
        self.tasks[2].delete()
        counts = self.packages()[self.b]['counts']
        assert counts['fixed_tasks'] == 2 and counts['unavailable_tasks'] == 1

    def test_summary_queries_do_not_grow_per_package(self):
        with CaptureQueriesContext(connection) as small:
            self.packages()
        for index in range(12):
            self.create(name=f'Extra {index}', task_ids=[self.tasks[0].pk])
        with CaptureQueriesContext(connection) as many:
            self.packages()
        assert len(many) <= len(small) + 2, (len(small), len(many))

    def test_detail_state_is_applied_before_pagination_and_keeps_full_counts(self):
        for actor in [self.manager, self.annotator]:
            self.client.force_authenticate(actor)
            result = self.client.get(f'{self.url}{self.a}/', {'state': 'rework', 'page_size': 1})
            assert result.status_code == 200, result.content
            data = result.json()
            assert data['count'] == 1 and data['results'][0]['task_id'] == self.tasks[1].pk
            assert data['counts']['assignments'] == 2
            assert data['counts']['assigned'] == data['counts']['rework'] == 1
            assert data['state'] == 'rework' and data['continue_url'] is None

    def test_manager_unassigned_unavailable_and_retained_pending_filters(self):
        self.first.status = 'cancelled'
        self.first.save(update_fields=['status'])
        self.submission(self.shared, revision=2)
        self.shared.status = 'cancelled'
        self.shared.save(update_fields=['status'])
        pending = self.client.get(f'{self.url}{self.a}/', {'state': 'pending_review'}).json()
        assert [row['task_id'] for row in pending['results']] == [self.tasks[1].pk]
        unassigned = self.client.get(f'{self.url}{self.a}/', {'state': 'unassigned'}).json()
        assert unassigned['count'] == 2
        self.tasks[0].delete()
        unavailable = self.client.get(f'{self.url}{self.a}/', {'state': 'unavailable'}).json()
        assert unavailable['count'] == 1 and unavailable['results'][0]['available'] is False

    def test_invalid_repeated_and_role_inapplicable_states_are_denied(self):
        self.client.force_authenticate(self.annotator)
        for state in ['unassigned', 'unavailable', 'pending_review', '', 'unknown']:
            assert self.client.get(f'{self.url}{self.a}/', {'state': state}).status_code == 400
        assert self.client.get(f'{self.url}{self.a}/?state=assigned&state=rework').status_code == 400
        assert self.client.get(self.url, {'state': 'assigned'}).status_code == 400

    def test_selected_my_work_is_personal_and_overlapping_items_are_unique(self):
        self.client.force_authenticate(self.annotator)
        result = self.scoped_work().json()
        assert len(result['results']) == 2
        assert len({row['assignment_id'] for row in result['results']}) == 2
        assert all(f'task_package={self.a}' in row['editor_url'] for row in result['results'])
        assert result['task_package'] == {'id': self.a, 'name': 'Package A'}
        assert f'task_package={self.a}' in result['continue_url']
        filtered = self.scoped_work(state='rework', page_size=1).json()
        assert filtered['count'] == 1 and filtered['results'][0]['task_id'] == self.tasks[1].pk
        assert filtered['counts']['assigned'] == 1 and filtered['continue_url'] is None
        self.client.force_authenticate(self.manager)
        assert self.scoped_work().json()['count'] == 0

    def test_selected_review_work_keeps_cancelled_sources_and_excludes_self(self):
        self.submission(self.first)
        self.first.status = 'cancelled'
        self.first.save(update_fields=['status'])
        own = self.assignment(task=self.tasks[0], user=self.reviewer)
        self.submission(own)
        self.client.force_authenticate(self.reviewer)
        result = self.scoped_work()
        assert result.status_code == 200, result.content
        assert [row['assignment_id'] for row in result.json()['results']] == [self.first.pk]
        assert result.json()['results'][0]['source_cancelled']
        rows = self.packages()
        assert rows[self.a]['counts'] == {'pending_review': 1}
        assert self.client.get(f'{self.url}{self.a}/', {'state': 'assigned'}).status_code == 400

    def test_scope_swapping_disabled_feature_and_revocation_never_fall_back(self):
        self.client.force_authenticate(self.annotator)
        assert self.client.get(self.work_url, {'task_package': 999999}).status_code == 404
        assert self.client.get(f'{self.work_url}?task_package={self.a}&task_package={self.b}').status_code == 400
        assert self.client.get(f'{self.work_url}?task_package={self.a}&state=assigned&state=rework').status_code == 400
        self.client.force_authenticate(self.manager)
        foreign = ProjectFactory(organization=self.organization, created_by=self.manager)
        assert self.client.get(f'/api/projects/{foreign.pk}/my-work/', {'task_package': self.a}).status_code == 404
        self.client.force_authenticate(self.annotator)
        with override_settings(ANNOTATION_FIXED_PACKAGES_ENABLED=False):
            assert self.scoped_work().status_code == 404
            assert self.client.get(self.work_url).status_code == 200
        ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=False)
        assert self.scoped_work().status_code in (403, 404)

    def test_mapped_actor_is_used_for_selected_my_work(self):
        self.client.force_authenticate(self.manager)
        principal = Principal(f'test:{self.annotator.pk}', 'test', 'mapped', self.annotator.pk)
        with patch('access_control.identity.get_identity_provider') as provider:
            provider.return_value.resolve.return_value = principal
            result = self.scoped_work()
        assert result.status_code == 200, result.content
        assert {row['assignment_id'] for row in result.json()['results']} == {self.first.pk, self.shared.pk}

    def test_scoped_my_work_rechecks_materialized_assignment_versions(self):
        self.client.force_authenticate(self.annotator)
        from task_packages import access

        original = access.current_read_policy

        def rotate(*args, **kwargs):
            TaskAssignment.objects.filter(pk=self.first.pk).update(version=self.first.version + 1)
            return original(*args, **kwargs)

        with patch('task_packages.scope.current_read_policy', side_effect=original), patch(
            'task_packages.access.current_read_policy', side_effect=rotate
        ):
            response = self.scoped_work()
        assert response.status_code == 403, response.content
        assert b'editor_url' not in response.content

    def test_selected_my_work_refreshes_operation_policy_without_new_rights(self):
        self.client.force_authenticate(self.annotator)

        def pause(*args, **kwargs):
            self.project.description = 'read-only'
            self.project.save(update_fields=['description'])
            return current_read_policy(*args, **kwargs)

        with override_settings(
            ANNOTATION_PROJECT_OPERATION_PROVIDER='task_packages.tests.test_packages.ChangedProjectPolicy'
        ):
            with patch('task_packages.access.current_read_policy', side_effect=pause):
                response = self.scoped_work()
        assert response.status_code == 200, response.content
        assert response.json()['continue_url'] is None
        assert all(not row['allowed_actions'] and row['editor_url'] is None for row in response.json()['results'])

    def test_browser_fixture_refuses_non_test_mode_without_writes(self):
        with TemporaryDirectory() as directory, override_settings(TEST_ENVIRONMENT=False):
            with self.assertRaises(CommandError):
                call_command('seed_package_discovery_e2e', output=str(Path(directory) / 'fixture.json'))
        assert TaskAssignment.objects.count() == 3

    def test_browser_fixture_refuses_existing_output_without_writes(self):
        with TemporaryDirectory() as directory, override_settings(TEST_ENVIRONMENT=True):
            output = Path(directory) / 'fixture.json'
            output.write_text('retained')
            with self.assertRaises(CommandError):
                call_command('seed_package_discovery_e2e', output=str(output))
            assert output.read_text() == 'retained'
        assert TaskAssignment.objects.count() == 3
