"""Bounded replay; safe for concurrent timer invocations."""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from io_storages.admission_models import StorageOperation
from io_storages.dispatch_recovery import redispatch_queued_operation


class Command(BaseCommand):
    help = 'Redispatch a bounded set of queued storage operations.'

    def add_arguments(self, parser):
        parser.add_argument('--project-id', type=int)
        parser.add_argument('--limit', type=int, default=50)

    def handle(self, *args, **options):
        if not getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
            raise CommandError('Storage admission is disabled.')
        if not 1 <= options['limit'] <= 100:
            raise CommandError('Limit must be between 1 and 100.')
        if connection.vendor != 'postgresql':
            raise CommandError('Storage replay requires PostgreSQL row locks.')
        now = timezone.now()
        with transaction.atomic():
            pending = StorageOperation.objects.filter(
                state=StorageOperation.State.QUEUED,
                started_at__lte=now - timedelta(seconds=60),
                operation__in=[
                    'annotation_export_v3_queued',
                    'import_sync',
                    'export_sync',
                    'export_sync_new',
                    'webhook_task_created',
                    'webhook_annotation_created',
                    'webhook_annotation_updated',
                    'webhook_annotations_deleted',
                    'webhook_tasks_deleted',
                    'webhook_project_created',
                    'webhook_project_updated',
                    'webhook_label_link_created',
                    'webhook_label_link_updated',
                    'webhook_label_link_deleted',
                ],
            ).filter(Q(next_dispatch_at__isnull=True) | Q(next_dispatch_at__lte=now))
            if options['project_id'] is not None:
                pending = pending.filter(project_id=options['project_id'])
            entries = list(
                pending.order_by('started_at', 'pk').select_for_update(skip_locked=True)[: options['limit']]
            )
            for entry in entries:
                entry.next_dispatch_at = now + timedelta(minutes=5)
                entry.dispatch_attempts += 1
            StorageOperation.objects.bulk_update(entries, ['next_dispatch_at', 'dispatch_attempts'])
            reservations = [(entry.pk, entry.dispatch_attempts) for entry in entries]
        failures = 0
        for operation_id, attempt in reservations:
            try:
                redispatch_queued_operation(operation_id, expected_attempt=attempt)
            except Exception as exc:
                failures += 1
                self.stderr.write(f'{operation_id}: {type(exc).__name__}')
        self.stdout.write(f'Attempted {len(reservations)}; failed {failures}.')
        legacy = StorageOperation.objects.filter(operation='annotation_export_queued').exclude(
            state__in=(
                StorageOperation.State.COMPLETED,
                StorageOperation.State.RECONCILED,
                StorageOperation.State.CANCELLED,
            ),
        )
        if options['project_id'] is not None:
            legacy = legacy.filter(project_id=options['project_id'])
        if legacy.exists():
            self.stdout.write('Legacy annotation exports remain unresolved and were not redispatched.')
        if failures:
            raise CommandError('Some operations could not be dispatched; their records remain authoritative.')
