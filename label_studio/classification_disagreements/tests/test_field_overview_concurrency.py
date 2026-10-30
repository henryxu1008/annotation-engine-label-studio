"""Independent PostgreSQL writers after render, final counters and SQL preflight."""
import copy
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from classification_disagreements import field_api, fields
from classification_disagreements import field_batches as batches
from classification_disagreements import field_overview as overview
from classification_disagreements.tests import test_field_overview as f
from django.db import close_old_connections, connection, transaction
from django.db.models.query import QuerySet
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from organizations.models import Organization, OrganizationMember
from projects.models import Project, ProjectMember
from rest_framework.response import Response
from rest_framework.test import APIClient
from tasks.models import Submission, Task, TaskAssignment
from tasks.submissions import _snapshot_hash
from users.models import User


@override_settings(
    ANNOTATION_FIELD_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_FIELD_BATCHES_ENABLED=True,
    ANNOTATION_FIELD_QUALITY_OVERVIEW_ENABLED=True,
)
class TestFieldOverviewConcurrency(TransactionTestCase):
    client_class = APIClient
    make_source = f.TestFieldOverview.make_source
    write = f.TestFieldOverview.write
    extra_task = f.TestFieldOverview.extra_task
    preview = f.TestFieldOverview.preview
    create = f.TestFieldOverview.create
    freeze = f.TestFieldOverview.freeze
    read = f.TestFieldOverview.read

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Requires real PostgreSQL independent connections')
        f.TestFieldOverview.setUpTestData.__func__(self)
        f.TestFieldOverview.setUp(self)

    def writer(self, change):
        close_old_connections()
        try:
            change()
        finally:
            connection.close()

    def commit(self, change):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(self.writer, change).result(20)

    def interleave(self, change, expected, *, stage='render', remap=False):
        rendered, equal, iterate, resolve = Response.render, fields.equal, QuerySet.iterator, field_api.resolve_actor
        entered, calls, loads = False, 0, 0

        def once():
            nonlocal entered
            if not entered:
                entered = True
                self.commit(change)

        def render(response):
            result = rendered(response)
            if (
                stage == 'render'
                and isinstance(response.data, dict)
                and response.data.get('schema') == overview.SCHEMA
            ):
                once()
            return result

        def compare(*args):
            nonlocal calls
            calls += 1
            if stage == 'last_counter' and calls == 12:
                once()
            return equal(*args)

        def iterator(query, *args, **kwargs):
            nonlocal loads
            if query.model is Submission:
                loads += 1
                if (stage == 'first_hydrate' and loads == 1) or (stage == 'last_hydrate' and loads == 2):
                    once()
            return iterate(query, *args, **kwargs)

        def actor(request):
            principal, current = resolve(request)
            if remap and Project.objects.get(pk=self.project.pk).description == 'remap':
                return principal, self.reviewer
            return principal, current

        with (
            patch.object(Response, 'render', render),
            patch.object(fields, 'equal', compare),
            patch.object(QuerySet, 'iterator', iterator),
            patch.object(field_api, 'resolve_actor', actor),
        ):
            response = self.read()
        self.assertTrue(entered)
        self.assertIn(response.status_code, expected, response.data)
        if response.status_code != 200:
            self.assertNotIn('summary', response.data)
            self.assertNotIn('annotators', response.data)
        return response, calls, loads

    def test_membership_revocation_after_render(self):
        self.interleave(
            lambda: OrganizationMember.objects.filter(user=self.manager, organization=self.org).update(
                deleted_at=timezone.now()
            ),
            (401, 403, 404),
        )

    @override_settings(
        ANNOTATION_PROJECT_OPERATION_PROVIDER='classification_disagreements.tests.test_field_batch_concurrency.ReadPolicy'
    )
    def test_read_work_withdrawal_after_render(self):
        self.interleave(lambda: Project.objects.filter(pk=self.project.pk).update(description='deny-read'), (403,))

    def test_account_disabled_after_render(self):
        self.interleave(lambda: User.objects.filter(pk=self.manager.pk).update(is_active=False), (401, 403, 404))

    def test_same_role_actor_mapping_change_after_render(self):
        ProjectMember.objects.filter(project=self.project, user=self.reviewer).update(role='manager')
        self.interleave(
            lambda: Project.objects.filter(pk=self.project.pk).update(description='remap'), (403, 404, 409), remap=True
        )

    def test_source_deleted_after_render(self):
        self.interleave(lambda: Submission.objects.filter(pk=self.first.pk).delete(), (409,))

    def test_source_hash_corrupted_after_render(self):
        self.interleave(lambda: Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64), (409,))

    def test_source_snapshot_rehashed_after_render(self):
        value = copy.deepcopy(self.first.result_snapshot)
        value['annotation']['result'][0]['value']['text'] = ['changed']
        self.interleave(
            lambda: Submission.objects.filter(pk=self.first.pk).update(
                result_snapshot=value, result_hash=_snapshot_hash(value)
            ),
            (409,),
        )

    def test_assignment_scope_rebound_after_render(self):
        other = Task.objects.create(project=self.project, data={'text': 'Other synthetic scope'})
        self.interleave(lambda: TaskAssignment.objects.filter(pk=self.first.assignment_id).update(task=other), (409,))

    def test_new_revision_after_render_keeps_frozen_input(self):
        before = self.read().data

        def append():
            row = self.make_source(assignment=self.first.assignment, revision=2)
            self.write(row, ('new',) * 3)
            Submission.objects.filter(pk=self.first.pk).update(status=Submission.Status.SUPERSEDED)

        result, _, _ = self.interleave(append, (200,))
        for key in ('scope', 'source_set_hash', 'scope_counts', 'summary', 'annotators', 'result_hash'):
            self.assertEqual(result.data[key], before[key])

    def move_organization_after_final_hydration(self, *, replay):
        first = self.read().data if replay else None
        owner = User.objects.create_user('scope-owner@synthetic.invalid', None, username='scope-owner')
        other = Organization.create_organization(created_by=owner, title='Synthetic scope migration')
        other.add_user(self.manager)
        User.objects.filter(pk=self.manager.pk).update(active_organization=self.org)
        original, calls = batches.inspect_items, 0

        def move():
            with transaction.atomic():
                Project.objects.filter(pk=self.project.pk).update(organization=other)
                User.objects.filter(pk=self.manager.pk).update(active_organization=other)

        def inspect(*args):
            nonlocal calls
            result = original(*args)
            calls += 1
            if calls == 2:
                self.commit(move)
            return result

        with self.settings(
            ANNOTATION_FIELD_DISAGREEMENT_MODES={
                f'{self.org.pk}:{self.project.pk}': 'fork_review',
                f'{other.pk}:{self.project.pk}': 'fork_review',
            }
        ), patch.object(batches, 'inspect_items', inspect):
            response = self.read(**({'frozen': first['frozen']} if replay else {}))
        self.assertEqual(calls, 2)
        self.assertIn(response.status_code, (403, 404, 409), response.data)
        self.assertNotIn('summary', response.data)
        self.assertNotIn('annotators', response.data)

    def test_organization_change_after_final_hydration_denies_original_viewer(self):
        self.move_organization_after_final_hydration(replay=False)

    def test_organization_change_after_final_hydration_denies_frozen_replay(self):
        self.move_organization_after_final_hydration(replay=True)

    def test_source_changed_during_last_equality_is_checked_after_counter_without_third_pass(self):
        with patch.object(field_api, 'load', wraps=field_api.load) as loader:
            _, calls, _ = self.interleave(
                lambda: Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64),
                (409,),
                stage='last_counter',
            )
        self.assertEqual(calls, 12)
        self.assertEqual(loader.call_count, 2)

    def test_revocation_during_last_equality_still_denies(self):
        self.interleave(
            lambda: OrganizationMember.objects.filter(user=self.manager, organization=self.org).update(
                deleted_at=timezone.now()
            ),
            (401, 403, 404),
            stage='last_counter',
        )

    def growth(self):
        value = copy.deepcopy(self.first.result_snapshot)
        value['padding'] = 'x' * 8192
        Submission.objects.filter(pk=self.first.pk).update(result_snapshot=value, result_hash=_snapshot_hash(value))

    def test_growth_after_first_predecode_preflight_performs_zero_calculations(self):
        with patch.object(fields, 'MAX_STORED_BYTES', 8192), patch.object(
            fields, 'normalize', wraps=fields.normalize
        ) as n:
            self.interleave(self.growth, (422,), stage='first_hydrate')
        self.assertEqual(n.call_count, 0)

    def test_growth_at_final_hydration_reports_that_both_counter_passes_already_happened(self):
        with patch.object(fields, 'MAX_STORED_BYTES', 8192), patch.object(
            fields, 'normalize', wraps=fields.normalize
        ) as n:
            _, calls, _ = self.interleave(self.growth, (422,), stage='last_hydrate')
        self.assertEqual(calls, 12)
        self.assertEqual(n.call_count, 12)
