"""Opt-in RQ integration against a dedicated, loopback-only Redis fixture."""
import os
import unittest
import uuid
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.urls import get_resolver
from io_storages.admission import prepare_storage_dispatch
from io_storages.admission_models import StorageOperation
from io_storages.base_models import storage_sync_v2, storage_sync_v2_failure
from io_storages.localfiles.models import LocalFilesImportStorage
from organizations.tests.factories import OrganizationFactory
from projects.tests.factories import ProjectFactory
from redis import Redis
from rq import Queue, SimpleWorker


@unittest.skipUnless(os.environ.get('ISSUE86_REDIS_ISOLATION_TEST') == '1', 'requires disposable Redis')
@override_settings(
    ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
    ANNOTATION_PROJECT_OPERATION_PROVIDER='projects.tests.test_lifecycle_task_fence.TitlePolicy',
)
class IsolatedRQSyncAttemptTests(TransactionTestCase):
    def test_old_payload_is_fenced_and_reserved_attempt_runs_once(self):
        get_resolver().url_patterns
        org = OrganizationFactory()
        project = ProjectFactory(organization=org, title='test-active')
        storage = LocalFilesImportStorage.objects.create(project=project, path='/tmp/issue86-rq-fixture')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        StorageOperation.objects.filter(pk=record.pk).update(dispatch_attempts=1)
        # The Redis endpoint and credentials are deliberately not configurable:
        # only a disposable service in this container's network may be used.
        redis = Redis(host='127.0.0.1', port=6379)
        queue = Queue('issue86-' + uuid.uuid4().hex, connection=redis, default_timeout=30)
        old = queue.enqueue(
            storage_sync_v2,
            LocalFilesImportStorage,
            storage.pk,
            str(record.pk),
            'import_sync',
            on_failure=storage_sync_v2_failure,
        )
        current = queue.enqueue(
            storage_sync_v2,
            LocalFilesImportStorage,
            storage.pk,
            str(record.pk),
            'import_sync',
            1,
            on_failure=storage_sync_v2_failure,
        )
        with patch.object(LocalFilesImportStorage, 'scan_and_create_links') as scan:
            SimpleWorker([queue], connection=redis).work(burst=True, with_scheduler=False, logging_level='WARNING')
            scan.assert_called_once()
        old_status, current_status = old.get_status(refresh=True), current.get_status(refresh=True)
        self.assertEqual(getattr(old_status, 'value', old_status), 'failed')
        self.assertEqual(getattr(current_status, 'value', current_status), 'finished')
        record.refresh_from_db()
        self.assertEqual((record.state, record.dispatch_attempts), (StorageOperation.State.COMPLETED, 1))
        redis.close()

    def test_claimed_failure_marks_only_matching_storage_and_keeps_ledger_uncertain(self):
        get_resolver().url_patterns
        org = OrganizationFactory()
        project = ProjectFactory(organization=org, title='test-active')
        storage = LocalFilesImportStorage.objects.create(project=project, path='/tmp/issue86-rq-failure')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        redis = Redis(host='127.0.0.1', port=6379)
        queue = Queue('issue86-' + uuid.uuid4().hex, connection=redis, default_timeout=30)
        job = queue.enqueue(
            storage_sync_v2,
            LocalFilesImportStorage,
            storage.pk,
            str(record.pk),
            'import_sync',
            0,
            on_failure=storage_sync_v2_failure,
        )

        def fail_after_claim(instance):
            instance.info_set_in_progress()
            raise TimeoutError('synthetic provider timeout')

        with patch.object(
            LocalFilesImportStorage, 'scan_and_create_links', autospec=True, side_effect=fail_after_claim
        ):
            SimpleWorker([queue], connection=redis).work(burst=True, with_scheduler=False, logging_level='WARNING')
        state = job.get_status(refresh=True)
        self.assertEqual(getattr(state, 'value', state), 'failed')
        record.refresh_from_db()
        storage.refresh_from_db()
        self.assertEqual((record.state, storage.status), (StorageOperation.State.UNCERTAIN, 'failed'))
        redis.close()
