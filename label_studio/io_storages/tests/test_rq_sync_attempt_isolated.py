"""Opt-in RQ integration against a dedicated, loopback-only Redis fixture."""
import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from access_control.project_operations import local_principal
from django.conf import settings
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.urls import get_resolver
from io_storages.admission import claim_storage_dispatch, prepare_storage_dispatch, recover_unstarted_storage
from io_storages.admission_models import StorageOperation
from io_storages.base_models import storage_sync_v2, storage_sync_v2_failure, storage_sync_v3
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
    def test_previous_worker_attempt_cannot_claim_recovered_storage(self):
        old_dir = os.environ.get('ISSUE86_OLD_DURABLE_WORKER_DIR')
        redis_url = os.environ.get('ISSUE86_TEST_REDIS_URL')
        if not old_dir or not redis_url:
            self.skipTest('requires the pinned previous worker and isolated Redis')
        get_resolver().url_patterns
        org = OrganizationFactory()
        project = ProjectFactory(organization=org, title='test-active')
        storage = LocalFilesImportStorage.objects.create(project=project, path='/tmp/issue86-old-rq')
        record = prepare_storage_dispatch(storage, 'import_sync', mark_queued=True)
        claim_storage_dispatch(storage, record.pk, 'import_sync', fenced=True)
        with patch('io_storages.dispatch_recovery.redispatch_queued_operation', side_effect=OSError('broker unavailable')):
            recover_unstarted_storage(project.pk, record.pk, local_principal(org.created_by), reason='worker stopped')
        redis = Redis.from_url(redis_url)
        queue = Queue('issue86-previous-' + uuid.uuid4().hex, connection=redis)
        job = queue.enqueue_call('io_storages.base_models.storage_sync_v2', args=(
            LocalFilesImportStorage, storage.pk, str(record.pk), 'import_sync', 0,
        ))
        code = """
import os, django
django.setup()
from redis import Redis
from rq import Queue, SimpleWorker
r = Redis.from_url(os.environ['ISSUE86_TEST_REDIS_URL'])
SimpleWorker([Queue(os.environ['ISSUE86_TEST_QUEUE'], connection=r)], connection=r).work(
    burst=True, with_scheduler=False, logging_level='WARNING')
"""
        environment = os.environ.copy()
        environment.update(
            POSTGRE_NAME=connection.settings_dict['NAME'], SECRET_KEY=settings.SECRET_KEY,
            ANNOTATION_STORAGE_ADMISSION_ENABLED='true',
            ANNOTATION_PROJECT_OPERATION_PROVIDER='projects.tests.test_lifecycle_task_fence.TitlePolicy',
            ISSUE86_TEST_QUEUE=queue.name, ISSUE86_TEST_REDIS_URL=redis_url,
            DJANGO_SETTINGS_MODULE='core.settings.label_studio',
            PYTHONPATH=os.pathsep.join((str(Path(old_dir).parent), old_dir)), LATEST_VERSION_CHECK='false',
        )
        result = subprocess.run([sys.executable, '-c', code], cwd=old_dir, env=environment,
                                capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        job.refresh()
        self.assertTrue(job.is_failed)
        self.assertIn('STALE_STORAGE_DISPATCH_ATTEMPT', job.exc_info)
        record.refresh_from_db()
        self.assertEqual((record.state, record.dispatch_attempts), ('queued', 1))
        with patch.object(LocalFilesImportStorage, 'scan_and_create_links') as scan:
            storage_sync_v3(LocalFilesImportStorage, storage.pk, record.pk, 'import_sync', 1)
        scan.assert_called_once()
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.COMPLETED)
        redis.close()

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
            storage_sync_v3,
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
            storage_sync_v3,
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
