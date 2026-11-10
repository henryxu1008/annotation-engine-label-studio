"""Reconcile a bounded page of expired or abandoned bbox candidates."""

import logging

from classification_disagreements import bbox_attempts, bbox_storage
from classification_disagreements.models import BboxMediaAttempt
from django.core.management.base import BaseCommand, CommandError
from django.db.models import F, Q
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Safely reconcile expired or abandoned bbox media candidates.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=50)

    def handle(self, *args, **options):
        limit = options['limit']
        if not 1 <= limit <= 100:
            raise CommandError('limit must be between 1 and 100')
        # Never consume another mount's page or treat a missing marker as an
        # empty sweep. Each storage namespace runs its own reconciler.
        namespace = bbox_storage.binding(bbox_storage.root())
        eligible = Q(status='abandoned') | Q(status='active', expires_at__lte=timezone.now()) | Q(
            status='active', task__isnull=True
        )
        tokens = list(BboxMediaAttempt.objects.filter(eligible, storage_binding=namespace).order_by(
            F('last_reconciled_at').asc(nulls_first=True), 'created_at', 'token'
        ).values_list('token', flat=True)[:limit])
        removed = failed = 0
        for token in tokens:
            try:
                removed += bool(bbox_attempts.reconcile(token))
            except Exception:
                failed += 1
                logger.exception('Unable to reconcile a bbox media candidate')
        self.stdout.write(f'checked={len(tokens)} removed={removed} failed={failed}')
        if failed:
            raise CommandError('Some bbox candidates could not be reconciled.')
