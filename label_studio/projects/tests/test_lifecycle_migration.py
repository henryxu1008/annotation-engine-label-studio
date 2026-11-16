"""Exercise additive migration and empty-ledger schema rollback on disposable DBs."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.urls import get_resolver
from io_storages.admission_models import StorageOperation
from io_storages.s3.models import S3ExportStorage, S3ExportStorageLink
from organizations.tests.factories import OrganizationFactory
from projects.tests.factories import ProjectFactory
from tasks.tests.factories import AnnotationFactory, TaskFactory
from users.product_tours.models import UserProductTour  # noqa: F401


class LifecycleMigrationTests(TransactionTestCase):
    def setUp(self):
        get_resolver().url_patterns
        self.organization = OrganizationFactory()
        self.project = ProjectFactory(organization=self.organization, is_published=False, is_draft=False)

    def test_upgrade_and_empty_ledger_rollback_preserve_legacy_project(self):
        before = [
            ('projects', '0035_projectmember_role_and_unique'),
            ('io_storages', '0022_normalize_localfiles_paths'),
        ]
        after = [('projects', '0036_project_lifecycle'), ('io_storages', '0024_s3_export_expected_owner')]
        try:
            MigrationExecutor(connection).migrate(before)
            with connection.cursor() as cursor:
                cursor.execute('SELECT is_draft, is_published FROM project WHERE id=%s', [self.project.pk])
                self.assertEqual(tuple(cursor.fetchone()), (False, False))
            MigrationExecutor(connection).migrate(after)
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT is_draft, is_published, lifecycle_state, lifecycle_revision FROM project WHERE id=%s',
                    [self.project.pk],
                )
                self.assertEqual(tuple(cursor.fetchone()), (False, False, 'legacy', 0))
                if connection.vendor == 'sqlite':
                    cursor.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name='concern_followup_delete'"
                    )
                    self.assertEqual(cursor.fetchone()[0], 1)
        finally:
            MigrationExecutor(connection).migrate(after)

    def test_nonempty_storage_ledger_blocks_schema_rollback_without_data_loss(self):
        record = StorageOperation.objects.create(
            project=self.project,
            storage_model='io_storages.test',
            storage_id=1,
            operation='import_sync',
            lifecycle_revision=0,
            state=StorageOperation.State.QUEUED,
        )
        with self.assertRaisesRegex(RuntimeError, 'storage operations or S3 version bindings'):
            MigrationExecutor(connection).migrate([('io_storages', '0022_normalize_localfiles_paths')])
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.QUEUED)

    def test_mixed_annotation_outbox_ledger_blocks_rollback_without_losing_either_version(self):
        records = [
            StorageOperation.objects.create(
                project=self.project,
                storage_model='io_storages.s3exportstorage',
                storage_id=1,
                operation=operation,
                lifecycle_revision=0,
                subject_id=17,
                state=StorageOperation.State.QUEUED,
            )
            for operation in ('annotation_export_queued', 'annotation_export_v3_queued')
        ]
        with self.assertRaisesRegex(RuntimeError, 'storage operations or S3 version bindings'):
            MigrationExecutor(connection).migrate([('io_storages', '0022_normalize_localfiles_paths')])
        for record, expected in zip(records, ('annotation_export_queued', 'annotation_export_v3_queued')):
            record.refresh_from_db()
            self.assertEqual(
                (record.operation, record.state, record.subject_id), (expected, StorageOperation.State.QUEUED, 17)
            )

    def test_nonempty_webhook_payload_survives_rejected_storage_rollback(self):
        payload = {'action': 'LABEL_LINK_UPDATED', 'label_link': {'id': 17, 'from_name': 'synthetic'}}
        record = StorageOperation.objects.create(
            project=self.project,
            storage_model='webhooks.webhook',
            storage_id=42,
            operation='webhook_label_link_updated',
            lifecycle_revision=0,
            state=StorageOperation.State.QUEUED,
            effect_payload=payload,
            target_storage_binding='a' * 64,
            dispatch_attempts=2,
        )
        with self.assertRaisesRegex(RuntimeError, 'storage operations or S3 version bindings'):
            MigrationExecutor(connection).migrate([('io_storages', '0022_normalize_localfiles_paths')])
        record.refresh_from_db()
        self.assertEqual(record.effect_payload, payload)
        self.assertEqual(record.target_storage_binding, 'a' * 64)
        self.assertEqual(
            (record.operation, record.state, record.dispatch_attempts),
            ('webhook_label_link_updated', StorageOperation.State.QUEUED, 2),
        )

    def test_classified_project_blocks_lifecycle_schema_rollback(self):
        self.project.lifecycle_state = 'paused'
        self.project.lifecycle_revision = 1
        self.project.save(update_fields=['lifecycle_state', 'lifecycle_revision'])
        try:
            with self.assertRaisesRegex(RuntimeError, 'lifecycle state or events'):
                MigrationExecutor(connection).migrate([('projects', '0035_projectmember_role_and_unique')])
        finally:
            # A dependent empty storage migration may already have been unapplied.
            MigrationExecutor(connection).migrate([('io_storages', '0024_s3_export_expected_owner')])
        self.project.refresh_from_db()
        self.assertEqual((self.project.lifecycle_state, self.project.lifecycle_revision), ('paused', 1))

    def test_pinned_s3_version_blocks_storage_schema_rollback(self):
        annotation = AnnotationFactory(task=TaskFactory(project=self.project), project=self.project)
        storage = S3ExportStorage.objects.create(project=self.project, bucket='migration-test')
        link = S3ExportStorageLink.objects.create(
            storage=storage,
            annotation=annotation,
            pinned_key='key',
            pinned_version_id='version',
            pinned_storage_binding='a' * 64,
        )
        with self.assertRaisesRegex(RuntimeError, 'storage operations or S3 version bindings'):
            MigrationExecutor(connection).migrate([('io_storages', '0022_normalize_localfiles_paths')])
        link.refresh_from_db()
        self.assertEqual(link.pinned_version_id, 'version')

    def test_configured_owner_blocks_downgrade_and_survives(self):
        storage = S3ExportStorage.objects.create(
            project=self.project,
            bucket='migration-test',
            expected_bucket_owner='123456789012',
        )
        with self.assertRaisesRegex(RuntimeError, 'bucket owner bindings are configured'):
            MigrationExecutor(connection).migrate([('io_storages', '0023_storage_operation')])
        storage.refresh_from_db()
        self.assertEqual(storage.expected_bucket_owner, '123456789012')
