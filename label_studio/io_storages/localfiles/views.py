"""
HTTP views dedicated to LocalFiles storage download operations.
"""
import logging
import mimetypes
import os
import posixpath
from pathlib import Path
from typing import Optional

from data_export.boundary import is_private_local_path
from django.conf import settings
from django.db.models import CharField, F, Value
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden, HttpResponseNotFound, HttpResponseNotModified
from django.utils._os import safe_join
from drf_spectacular.utils import extend_schema
from io_storages.localfiles.models import LocalFilesImportStorage
from io_storages.localfiles.secure_open import open_local_file_readonly
from ranged_fileresponse import RangedFileResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

logger = logging.getLogger(__name__)


"""
Utility helpers for LocalFiles storage operations.
"""


def _if_none_match_satisfied(header_value: Optional[str], etag: str) -> bool:
    """Return True if the client's cached representation matches the current file."""
    if not header_value:
        return False
    etag_candidates = [candidate.strip() for candidate in header_value.split(',') if candidate.strip()]
    return '*' in etag_candidates or etag in etag_candidates


def build_localfile_response(
    request: HttpRequest,
    full_path: str,
    if_none_match_header: Optional[str],
    storage_root: str,
) -> HttpResponse:
    """
    Stream the requested file and attach a weak ETag so browsers can cache it.

    We also honor `If-None-Match` headers to short-circuit the response with 304
    when the client's cached representation is still valid.
    """
    try:
        file_handle = open_local_file_readonly(storage_root, full_path)
    except OSError as exc:
        logger.error('Error opening file %s: %s', full_path, exc)
        return HttpResponseNotFound()

    # Weak ETag keeps the implementation simple while still invalidating on file edits.
    stat_result = os.fstat(file_handle.fileno())
    mtime_ns = getattr(stat_result, 'st_mtime_ns', int(stat_result.st_mtime * 1_000_000_000))
    etag = f'W/"{mtime_ns:x}-{stat_result.st_size:x}"'

    # Check if the client's cached representation matches the current file
    if _if_none_match_satisfied(if_none_match_header, etag):
        file_handle.close()
        not_modified = HttpResponseNotModified()
        not_modified['ETag'] = etag
        return not_modified

    # Detect mime type and encoding
    content_type, _ = mimetypes.guess_type(str(full_path))
    content_type = content_type or 'application/octet-stream'

    response = RangedFileResponse(request, file_handle, content_type=content_type)
    response._resource_closers.append(file_handle.close)
    response['ETag'] = etag  # Enables client-side caching for unchanged files.
    return response


"""
Main view for serving files residing under LocalFilesImportStorage roots with ETag support
"""


@extend_schema(exclude=True)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def localfiles_data(request):
    """Serve files residing under LocalFilesImportStorage roots with ETag support."""
    path = request.GET.get('d')
    if settings.LOCAL_FILES_SERVING_ENABLED is False:
        return HttpResponseForbidden(
            "Serving local files can be dangerous, so it's disabled by default. "
            'You can enable it with LOCAL_FILES_SERVING_ENABLED environment variable, '
            'please check docs: https://labelstud.io/guide/storage.html#Local-storage'
        )

    local_serving_document_root = settings.LOCAL_FILES_DOCUMENT_ROOT
    if path and request.user.is_authenticated:
        from access_control.identity import resolve_actor

        _, actor = resolve_actor(request)
        # Normalize the incoming relative path so we don't depend on trailing slashes
        path = posixpath.normpath(path).lstrip('/')
        full_path = Path(safe_join(local_serving_document_root, path))
        try:
            resolved = full_path.resolve()
            document_root = Path(local_serving_document_root).resolve()
            if not resolved.is_relative_to(document_root) or is_private_local_path(resolved):
                return HttpResponseNotFound()
        except (OSError, RuntimeError, ValueError):
            return HttpResponseNotFound()
        authorized_root = None
        matching_projects = set()

        # Storage paths are normalized on save/migration, so prefix matches using the
        # directory that contains the requested file stay consistent across OSes.
        full_path_dir = os.path.normpath(os.path.dirname(str(full_path)))
        localfiles_storage = LocalFilesImportStorage.objects.annotate(
            _full_path=Value(full_path_dir, output_field=CharField())
        ).filter(_full_path__startswith=F('path'))
        for storage in localfiles_storage:
            if (not is_private_local_path(storage.path, storage_root=True)
                    and resolved.is_relative_to(Path(storage.path).resolve())):
                matching_projects.add(storage.project_id)
                if storage.project.has_permission(actor):
                    authorized_root = storage.path

        # Open through pinned, no-follow directory handles after authorization.
        if authorized_root is not None:
            from classification_disagreements.bbox_source_guard import pinned_local_path

            if pinned_local_path(resolved):
                return HttpResponseNotFound()
            from classification_disagreements.blind_provenance import record_project_file_accesses

            record_project_file_accesses(account_id=actor.pk, project_ids=matching_projects)
            # Detect mime type and encoding
            return build_localfile_response(
                request=request,
                full_path=str(full_path),
                if_none_match_header=request.META.get('HTTP_IF_NONE_MATCH'),
                storage_root=authorized_root,
            )
        else:
            return HttpResponseNotFound()

    return HttpResponseForbidden()
