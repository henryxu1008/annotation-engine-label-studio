"""Dormant bounded replay for version-pinned S3 deletion intents."""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone
from io_storages.admission_models import StorageOperation
from io_storages.s3 import version_delete


class Command(BaseCommand):
    help = 'Replay due version-pinned S3 deletions only after verified cloud isolation.'

    def add_arguments(self, parser):
        parser.add_argument('--project-id', type=int)
        parser.add_argument('--limit', type=int, default=50)

    def handle(self, *args, **options):
        if not 1 <= options['limit'] <= 100 or (options['project_id'] is not None and options['project_id'] < 1):
            raise CommandError('A positive project and a limit between 1 and 100 are required.')
        if not getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
            raise CommandError('Storage admission is disabled.')
        try:
            # Always fails until #144 supplies a reviewed proof; importantly,
            # no queue scan, reservation, or provider request precedes it.
            version_delete._require_verified_cloud_isolation()
        except Exception:
            raise CommandError('Verified cloud isolation and request drain are unavailable.') from None
        pending = StorageOperation.objects.filter(
            operation='annotation_delete_pending', storage_model='io_storages.s3exportstorage',
            state=StorageOperation.State.QUEUED, effect_phase__isnull=True, effect_claim_token__isnull=True,
            effect_payload__isnull=True,
        ).filter(Q(next_dispatch_at__isnull=True) | Q(next_dispatch_at__lte=timezone.now()))
        if options['project_id'] is not None:
            pending = pending.filter(project_id=options['project_id'])
        ids = list(pending.order_by('started_at', 'pk').values_list('pk', flat=True)[:options['limit']])
        failures = 0
        for operation_id in ids:
            try:
                version_delete.dispatch_versioned_delete(operation_id)
            except Exception as exc:
                failures += 1
                self.stderr.write(f'{operation_id}: {type(exc).__name__}')
                # A malformed or temporarily blocked intent must not occupy
                # every bounded replay page indefinitely. Never overwrite a
                # concurrent reservation or a claim, and do not touch outcome.
                StorageOperation.objects.filter(
                    pk=operation_id, operation='annotation_delete_pending',
                    storage_model='io_storages.s3exportstorage', state=StorageOperation.State.QUEUED,
                    effect_phase__isnull=True, effect_claim_token__isnull=True, effect_payload__isnull=True,
                ).filter(Q(next_dispatch_at__isnull=True) | Q(next_dispatch_at__lte=timezone.now())).update(
                    next_dispatch_at=timezone.now() + timedelta(minutes=5)
                )
        self.stdout.write(f'Attempted {len(ids)}; failed {failures}.')
        if failures:
            raise CommandError('Some deletion intents remain unresolved; no outcome was assumed.')
