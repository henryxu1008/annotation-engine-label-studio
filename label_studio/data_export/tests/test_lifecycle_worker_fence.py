"""Old queued export entries must not begin work after admission is enabled."""
from unittest.mock import patch

from data_export import approved, review_audit, snapshots
from data_export.api import async_convert
from data_export.approved_models import ApprovedDatasetRelease
from data_export.mixins import export_background
from data_export.models import ConvertedFormat, Export
from data_export.snapshot_models import ReleaseSnapshot
from django.db import connection, transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.urls import get_resolver
from io_storages.admission import lock_drained_project
from organizations.tests.factories import OrganizationFactory
from projects.tests.factories import ProjectFactory
from rest_framework.exceptions import PermissionDenied, ValidationError


@override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
class LegacyExportWorkerFenceTests(SimpleTestCase):
    def test_dispatch_callbacks_leave_existing_receipts_unmodified(self):
        # No database or broker access is allowed on these paths.
        with patch.object(approved, 'api_queue') as queue:
            self.assertIsNone(approved.dispatch('unknown-release'))
            self.assertIsNone(snapshots.dispatch('unknown-snapshot'))
            self.assertIsNone(review_audit.enqueue('unknown-audit'))
        queue.assert_not_called()

    def test_queued_workers_fail_before_database_or_external_effects(self):
        cases = (
            (export_background, (123, None, None, None)),
            (async_convert, (123, 'CSV', None, 'https://synthetic.invalid/')),
            (approved.generate, ('unknown-release', 'unknown-token')),
            (snapshots.generate, ('unknown-snapshot', 'unknown-token')),
            (review_audit.generate, ('unknown-audit', 'unknown-token')),
            (review_audit.generate_bound_v1, ('unknown-audit', 'unknown-token')),
        )
        for function, args in cases:
            with self.subTest(worker=function.__module__ + '.' + function.__name__), self.assertRaises(
                PermissionDenied
            ):
                function(*args)

    def test_internal_creation_and_retry_paths_cannot_bypass_api_fence(self):
        cases = (
            (approved.freeze, (None, None, None, None, None)),
            (snapshots.create, (None, None, None, None, None)),
            (snapshots.retry, (None, None)),
            (review_audit.capture, (None, None, None, None)),
            (review_audit.retry, (None, None, None, None)),
        )
        for function, args in cases:
            with self.subTest(function=function.__name__), self.assertRaises(PermissionDenied):
                function(*args)


@override_settings(ANNOTATION_STORAGE_ADMISSION_ENABLED=True)
class LegacyExportReceiptFenceTests(TransactionTestCase):
    def setUp(self):
        # Register lazily loaded storage models before TransactionTestCase flush.
        get_resolver().url_patterns

    def test_existing_receipts_remain_visible_and_block_transition(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires PostgreSQL row locks')
        org = OrganizationFactory()
        project = ProjectFactory(organization=org)
        export = Export.objects.create(project=project, status=Export.Status.IN_PROGRESS)
        conversion = ConvertedFormat.objects.create(export=export, export_type='CSV')
        release = ApprovedDatasetRelease.objects.create(
            project=project,
            organization_id_frozen=org.pk,
            actor_id_frozen=org.created_by.pk,
            principal_id='synthetic',
            idempotency_key='synthetic',
            request_hash='a',
            frozen_input={},
            input_sha256='a',
            content_sha256='a',
        )
        snapshot = ReleaseSnapshot.objects.create(
            project=project,
            release=release,
            actor_id=org.created_by.pk,
            principal_id='synthetic',
            idempotency_key='synthetic',
            policy={},
        )
        for function, args in (
            (export_background, (export.pk, None, None, None)),
            (async_convert, (conversion.pk, 'CSV', project, 'https://synthetic.invalid/')),
            (approved.generate, (str(release.pk), 'synthetic-token')),
            (snapshots.generate, (str(snapshot.pk), str(snapshot.token))),
        ):
            with self.subTest(worker=function.__name__), self.assertRaises(PermissionDenied):
                function(*args)
        approved.dispatch(release.pk)
        snapshots.dispatch(snapshot.pk)
        export.refresh_from_db()
        conversion.refresh_from_db()
        release.refresh_from_db()
        snapshot.refresh_from_db()
        self.assertEqual(export.status, Export.Status.IN_PROGRESS)
        self.assertEqual(conversion.status, ConvertedFormat.Status.CREATED)
        self.assertEqual(release.state, 'dispatch_pending')
        self.assertEqual(snapshot.state, 'dispatch_pending')
        with self.assertRaisesRegex(ValidationError, 'EXPORT_NOT_DRAINED'):
            with transaction.atomic():
                lock_drained_project(project.pk)
