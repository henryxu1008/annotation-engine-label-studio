"""Uniform, durable scoring queue; no public endpoint is exposed."""

import logging
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import APIException

from .models import BlindQualityAssignment, BlindScoreOutbox

logger = logging.getLogger(__name__)


def record_submission(submission, project):
    """Record all project submissions alike; never synchronously score one."""
    projects = settings.ANNOTATION_CLASSIFICATION_BLIND_PROJECTS
    enabled = (settings.ANNOTATION_CLASSIFICATION_BLIND_ENABLED and isinstance(projects, dict)
               and projects.get(f'{project.organization_id}:{project.pk}') is True)
    if not enabled and not BlindQualityAssignment.objects.filter(project_id=project.pk).exists():
        return None
    outbox = BlindScoreOutbox.objects.create(submission=submission)
    transaction.on_commit(lambda: dispatch(outbox.pk))
    return outbox


def dispatch(outbox_id):
    """Best-effort enqueue; failure leaves the durable row pending."""
    try:
        from data_export.approved import api_queue

        with transaction.atomic():
            row = BlindScoreOutbox.objects.select_for_update().filter(pk=outbox_id).first()
            if row is None or row.state != 'pending':
                return
            api_queue().enqueue(process, row.pk, str(row.token), job_timeout=300, result_ttl=0)
            row.state, row.queued_at = 'queued', timezone.now()
            row.save(update_fields=['state', 'queued_at', 'updated_at'])
    except Exception as exc:
        logger.warning('Quality score enqueue remains pending: id=%s error=%s', outbox_id, type(exc).__name__)


def process(outbox_id, token):
    """Apply one fenced delivery; unexpected failures remain recoverable."""
    try:
        _process(outbox_id, token)
    except Exception as exc:
        logger.warning('Quality score worker needs retry: id=%s error=%s', outbox_id, type(exc).__name__)


@transaction.atomic
def _process(outbox_id, token):
    from projects.models import Project

    project_id = BlindScoreOutbox.objects.filter(pk=outbox_id).values_list(
        'submission__assignment__project_id', flat=True,
    ).first()
    if project_id is None:
        return
    project = Project.objects.select_for_update().filter(pk=project_id).first()
    if project is None:
        return
    row = BlindScoreOutbox.objects.select_for_update().select_related('submission__assignment').filter(pk=outbox_id).first()
    if row is None or row.state != 'queued' or str(row.token) != token:
        return
    assignment = row.submission.assignment
    if assignment.project_id != project.pk:
        row.state, row.error_code = 'invalid', 'SCOPE_CHANGED'
    elif not BlindQualityAssignment.objects.filter(task_id=assignment.task_id).exists():
        row.state, row.error_code = 'ignored', ''
    else:
        from .blind_scoring import score_quality_revision

        try:
            with transaction.atomic():
                score_quality_revision(row.submission_id)
        except (APIException, Http404):
            row.state, row.error_code = 'invalid', 'REVISION_UNAVAILABLE'
        else:
            row.state, row.error_code = 'scored', ''
    row.save(update_fields=['state', 'error_code', 'updated_at'])


def reconcile(*, limit=50, stale_seconds=600):
    """Retry a bounded page; stale deliveries are fenced by a new token."""
    if type(limit) is not int or not 1 <= limit <= 100 or type(stale_seconds) is not int or stale_seconds < 300:
        raise ValueError('Invalid reconciliation bounds.')
    cutoff = timezone.now() - timedelta(seconds=stale_seconds)
    ids = list(BlindScoreOutbox.objects.filter(
        Q(state='pending') | Q(state='queued', queued_at__lt=cutoff) | Q(state='queued', queued_at__isnull=True),
    ).order_by('pk').values_list('pk', flat=True)[:limit])
    for outbox_id in ids:
        with transaction.atomic():
            row = BlindScoreOutbox.objects.select_for_update().filter(pk=outbox_id).first()
            if row is None:
                continue
            if row.state == 'queued' and (row.queued_at is None or row.queued_at < cutoff):
                row.state, row.token, row.queued_at = 'pending', uuid4(), None
                row.save(update_fields=['state', 'token', 'queued_at', 'updated_at'])
            if row.state == 'pending':
                transaction.on_commit(lambda pk=row.pk: dispatch(pk))
    return len(ids)
