"""Bounded, restart-safe scans for optional package automation."""

import hashlib
import json
from io import StringIO
from uuid import NAMESPACE_URL, uuid5

from access_control.project_operations import local_principal
from django.conf import settings
from django.core.management import call_command
from django.db.models import Exists, OuterRef, Q
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from tasks.models import TaskAssignment

from .dispatch import DispatchConflict
from .models import TaskPackagePool
from .pools import balanced_dispatch, local_automation_identity, postgres_required


def scan_allocations(cursor=None, limit=100):
    """Scan by descending priority and ascending ID; callers retain the returned cursor."""
    postgres_required()
    if not local_automation_identity() or not all(
        (
            settings.ANNOTATION_PACKAGE_AUTOMATION_ENABLED,
            settings.ANNOTATION_FIXED_PACKAGES_ENABLED,
            settings.ANNOTATION_PACKAGE_CLAIM_ENABLED,
            settings.ANNOTATION_PACKAGE_ADVANCED_ENABLED,
            settings.ANNOTATION_PACKAGE_DISPATCH_ENABLED,
        )
    ):
        return {'scanned': 0, 'allocated': 0, 'skipped': 0, 'next': None}
    occupied = TaskAssignment.objects.filter(
        task__package_items__package_id=OuterRef('package_id'), status__in=TaskAssignment.ACTIVE_STATUSES
    )
    pools = (
        TaskPackagePool.objects.filter(
            automatic=True, enabled=True, automation_actor__isnull=False, claim__isnull=True
        )
        .filter(Q(due_at__isnull=True) | Q(due_at__gt=timezone.now()))
        .annotate(occupied=Exists(occupied))
        .filter(occupied=False)
        .select_related('automation_actor', 'package')
        .order_by('-priority', 'pk')
    )
    if cursor:
        priority, pk = cursor
        pools = pools.filter(Q(priority__lt=priority) | Q(priority=priority, pk__gt=pk))
    rows = list(pools[: limit + 1])
    result = {'scanned': 0, 'allocated': 0, 'skipped': 0, 'next': None}
    for pool in rows[:limit]:
        result['scanned'] += 1
        actor = pool.automation_actor
        principal = local_principal(actor)
        key = uuid5(
            NAMESPACE_URL,
            f'label-studio:package-auto:{pool.package.project_id}:{pool.pk}:{pool.automation_generation}',
        )
        request = {'package_id': pool.package_id, 'mode': 'automatic', 'generation': pool.automation_generation}
        digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        try:
            _, replayed = balanced_dispatch(
                project_id=pool.package.project_id,
                package_id=pool.package_id,
                principal=principal,
                key=key,
                request_hash=digest,
                automation_revision=pool.revision,
            )
            result['allocated'] += int(not replayed)
        except (DispatchConflict, PermissionDenied, ValidationError, Http404):
            result['skipped'] += 1
    if len(rows) > limit:
        result['next'] = (rows[limit - 1].priority, rows[limit - 1].pk)
    return result


def scan_notices(after=0, limit=100):
    output = StringIO()
    call_command('reconcile_package_overdue', after=after, limit=limit, stdout=output)
    value = output.getvalue().strip()
    if not value.startswith('next_after='):
        raise RuntimeError('Unexpected overdue reconciliation cursor')
    next_value = value.removeprefix('next_after=')
    if next_value and (not next_value.isdigit() or int(next_value) <= after):
        raise RuntimeError('Overdue reconciliation cursor did not advance')
    return int(next_value) if next_value else None
