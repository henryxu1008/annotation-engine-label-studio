"""Deny mutable originals after a task has a pinned media artifact."""

from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.db import connection

from .models import BboxMediaPin


def pinned_upload(upload):
    from data_import.models import FileUpload

    if connection.vendor not in ('postgresql', 'sqlite'):
        return BboxMediaPin.objects.exists()
    if BboxMediaPin.objects.filter(source__kind='managed-upload', source__upload_name=upload.file.name).exists():
        return True
    uploads = list(FileUpload.objects.filter(file=upload.file.name).values_list('pk', flat=True)[:2])
    if len(uploads) != 1:
        return BboxMediaPin.objects.filter(source__kind='managed-upload').exists()
    return BboxMediaPin.objects.filter(source__kind='managed-upload', source__upload_id=uploads[0]).exists()


def pinned_local_path(path):
    """Resolve a LocalFiles path back to a managed upload, across projects."""
    from data_import.models import FileUpload

    if connection.vendor not in ('postgresql', 'sqlite'):
        return BboxMediaPin.objects.exists()
    try:
        relative = Path(path).resolve(strict=True).relative_to(Path(settings.MEDIA_ROOT).resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    if BboxMediaPin.objects.filter(source__kind='managed-upload', source__upload_name=relative.as_posix()).exists():
        return True
    uploads = list(FileUpload.objects.filter(file=relative.as_posix()).values_list('pk', flat=True)[:2])
    if len(uploads) > 1:
        # Multiple uploads with one path cannot safely be attributed to a pin.
        return BboxMediaPin.objects.filter(source__kind='managed-upload').exists()
    return bool(uploads) and BboxMediaPin.objects.filter(
        source__kind='managed-upload', source__upload_id=uploads[0]
    ).exists()


def pinned_storage(project, uri):
    """Match the same bucket/key semantics as GCS/S3 storage readers.

    Ambiguous references are denied when any object pins exist.
    The original may have multiple frozen versions, so redirecting it to an
    arbitrary pin would not be a safe substitute.
    """
    if connection.vendor not in ('postgresql', 'sqlite'):
        return BboxMediaPin.objects.exists()
    # A different project can point at the same mutable bucket/key. A
    # project-local check would permit an authorized second proxy to bypass
    # the fixed copy. Names shared by separate S3 endpoints are conservatively
    # blocked until endpoint identity is available in the public read path.
    pins = BboxMediaPin.objects.filter(source__kind__in=['gcs', 's3'])
    try:
        parsed = urlparse(uri, allow_fragments=False)
        if parsed.scheme not in ('gs', 's3') or not parsed.netloc or not parsed.path.lstrip('/'):
            return pins.exists()
        canonical = f'{parsed.scheme}://{parsed.netloc}/{parsed.path.lstrip("/")}'
        return pins.filter(source__reference=canonical).exists()
    except (TypeError, ValueError):
        return pins.exists()


def pinned_task_redirect(task, uri):
    """Identify a task's own pin for a legacy task-scoped storage URL.

    The caller must redirect to the authenticated pin endpoint, which rechecks
    current assignment and the complete binding before returning any bytes.
    Project-scoped URLs and pins belonging to other tasks remain denied.
    """
    if connection.vendor not in ('postgresql', 'sqlite'):
        return None
    try:
        parsed = urlparse(uri, allow_fragments=False)
        if parsed.scheme not in ('gs', 's3') or not parsed.netloc or not parsed.path.lstrip('/'):
            return None
        canonical = f'{parsed.scheme}://{parsed.netloc}/{parsed.path.lstrip("/")}'
        return BboxMediaPin.objects.filter(task=task, project_id=task.project_id,
            source__kind='gcs' if parsed.scheme == 'gs' else 's3', source__reference=canonical).first()
    except (TypeError, ValueError):
        return None
