from datetime import timedelta
from io import StringIO
from threading import Event, Thread
from uuid import uuid4

import pytest
from classification_disagreements import bbox_attempts, bbox_storage
from classification_disagreements.models import BboxMediaAttempt, BboxMediaPin
from django.core.management import call_command
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.urls import get_resolver
from django.utils import timezone
from projects.tests.factories import ProjectFactory
from tasks.tests.factories import TaskFactory


@pytest.fixture
def storage(settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = str(tmp_path / 'public')
    settings.STATIC_ROOT = str(tmp_path / 'static')
    settings.APPROVED_EXPORT_ROOT = str(tmp_path / 'private')
    audit_root = tmp_path / 'private' / 'audit-packages'
    audit_root.mkdir(parents=True)
    monkeypatch.setattr(bbox_storage.audit_files, 'private_root', lambda: audit_root)
    return bbox_storage.reserve_namespace()


@pytest.mark.django_db
def test_expired_attempt_cleans_and_delayed_writer_cannot_publish(storage):
    project = ProjectFactory()
    task = TaskFactory(project=project)
    token = uuid4()
    attempt = BboxMediaAttempt.objects.create(
        token=token, task=task, project=project, task_id_frozen=task.pk, project_id_frozen=project.pk,
        actor_id_frozen=project.created_by_id, storage_binding=storage, expires_at=timezone.now() - timedelta(seconds=1),
    )
    bbox_storage.publish(token, b'candidate', expected_binding=storage)
    assert bbox_attempts.reconcile(token) is True
    attempt.refresh_from_db()
    assert attempt.status == 'abandoned' and attempt.last_reconciled_at is not None
    bbox_storage.publish(token, b'late writer', expected_binding=storage)
    assert bbox_attempts.reconcile(token) is True
    with pytest.raises(IntegrityError), transaction.atomic():
        BboxMediaPin.objects.create(
            task=task, project=project, token=token, actor_id_frozen=project.created_by_id,
            field='image', source={}, task_data_hash='a' * 64, config_hash='b' * 64,
            artifact_key=str(token), storage_binding=storage, sha256='c' * 64,
            size_bytes=1, width=1, height=1,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        BboxMediaAttempt.objects.filter(pk=token).update(status='published')


@pytest.mark.django_db
def test_committed_pin_is_never_reconciled(storage):
    project = ProjectFactory()
    task = TaskFactory(project=project)
    token = uuid4()
    attempt = BboxMediaAttempt.objects.create(
        token=token, task=task, project=project, task_id_frozen=task.pk, project_id_frozen=project.pk,
        actor_id_frozen=project.created_by_id, storage_binding=storage, expires_at=timezone.now() + timedelta(minutes=5),
    )
    bbox_storage.publish(token, b'published', expected_binding=storage)
    pin = BboxMediaPin.objects.create(
        task=task, project=project, token=token, actor_id_frozen=project.created_by_id,
        field='image', source={}, task_data_hash='a' * 64, config_hash='b' * 64,
        artifact_key=str(token), storage_binding=storage, sha256='c' * 64,
        size_bytes=1, width=1, height=1,
    )
    assert bbox_attempts.reconcile(token, abandon_active=True) is False
    assert (bbox_storage.root() / str(token)).read_bytes() == b'published'
    assert BboxMediaPin.objects.filter(pk=pin.pk).exists()
    attempt.status = 'published'
    attempt.save(update_fields=['status'])
    task.delete()
    attempt.refresh_from_db()
    assert attempt.task_id is None and bbox_attempts.reconcile(token) is False
    assert (bbox_storage.root() / str(token)).read_bytes() == b'published'


@pytest.mark.django_db
def test_reconcile_command_processes_expired_attempt(storage):
    project = ProjectFactory()
    task = TaskFactory(project=project)
    token = uuid4()
    BboxMediaAttempt.objects.create(
        token=token, task=task, project=project, task_id_frozen=task.pk, project_id_frozen=project.pk,
        actor_id_frozen=project.created_by_id, storage_binding=storage, expires_at=timezone.now() - timedelta(seconds=1),
    )
    bbox_storage.publish(token, b'candidate', expected_binding=storage)
    output = StringIO()
    call_command('reconcile_bbox_media', limit=1, stdout=output)
    assert 'checked=1 removed=1 failed=0' in output.getvalue()
    assert not (bbox_storage.root() / str(token)).exists()


@pytest.mark.django_db
def test_reconcile_command_only_sweeps_its_storage_namespace(storage):
    project = ProjectFactory()
    other_task = TaskFactory(project=project)
    other = BboxMediaAttempt.objects.create(
        token=uuid4(), task=other_task, project=project, task_id_frozen=other_task.pk,
        project_id_frozen=project.pk, actor_id_frozen=project.created_by_id,
        storage_binding='another-namespace', expires_at=timezone.now() - timedelta(minutes=2),
    )
    task = TaskFactory(project=project)
    token = uuid4()
    BboxMediaAttempt.objects.create(
        token=token, task=task, project=project, task_id_frozen=task.pk, project_id_frozen=project.pk,
        actor_id_frozen=project.created_by_id, storage_binding=storage, expires_at=timezone.now() - timedelta(seconds=1),
    )
    bbox_storage.publish(token, b'candidate', expected_binding=storage)
    output = StringIO()
    call_command('reconcile_bbox_media', limit=1, stdout=output)
    other.refresh_from_db()
    assert other.status == 'active' and other.last_reconciled_at is None
    assert 'checked=1 removed=1 failed=0' in output.getvalue()


@pytest.mark.django_db(transaction=True)
def test_reconciler_waits_for_pin_transaction_and_keeps_committed_file(storage):
    if connection.vendor != 'postgresql':
        pytest.skip('PostgreSQL row-lock semantics')
    # The application lazily registers several models when loading routes;
    # initialize them before TransactionTestCase's PostgreSQL flush.
    get_resolver().url_patterns
    project = ProjectFactory()
    task = TaskFactory(project=project)
    token = uuid4()
    BboxMediaAttempt.objects.create(
        token=token, task=task, project=project, task_id_frozen=task.pk, project_id_frozen=project.pk,
        actor_id_frozen=project.created_by_id, storage_binding=storage, expires_at=timezone.now() + timedelta(minutes=5),
    )
    bbox_storage.publish(token, b'candidate', expected_binding=storage)
    inserted, release, finished = Event(), Event(), Event()
    errors, outcome = [], []

    def writer():
        close_old_connections()
        try:
            with transaction.atomic():
                BboxMediaPin.objects.create(
                    task_id=task.pk, project_id=project.pk, token=token, actor_id_frozen=project.created_by_id,
                    field='image', source={}, task_data_hash='a' * 64, config_hash='b' * 64,
                    artifact_key=str(token), storage_binding=storage, sha256='c' * 64,
                    size_bytes=1, width=1, height=1,
                )
                inserted.set()
                if not release.wait(10):
                    raise RuntimeError('writer timed out')
        except Exception as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    def cleaner():
        close_old_connections()
        try:
            outcome.append(bbox_attempts.reconcile(token, abandon_active=True))
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()
            close_old_connections()

    first = Thread(target=writer)
    first.start()
    assert inserted.wait(10)
    second = Thread(target=cleaner)
    second.start()
    assert not finished.wait(0.3)
    release.set()
    first.join(10)
    second.join(10)
    assert not first.is_alive() and not second.is_alive() and not errors
    assert outcome == [False] and (bbox_storage.root() / str(token)).read_bytes() == b'candidate'
