"""Task-created webhook admission and failure recovery."""
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from access_control.project_operations import local_principal
from django.conf import settings
from django.core.management import call_command
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import get_resolver
from django.utils import timezone
from io_storages.admission import lock_drained_project, recover_unsubmitted_webhook
from io_storages.admission_models import StorageOperation, StorageOperationRecovery
from io_storages.dispatch_recovery import redispatch_queued_operation
from io_storages.localfiles.models import LocalFilesImportStorage, LocalFilesImportStorageLink
from io_storages.utils import StorageObject
from labels_manager.models import Label, LabelLink
from labels_manager.serializers import LabelCreateSerializer
from organizations.tests.factories import OrganizationFactory
from projects.models import Project, ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIClient
from tasks.models import Annotation, Task, TaskAssignment
from tasks.tests.factories import TaskFactory
from users.tests.factories import UserFactory
from webhooks.durable import (
    WEBHOOK_PRE_SEND,
    WEBHOOK_SUBMITTING,
    deliver_project_event_v3,
    deliver_project_event_v4,
    deliver_task_created_v2,
    deliver_task_created_v4,
    queue_project_event,
)
from webhooks.models import Webhook, WebhookAction


@override_settings(
    ANNOTATION_STORAGE_ADMISSION_ENABLED=True,
    ANNOTATION_PROJECT_OPERATION_PROVIDER='projects.tests.test_lifecycle_task_fence.TitlePolicy',
)
class TaskCreatedWebhookOutboxTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('requires PostgreSQL row locks')
        get_resolver().url_patterns
        self.org = OrganizationFactory()
        self.project = ProjectFactory(organization=self.org, title='test-active')
        self.hook = Webhook.objects.create(
            organization=self.org,
            project=self.project,
            url='https://synthetic.invalid/hook',
            headers={'Authorization': 'synthetic-only'},
            is_active=True,
            send_payload=True,
            send_for_all_actions=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.org.created_by)
        self.url = f'/api/projects/{self.project.pk}/tasks/'

    def create_queued(self):
        with patch('webhooks.durable.dispatch_webhook', side_effect=OSError('broker unavailable')):
            response = self.client.post(self.url, {'data': {'text': 'synthetic'}}, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        return StorageOperation.objects.get(project=self.project, operation='webhook_task_created')

    def test_lost_dispatch_is_durable_and_success_claims_once(self):
        record = self.create_queued()
        self.assertEqual(record.state, StorageOperation.State.QUEUED)
        self.assertEqual(record.effect_payload['action'], 'TASKS_CREATED')
        self.assertEqual(len(record.effect_payload['tasks']), 1)
        with transaction.atomic(), self.assertRaises(ValidationError):
            lock_drained_project(self.project.pk)
        with patch('webhooks.durable.requests.post', return_value=MagicMock(status_code=204)) as send:
            deliver_task_created_v2(record.pk)
            with self.assertRaises(ValidationError):
                deliver_task_created_v2(record.pk)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.kwargs['headers']['X-Label-Studio-Delivery-ID'], str(record.pk))
        self.assertIs(send.call_args.kwargs['allow_redirects'], False)
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.COMPLETED)
        with transaction.atomic():
            lock_drained_project(self.project.pk)

    def test_storage_import_commits_task_and_webhook_intent_together(self):
        storage = LocalFilesImportStorage.objects.create(project=self.project, path='/tmp/issue86-webhook-source')
        obj = StorageObject(task_data={'data': {'text': 'synthetic'}}, key='synthetic.json')
        with patch('webhooks.durable.dispatch_webhook', side_effect=OSError('broker unavailable')):
            task = storage.add_task(self.project, 1, 1, storage, obj, LocalFilesImportStorageLink)
        self.assertTrue(Task.objects.filter(pk=task.pk, project=self.project).exists())
        record = StorageOperation.objects.get(project=self.project, operation='webhook_task_created')
        self.assertEqual(record.state, StorageOperation.State.QUEUED)

    def test_source_rollback_removes_intent_without_dispatch(self):
        with patch('webhooks.durable.dispatch_webhook') as dispatch:
            with self.assertRaisesRegex(RuntimeError, 'rollback'):
                with transaction.atomic():
                    response = self.client.post(self.url, {'data': {'text': 'synthetic'}}, format='json')
                    self.assertEqual(response.status_code, 201, response.content)
                    self.assertTrue(StorageOperation.objects.filter(operation='webhook_task_created').exists())
                    raise RuntimeError('rollback')
            dispatch.assert_not_called()
        self.assertFalse(Task.objects.filter(project=self.project).exists())
        self.assertFalse(StorageOperation.objects.filter(project=self.project).exists())

    def test_network_timeout_is_uncertain_and_not_retried(self):
        record = self.create_queued()
        with patch('webhooks.durable.requests.post', side_effect=TimeoutError('synthetic timeout')) as send:
            with self.assertRaises(TimeoutError):
                deliver_task_created_v2(record.pk)
            with self.assertRaises(ValidationError):
                deliver_task_created_v2(record.pk)
        self.assertEqual(send.call_count, 1)
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.UNCERTAIN)
        StorageOperation.objects.filter(pk=record.pk).update(started_at=timezone.now() - timedelta(minutes=2))
        with patch('io_storages.management.commands.replay_storage_outbox.redispatch_queued_operation') as replay:
            call_command('replay_storage_outbox', project_id=self.project.pk)
        replay.assert_not_called()

    def test_recovery_fences_old_claim_even_if_new_worker_has_reclaimed_same_delivery(self):
        from webhooks import durable

        record = self.create_queued()
        first_ready, second_ready = threading.Event(), threading.Event()
        release_first, release_second = threading.Event(), threading.Event()
        real_begin = durable._begin_fenced_send
        tokens = []
        token_lock = threading.Lock()

        def paused_begin(operation_id, claim_token, project_id, revision, operations):
            with token_lock:
                tokens.append(claim_token)
                first = len(tokens) == 1
            (first_ready if first else second_ready).set()
            if not (release_first if first else release_second).wait(20):
                raise TimeoutError('test did not release claimant')
            return real_begin(operation_id, claim_token, project_id, revision, operations)

        def worker():
            close_old_connections()
            try:
                return deliver_task_created_v4(record.pk)
            finally:
                connection.close()

        with patch.object(durable, '_begin_fenced_send', side_effect=paused_begin), \
             patch.object(durable, 'dispatch_webhook', side_effect=OSError('broker unavailable')), \
             patch.object(durable.requests, 'post', return_value=MagicMock(status_code=204)) as post, \
             ThreadPoolExecutor(max_workers=2) as pool:
            old = pool.submit(worker)
            try:
                self.assertTrue(first_ready.wait(15))
                recover_unsubmitted_webhook(self.project.pk, record.pk, local_principal(self.org.created_by), reason='recover')
                record.refresh_from_db()
                self.assertEqual(record.state, StorageOperation.State.QUEUED)
                self.assertEqual(StorageOperationRecovery.objects.filter(operation=record).count(), 1)
                new = pool.submit(worker)
                self.assertTrue(second_ready.wait(15))
                self.assertNotEqual(tokens[0], tokens[1])
                release_first.set()
                with self.assertRaises(ValidationError):
                    old.result(15)
                post.assert_not_called()
                release_second.set()
                new.result(15)
            finally:
                release_first.set()
                release_second.set()
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.COMPLETED)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['headers']['X-Label-Studio-Delivery-ID'], str(record.pk))

    def test_recovery_rejects_legacy_submitted_other_project_and_non_manager(self):
        record = self.create_queued()
        principal = local_principal(self.org.created_by)
        for phase, token in ((None, None), (WEBHOOK_SUBMITTING, uuid.uuid4())):
            StorageOperation.objects.filter(pk=record.pk).update(state='running', effect_phase=phase, effect_claim_token=token)
            with self.assertRaises(ValidationError):
                recover_unsubmitted_webhook(self.project.pk, record.pk, principal, reason='recover')
        StorageOperation.objects.filter(pk=record.pk).update(effect_phase=WEBHOOK_PRE_SEND, effect_claim_token=uuid.uuid4())
        other = ProjectFactory(organization=self.org)
        with self.assertRaises(ValidationError):
            recover_unsubmitted_webhook(other.pk, record.pk, principal, reason='recover')
        annotator = UserFactory(active_organization=self.org)
        self.org.add_user(annotator)
        ProjectMember.objects.create(project=self.project, user=annotator, role='annotator')
        with self.assertRaises(PermissionDenied):
            recover_unsubmitted_webhook(self.project.pk, record.pk, local_principal(annotator), reason='recover')
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.RUNNING)
        self.assertFalse(StorageOperationRecovery.objects.filter(operation=record).exists())

    def test_recovery_refuses_a_webhook_already_in_network_send(self):
        record = self.create_queued()
        entered, release = threading.Event(), threading.Event()

        def post(*args, **kwargs):
            entered.set()
            if not release.wait(15):
                raise TimeoutError('test did not release request')
            return MagicMock(status_code=204)

        def worker():
            close_old_connections()
            try:
                deliver_task_created_v4(record.pk)
            finally:
                connection.close()

        with patch('webhooks.durable.requests.post', side_effect=post) as send, ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            try:
                self.assertTrue(entered.wait(15))
                record.refresh_from_db()
                self.assertEqual(record.effect_phase, WEBHOOK_SUBMITTING)
                with self.assertRaises(ValidationError):
                    recover_unsubmitted_webhook(self.project.pk, record.pk, local_principal(self.org.created_by), reason='recover')
                self.assertFalse(StorageOperationRecovery.objects.filter(operation=record).exists())
            finally:
                release.set()
            future.result(15)
        self.assertEqual(send.call_count, 1)

    @override_settings(
        ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED=True,
        ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=True,
        ANNOTATION_PROJECT_OPERATION_PROVIDER='access_control.project_operations.LifecycleProjectOperationProvider',
    )
    def test_public_recovery_checks_manager_project_attestation_and_stale_request(self):
        record = self.create_queued()
        StorageOperation.objects.filter(pk=record.pk).update(
            state='running', effect_phase=WEBHOOK_PRE_SEND, effect_claim_token=uuid.uuid4()
        )
        url = f'/api/projects/{self.project.pk}/storage-operations/{record.pk}/resolve/'
        body = {'action': 'recover_webhook_pre_send', 'reason': 'worker stopped', 'attested': False}
        inventory = self.client.get(f'/api/projects/{self.project.pk}/storage-operations/')
        self.assertEqual(inventory.status_code, 200, inventory.content)
        self.assertTrue(inventory.data['operations'][0]['can_recover_webhook_pre_send'])
        self.assertNotIn('effect_claim_token', inventory.data['operations'][0])
        self.assertEqual(self.client.post(url, {**body, 'attested': True}, format='json').status_code, 400)
        other = ProjectFactory(organization=self.org)
        wrong_url = f'/api/projects/{other.pk}/storage-operations/{record.pk}/resolve/'
        self.assertEqual(self.client.post(wrong_url, body, format='json').status_code, 400)
        annotator = UserFactory(active_organization=self.org)
        self.org.add_user(annotator)
        ProjectMember.objects.create(project=self.project, user=annotator, role='annotator')
        self.client.force_authenticate(annotator)
        self.assertIn(self.client.post(url, body, format='json').status_code, (403, 404))
        self.client.force_authenticate(self.org.created_by)
        with patch('webhooks.durable.dispatch_webhook', side_effect=OSError('broker unavailable')):
            self.assertEqual(self.client.post(url, body, format='json').status_code, 204)
        self.assertEqual(self.client.post(url, body, format='json').status_code, 400)
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.QUEUED)
        self.assertEqual(StorageOperationRecovery.objects.filter(operation=record).count(), 1)

    def test_killed_before_send_can_be_recovered_without_duplicate_delivery(self):
        record = self.create_queued()
        code = """
import os, time, django
from unittest.mock import patch
django.setup()
from webhooks.durable import deliver_task_created_v4
def pause(*args):
    open(os.environ['ISSUE86_MARKER'], 'w').write('pre-send')
    time.sleep(300)
with patch('webhooks.durable._begin_fenced_send', pause):
    deliver_task_created_v4(os.environ['ISSUE86_OPERATION'])
"""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'started'
            environment = os.environ.copy()
            environment.update(
                POSTGRE_NAME=connection.settings_dict['NAME'], SECRET_KEY=settings.SECRET_KEY,
                ANNOTATION_STORAGE_ADMISSION_ENABLED='true', ISSUE86_MARKER=str(marker),
                ISSUE86_OPERATION=str(record.pk), DJANGO_SETTINGS_MODULE='core.settings.label_studio',
                LATEST_VERSION_CHECK='false',
            )
            process = subprocess.Popen(
                [sys.executable, '-c', code], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            try:
                deadline = time.monotonic() + 45
                while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(0.05)
                if not marker.exists():
                    if process.poll() is None:
                        process.kill()
                    _, error = process.communicate(timeout=10)
                    self.fail(f'worker did not reach pre-send: {error.decode(errors="replace")[-2000:]}')
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
        record.refresh_from_db()
        self.assertEqual((record.state, record.effect_phase), (StorageOperation.State.RUNNING, WEBHOOK_PRE_SEND))
        with patch('webhooks.durable.dispatch_webhook', side_effect=OSError('broker unavailable')):
            recover_unsubmitted_webhook(self.project.pk, record.pk, local_principal(self.org.created_by), reason='worker died')
        StorageOperation.objects.filter(pk=record.pk).update(started_at=timezone.now() - timedelta(minutes=2))
        with patch('webhooks.durable.start_job_async_or_sync', return_value='queued') as enqueue:
            call_command('replay_storage_outbox', project_id=self.project.pk)
        self.assertEqual(enqueue.call_args.args[0], deliver_task_created_v4)
        with patch('webhooks.durable.requests.post', return_value=MagicMock(status_code=204)) as post:
            deliver_task_created_v4(record.pk)
        self.assertEqual(post.call_count, 1)
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.COMPLETED)

    def test_recovered_delivery_can_be_claimed_by_real_old_webhook_worker(self):
        old_dir = os.environ.get('ISSUE86_OLD_DURABLE_WORKER_DIR')
        redis_url = os.environ.get('ISSUE86_TEST_REDIS_URL')
        if not old_dir or not redis_url:
            self.skipTest('requires isolated Redis and the pinned previous webhook worker')
        import redis
        from rq import Queue
        from webhooks.durable import _begin_fenced_send

        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.headers.get('X-Label-Studio-Delivery-ID'))
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.hook.url = f'http://127.0.0.1:{server.server_port}/hook'
        self.hook.save(update_fields=['url'])
        record = self.create_queued()
        old_token = uuid.uuid4()
        StorageOperation.objects.filter(pk=record.pk).update(
            state='running', effect_phase=WEBHOOK_PRE_SEND, effect_claim_token=old_token
        )
        with patch('webhooks.durable.dispatch_webhook', side_effect=OSError('broker unavailable')):
            recover_unsubmitted_webhook(self.project.pk, record.pk, local_principal(self.org.created_by), reason='requeue')
        connection_redis = redis.Redis.from_url(redis_url)
        queue = Queue('issue86-old-v2-' + uuid.uuid4().hex, connection=connection_redis)
        job = queue.enqueue_call('webhooks.durable.deliver_task_created_v2', args=(str(record.pk),))
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
            ANNOTATION_STORAGE_ADMISSION_ENABLED='true', ISSUE86_TEST_QUEUE=queue.name,
            ISSUE86_TEST_REDIS_URL=redis_url, DJANGO_SETTINGS_MODULE='core.settings.label_studio',
            PYTHONPATH=os.pathsep.join((str(Path(old_dir).parent), old_dir)), LATEST_VERSION_CHECK='false',
        )
        result = subprocess.run([sys.executable, '-c', code], cwd=old_dir, env=environment,
                                capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        job.refresh()
        self.assertTrue(job.is_finished, job.exc_info)
        self.assertEqual(received, [str(record.pk)])
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.COMPLETED)
        with self.assertRaises(ValidationError):
            _begin_fenced_send(record.pk, old_token, record.project_id, record.lifecycle_revision, {'webhook_task_created'})

    def test_changed_destination_blocks_before_network(self):
        record = self.create_queued()
        self.hook.url = 'https://changed.invalid/hook'
        self.hook.save(update_fields=['url'])
        with patch('webhooks.durable.requests.post') as send:
            with self.assertRaises(ValidationError):
                deliver_task_created_v2(record.pk)
        send.assert_not_called()
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.UNCERTAIN)

    def test_changed_payload_blocks_before_network(self):
        record = self.create_queued()
        StorageOperation.objects.filter(pk=record.pk).update(effect_payload={'action': 'TASKS_CREATED', 'tasks': []})
        with patch('webhooks.durable.requests.post') as send:
            with self.assertRaises(ValidationError):
                deliver_task_created_v2(record.pk)
        send.assert_not_called()

    def test_annotation_events_are_snapshotted_in_source_transactions(self):
        task = TaskFactory(project=self.project)
        assignment = TaskAssignment.objects.create(
            task=task,
            project=self.project,
            assignee=self.org.created_by,
            assigned_by=self.org.created_by,
        )
        token = {'assignment_id': assignment.pk, 'assignment_version': assignment.version}
        with patch('webhooks.durable.dispatch_project_event', side_effect=OSError('broker unavailable')), patch(
            'webhooks.utils.emit_webhooks_for_instance'
        ) as legacy:
            created = self.client.post(f'/api/tasks/{task.pk}/annotations/', {**token, 'result': []}, format='json')
            self.assertEqual(created.status_code, 201, created.content)
            annotation_id = created.data['id']
            updated = self.client.patch(f'/api/annotations/{annotation_id}/', {**token, 'result': []}, format='json')
            self.assertEqual(updated.status_code, 200, updated.content)
            deleted = self.client.delete(f'/api/annotations/{annotation_id}/', token, format='json')
            self.assertEqual(deleted.status_code, 204, deleted.content)
        legacy.assert_not_called()
        records = list(StorageOperation.objects.filter(project=self.project).order_by('started_at', 'pk'))
        self.assertEqual(
            [record.operation for record in records],
            [
                'webhook_annotation_created',
                'webhook_annotation_updated',
                'webhook_annotations_deleted',
            ],
        )
        self.assertEqual(records[0].effect_payload['annotation']['id'], annotation_id)
        self.assertEqual(records[0].effect_payload['task']['id'], task.pk)
        self.assertEqual(records[2].effect_payload['annotations'], [{'id': annotation_id}])
        self.assertFalse(Annotation.objects.filter(pk=annotation_id).exists())
        with patch('webhooks.durable.requests.post', return_value=MagicMock(status_code=204)) as send:
            deliver_project_event_v3(records[0].pk)
            with self.assertRaises(ValidationError):
                deliver_project_event_v3(records[0].pk)
        send.assert_called_once()

    def test_task_deletion_persists_tombstone_and_rolls_back_together(self):
        task = TaskFactory(project=self.project)
        with patch('webhooks.durable.dispatch_project_event') as dispatch:
            with self.assertRaisesRegex(RuntimeError, 'rollback'):
                with transaction.atomic():
                    response = self.client.delete(f'/api/tasks/{task.pk}/')
                    self.assertEqual(response.status_code, 204, response.content)
                    record = StorageOperation.objects.get(operation='webhook_tasks_deleted')
                    self.assertEqual(record.effect_payload['tasks'], [{'id': task.pk}])
                    raise RuntimeError('rollback')
            dispatch.assert_not_called()
        self.assertTrue(Task.objects.filter(pk=task.pk).exists())
        self.assertFalse(StorageOperation.objects.filter(operation='webhook_tasks_deleted').exists())

    def test_worker_generations_do_not_claim_each_others_records(self):
        old_record = self.create_queued()
        with patch('webhooks.durable.dispatch_project_event', side_effect=OSError('broker unavailable')):
            with transaction.atomic():
                queue_project_event(self.project, WebhookAction.TASKS_DELETED, [{'id': 123}])
        new_record = StorageOperation.objects.get(project=self.project, operation='webhook_tasks_deleted')
        with patch('webhooks.durable.requests.post') as send:
            with self.assertRaises(ValidationError):
                deliver_project_event_v3(old_record.pk)
            with self.assertRaises(ValidationError):
                deliver_task_created_v2(new_record.pk)
        send.assert_not_called()
        old_record.refresh_from_db()
        new_record.refresh_from_db()
        self.assertEqual((old_record.state, new_record.state), ('queued', 'queued'))
        with patch('webhooks.durable.start_job_async_or_sync', return_value='queued') as enqueue:
            self.assertEqual(redispatch_queued_operation(new_record.pk), 'queued')
        self.assertEqual(enqueue.call_args.args[0], deliver_project_event_v4)
        self.assertEqual(enqueue.call_args.args[1], str(new_record.pk))

    def test_project_update_is_snapshotted_without_legacy_dispatch(self):
        with patch('webhooks.durable.dispatch_project_event', side_effect=OSError('broker unavailable')), patch(
            'webhooks.utils.emit_webhooks_for_instance'
        ) as legacy:
            response = self.client.patch(f'/api/projects/{self.project.pk}', {'description': 'updated'}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        legacy.assert_not_called()
        record = StorageOperation.objects.get(project=self.project, operation='webhook_project_updated')
        self.assertEqual(record.effect_payload['action'], 'PROJECT_UPDATED')
        self.assertEqual(record.effect_payload['project']['description'], 'updated')

    def test_label_link_events_are_atomic_and_replayable(self):
        label = Label.objects.create(
            organization=self.org,
            created_by=self.org.created_by,
            title='synthetic',
            value='synthetic',
        )
        link = LabelLink.objects.create(project=self.project, label=label, from_name='before')
        url = f'/api/label_links/{link.pk}/'
        other_project = ProjectFactory(organization=self.org, title='test-active')
        with patch('webhooks.durable.dispatch_project_event', side_effect=OSError('broker unavailable')), patch(
            'webhooks.utils.emit_webhooks_for_instance'
        ) as legacy:
            moved = self.client.patch(url, {'project': other_project.pk}, format='json')
            self.assertEqual(moved.status_code, 400, moved.content)
            with self.assertRaisesRegex(RuntimeError, 'rollback'):
                with transaction.atomic():
                    changed = self.client.patch(url, {'from_name': 'rolled back'}, format='json')
                    self.assertEqual(changed.status_code, 200, changed.content)
                    raise RuntimeError('rollback')
            link.refresh_from_db()
            self.assertEqual(link.from_name, 'before')
            self.assertFalse(StorageOperation.objects.filter(operation='webhook_label_link_updated').exists())
            with self.assertRaisesRegex(RuntimeError, 'delete rollback'):
                with transaction.atomic():
                    deleted = self.client.delete(url)
                    self.assertEqual(deleted.status_code, 204, deleted.content)
                    self.assertTrue(StorageOperation.objects.filter(operation='webhook_label_link_deleted').exists())
                    raise RuntimeError('delete rollback')
            self.assertTrue(LabelLink.objects.filter(pk=link.pk).exists())
            self.assertFalse(StorageOperation.objects.filter(operation='webhook_label_link_deleted').exists())
            changed = self.client.patch(url, {'from_name': 'after'}, format='json')
            self.assertEqual(changed.status_code, 200, changed.content)
            deleted = self.client.delete(url)
            self.assertEqual(deleted.status_code, 204, deleted.content)
        legacy.assert_not_called()
        updated = StorageOperation.objects.get(operation='webhook_label_link_updated')
        removed = StorageOperation.objects.get(operation='webhook_label_link_deleted')
        self.assertEqual(updated.effect_payload['label_link']['from_name'], 'after')
        self.assertEqual(updated.effect_payload['label']['id'], label.pk)
        self.assertEqual(removed.effect_payload['label_link'], {'id': link.pk})
        self.assertFalse(LabelLink.objects.filter(pk=link.pk).exists())
        with patch('webhooks.durable.start_job_async_or_sync', return_value='queued') as enqueue:
            redispatch_queued_operation(removed.pk)
        self.assertEqual(enqueue.call_args.args[0], deliver_project_event_v4)
        # The previous v3 event registry cannot claim a newly enrolled action.
        from webhooks.durable import PROJECT_EVENTS

        older_registry = {key: value for key, value in PROJECT_EVENTS.items() if not key.startswith('LABEL_LINK_')}
        with patch('webhooks.durable.PROJECT_EVENTS', older_registry), patch('webhooks.durable.requests.post') as send:
            with self.assertRaises(ValidationError):
                deliver_project_event_v3(removed.pk)
            send.assert_not_called()
        removed.refresh_from_db()
        self.assertEqual(removed.state, StorageOperation.State.QUEUED)
        with patch('webhooks.durable.requests.post', return_value=MagicMock(status_code=204)) as send:
            deliver_project_event_v3(removed.pk)
            send.assert_called_once()

    def test_label_creation_event_is_atomic_and_replayable(self):
        payload = [{'project': self.project.pk, 'title': 'new label', 'value': 'new label', 'from_name': 'choices'}]
        with patch('webhooks.durable.dispatch_project_event', side_effect=OSError('broker unavailable')), patch(
            'webhooks.utils.emit_webhooks_for_instance'
        ) as legacy:
            with self.assertRaisesRegex(RuntimeError, 'rollback'):
                with transaction.atomic():
                    response = self.client.post('/api/labels/', payload, format='json')
                    self.assertEqual(response.status_code, 201, response.content)
                    self.assertTrue(StorageOperation.objects.filter(operation='webhook_label_link_created').exists())
                    raise RuntimeError('rollback')
            self.assertFalse(Label.objects.filter(organization=self.org, title='new label').exists())
            self.assertFalse(StorageOperation.objects.filter(operation='webhook_label_link_created').exists())
            response = self.client.post('/api/labels/', payload, format='json')
            self.assertEqual(response.status_code, 201, response.content)
        legacy.assert_not_called()
        link = LabelLink.objects.get(project=self.project, label__title='new label')
        record = StorageOperation.objects.get(operation='webhook_label_link_created')
        self.assertEqual(record.effect_payload['label_link'][0]['id'], link.pk)
        StorageOperation.objects.filter(pk=record.pk).update(started_at=timezone.now() - timedelta(minutes=2))
        with patch('webhooks.durable.start_job_async_or_sync', return_value='queued') as enqueue:
            call_command('replay_storage_outbox', project_id=self.project.pk)
        self.assertEqual(enqueue.call_args.args[0], deliver_project_event_v4)
        record.refresh_from_db()
        self.assertEqual(record.dispatch_attempts, 1)
        with patch('webhooks.durable.requests.post', return_value=MagicMock(status_code=204)) as send:
            deliver_project_event_v3(record.pk)
        send.assert_called_once()

    def test_label_creation_rejects_cross_project_annotator_and_paused(self):
        other = ProjectFactory(organization=self.org, title='test-active')

        def item(project, title):
            return {'project': project.pk, 'title': title, 'value': title, 'from_name': 'choices'}

        response = self.client.post('/api/labels/', [item(self.project, 'one'), item(other, 'two')], format='json')
        self.assertEqual(response.status_code, 400, response.content)
        annotator = UserFactory(active_organization=self.org)
        self.org.add_user(annotator)
        ProjectMember.objects.create(project=self.project, user=annotator, role='annotator')
        self.client.force_authenticate(annotator)
        response = self.client.post('/api/labels/', [item(self.project, 'denied')], format='json')
        self.assertIn(response.status_code, (403, 404), response.content)
        self.client.force_authenticate(self.org.created_by)
        self.project.title = 'test-paused'
        self.project.save(update_fields=['title'])
        response = self.client.post('/api/labels/', [item(self.project, 'paused')], format='json')
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(Label.objects.filter(organization=self.org).exists())
        self.assertFalse(StorageOperation.objects.filter(operation='webhook_label_link_created').exists())

    def test_label_reuse_is_scoped_to_the_locked_project_organization(self):
        other = OrganizationFactory()
        foreign = Label.objects.create(
            organization=other,
            created_by=other.created_by,
            title='shared title',
            value='foreign value',
        )
        serializer = LabelCreateSerializer(
            data=[
                {'project': self.project.pk, 'title': 'shared title', 'value': 'local value', 'from_name': 'choices'}
            ],
            many=True,
            context={'request': SimpleNamespace(user=other.created_by)},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        with patch('webhooks.durable.dispatch_project_event', side_effect=OSError('broker unavailable')):
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                serializer.save(created_by=self.org.created_by, organization=self.org)
        link = LabelLink.objects.get(project=self.project)
        self.assertNotEqual(link.label_id, foreign.pk)
        self.assertEqual(link.label.organization_id, self.org.pk)
        self.assertEqual(link.label.value, 'local value')

    def test_label_created_real_old_worker_rejected_then_new_worker_delivered(self):
        old_dir = os.environ.get('ISSUE86_OLD_WORKER_DIR')
        redis_url = os.environ.get('ISSUE86_TEST_REDIS_URL')
        if not old_dir or not redis_url:
            self.skipTest('requires isolated Redis and an old checkout')
        import redis
        from rq import Queue

        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(
                    (
                        self.headers.get('X-Label-Studio-Delivery-ID'),
                        self.rfile.read(int(self.headers['Content-Length'])),
                    )
                )
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.hook.url = f'http://127.0.0.1:{server.server_port}/hook'
        self.hook.save(update_fields=['url'])
        payload = [{'project': self.project.pk, 'title': 'mixed', 'value': 'mixed', 'from_name': 'choices'}]
        with patch('webhooks.durable.dispatch_project_event', side_effect=OSError('broker unavailable')):
            response = self.client.post('/api/labels/', payload, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        record = StorageOperation.objects.get(operation='webhook_label_link_created')
        broker = redis.Redis.from_url(redis_url)
        script = """
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings.label_studio')
django.setup()
from redis import Redis
from rq import Queue, SimpleWorker
broker = Redis.from_url(os.environ['ISSUE86_TEST_REDIS_URL'])
queue = Queue(os.environ['ISSUE86_TEST_QUEUE'], connection=broker)
SimpleWorker([queue], connection=broker).work(burst=True)
"""
        base_env = os.environ.copy()
        base_env['POSTGRE_NAME'] = connection.settings_dict['NAME']
        base_env['ANNOTATION_STORAGE_ADMISSION_ENABLED'] = 'true'
        base_env['DJANGO_SETTINGS_MODULE'] = 'core.settings.label_studio'

        def run_worker(directory, suffix):
            queue_name = f'issue86-{record.pk}-{suffix}'
            queue = Queue(queue_name, connection=broker)
            job = queue.enqueue_call('webhooks.durable.deliver_project_event_v4', args=(str(record.pk),))
            env = {
                **base_env,
                'ISSUE86_TEST_QUEUE': queue_name,
                'PYTHONPATH': os.pathsep.join((str(Path(directory).parent), directory)),
            }
            result = subprocess.run(
                [sys.executable, '-c', script], cwd=directory, env=env, capture_output=True, text=True, timeout=90
            )
            self.assertEqual(result.returncode, 0, result.stderr[-3000:])
            job.refresh()
            return job

        old_job = run_worker(old_dir, 'old')
        self.assertTrue(old_job.is_failed)
        self.assertIn("module 'webhooks' has no attribute 'durable'", old_job.exc_info)
        self.assertIn('deliver_project_event_v4', old_job.exc_info)
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.QUEUED)
        self.assertEqual(received, [])
        new_job = run_worker(str(Path(__file__).resolve().parents[2]), 'new')
        self.assertTrue(new_job.is_finished, new_job.exc_info)
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.COMPLETED)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], str(record.pk))
        self.assertIn(b'LABEL_LINK_CREATED', received[0][1])

    def test_label_creation_waits_for_pause_and_does_not_emit(self):
        pids = []
        payload = [{'project': self.project.pk, 'title': 'race', 'value': 'race', 'from_name': 'choices'}]

        def create():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pids.append(cursor.fetchone()[0])
                client = APIClient()
                client.force_authenticate(self.org.created_by)
                return client.post('/api/labels/', payload, format='json')
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                project = Project.objects.select_for_update().get(pk=self.project.pk)
                project.title = 'test-paused'
                project.save(update_fields=['title'])
                future = pool.submit(create)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if pids:
                        with connection.cursor() as cursor:
                            cursor.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s', [pids[0]])
                            row = cursor.fetchone()
                        if row and row[0] == 'Lock':
                            break
                    time.sleep(0.02)
                else:
                    self.fail('label creation did not wait for project transition')
            response = future.result(30)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(Label.objects.filter(organization=self.org, title='race').exists())
        self.assertFalse(StorageOperation.objects.filter(operation='webhook_label_link_created').exists())

    def test_label_link_rejects_paused_project_and_other_organization(self):
        label = Label.objects.create(
            organization=self.org,
            created_by=self.org.created_by,
            title='synthetic',
            value='synthetic',
        )
        link = LabelLink.objects.create(project=self.project, label=label, from_name='before')
        url = f'/api/label_links/{link.pk}/'
        self.project.title = 'test-paused'
        self.project.save(update_fields=['title'])
        self.assertEqual(self.client.patch(url, {'from_name': 'denied'}, format='json').status_code, 403)
        self.assertEqual(self.client.delete(url).status_code, 403)
        self.project.title = 'test-active'
        self.project.save(update_fields=['title'])
        annotator = UserFactory(active_organization=self.org)
        self.org.add_user(annotator)
        ProjectMember.objects.create(project=self.project, user=annotator, role='annotator')
        self.client.force_authenticate(annotator)
        self.assertIn(self.client.patch(url, {'from_name': 'denied'}, format='json').status_code, (403, 404))
        self.assertIn(self.client.delete(url).status_code, (403, 404))
        other = OrganizationFactory()
        self.client.force_authenticate(other.created_by)
        self.assertEqual(self.client.patch(url, {'from_name': 'denied'}, format='json').status_code, 404)
        self.assertFalse(StorageOperation.objects.filter(operation__startswith='webhook_label_link_').exists())
        link.refresh_from_db()
        self.assertEqual(link.from_name, 'before')

    def test_label_link_update_waits_for_pause_and_does_not_emit(self):
        label = Label.objects.create(
            organization=self.org,
            created_by=self.org.created_by,
            title='synthetic',
            value='synthetic',
        )
        link = LabelLink.objects.create(project=self.project, label=label, from_name='before')
        pids = []

        def change():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pids.append(cursor.fetchone()[0])
                client = APIClient()
                client.force_authenticate(self.org.created_by)
                return client.patch(f'/api/label_links/{link.pk}/', {'from_name': 'after'}, format='json')
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                project = Project.objects.select_for_update().get(pk=self.project.pk)
                project.title = 'test-paused'
                project.save(update_fields=['title'])
                future = pool.submit(change)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if pids:
                        with connection.cursor() as cursor:
                            cursor.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s', [pids[0]])
                            row = cursor.fetchone()
                        if row and row[0] == 'Lock':
                            break
                    time.sleep(0.02)
                else:
                    self.fail('label link update did not wait for project transition')
            response = future.result(30)
        self.assertEqual(response.status_code, 403, response.content)
        link.refresh_from_db()
        self.assertEqual(link.from_name, 'before')
        self.assertFalse(StorageOperation.objects.filter(operation__startswith='webhook_label_link_').exists())

    def test_project_creation_persists_organization_webhook(self):
        Webhook.objects.create(
            organization=self.org,
            project=None,
            url='https://organization.invalid/hook',
            headers={},
            is_active=True,
            send_payload=True,
            send_for_all_actions=True,
        )
        with patch('webhooks.durable.dispatch_project_event', side_effect=OSError('broker unavailable')), patch(
            'webhooks.utils.emit_webhooks_for_instance'
        ) as legacy:
            response = self.client.post('/api/projects', {'title': 'synthetic created'}, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        legacy.assert_not_called()
        record = StorageOperation.objects.get(project_id=response.data['id'], operation='webhook_project_created')
        self.assertEqual(record.effect_payload['action'], 'PROJECT_CREATED')
        self.assertEqual(record.effect_payload['project']['id'], response.data['id'])

    def test_webhook_edit_waits_for_inflight_send(self):
        record = self.create_queued()
        entered, release = threading.Event(), threading.Event()
        pid = []

        def post(*args, **kwargs):
            entered.set()
            if not release.wait(15):
                raise TimeoutError('test did not release send')
            return MagicMock(status_code=204)

        def deliver():
            close_old_connections()
            try:
                deliver_task_created_v2(record.pk)
            finally:
                connection.close()

        def change():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pid.append(cursor.fetchone()[0])
                client = APIClient()
                client.force_authenticate(self.org.created_by)
                return client.patch(
                    f'/api/webhooks/{self.hook.pk}/', {'url': 'https://changed.invalid/hook'}, format='json'
                )
            finally:
                connection.close()

        with patch('webhooks.durable.requests.post', side_effect=post), ThreadPoolExecutor(max_workers=2) as pool:
            worker = pool.submit(deliver)
            try:
                self.assertTrue(entered.wait(15))
                update = pool.submit(change)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if pid:
                        with connection.cursor() as cursor:
                            cursor.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s', [pid[0]])
                            row = cursor.fetchone()
                        if row and row[0] == 'Lock':
                            break
                    time.sleep(0.02)
                else:
                    self.fail('webhook edit did not wait on the delivery')
            finally:
                release.set()
            worker.result(30)
            response = update.result(30)
        self.assertEqual(response.status_code, 200, response.content)

    def test_killed_sender_keeps_running_receipt_and_blocks_drain(self):
        record = self.create_queued()
        code = """
import os, time, django
from unittest.mock import patch
django.setup()
from webhooks.durable import deliver_task_created_v2
def post(*args, **kwargs):
    open(os.environ['ISSUE86_MARKER'], 'w').write('request started')
    time.sleep(300)
with patch('webhooks.durable.requests.post', post):
    deliver_task_created_v2(os.environ['ISSUE86_OPERATION'])
"""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'started'
            environment = os.environ.copy()
            environment.update(
                POSTGRE_NAME=connection.settings_dict['NAME'],
                SECRET_KEY=settings.SECRET_KEY,
                ANNOTATION_STORAGE_ADMISSION_ENABLED='true',
                ISSUE86_MARKER=str(marker),
                ISSUE86_OPERATION=str(record.pk),
                DJANGO_SETTINGS_MODULE='core.settings.label_studio',
                LATEST_VERSION_CHECK='false',
            )
            process = subprocess.Popen(
                [sys.executable, '-c', code], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            try:
                deadline = time.monotonic() + 45
                while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(0.05)
                if not marker.exists():
                    if process.poll() is None:
                        process.kill()
                    _, error = process.communicate(timeout=10)
                    self.fail(f'worker did not reach send: {error.decode(errors="replace")[-2000:]}')
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
        record.refresh_from_db()
        self.assertEqual(record.state, StorageOperation.State.RUNNING)
        with transaction.atomic(), self.assertRaises(ValidationError):
            lock_drained_project(self.project.pk)
