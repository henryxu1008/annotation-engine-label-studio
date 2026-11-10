"""Bounded capture primitive; this does not authorize or version a source.

The caller must establish project ownership and a single-version read before
calling this function. Its return value alone is not submission provenance.
"""

import hashlib
import os
import stat

from core.utils.private_file import PrivateFileError, no_links

from . import bbox


def capture_stream(stream, metadata):
    """Read and validate one complete, already-authorized image response."""
    try:
        if stream is None or not isinstance(metadata, dict):
            raise bbox.Incomparable('unverified_image')
        size = metadata.get('ContentLength')
        if (type(size) is not int or size <= 0 or metadata.get('StatusCode') != 200
                or metadata.get('ContentRange')):
            raise bbox.Incomparable('unverified_image')
        if size > bbox.MAX_IMAGE_BYTES:
            raise bbox.Error('BBOX_TOO_LARGE')
        data = bytearray()
        for chunk in stream.iter_chunks(chunk_size=256 * 1024):
            if not isinstance(chunk, bytes):
                raise bbox.Incomparable('unverified_image')
            if len(data) + len(chunk) > size:
                raise bbox.Incomparable('unverified_image')
            data.extend(chunk)
        if len(data) != size:
            raise bbox.Incomparable('unverified_image')
        content = bytes(data)
        return content, bbox.image_identity(content)
    except (bbox.Error, bbox.Incomparable):
        raise
    except Exception as exc:
        raise bbox.Incomparable('unverified_image') from exc
    finally:
        if stream is not None:
            stream.close()


def capture_gcs(blob, *, timeout=30):
    """Capture one SDK-pinned GCS generation; the caller must authorize blob.

    This is intentionally separate from the generic proxy: proxy metadata and
    downloaded content are not currently bound to the same generation.
    """
    try:
        blob.reload(timeout=timeout, retry=None)
        generation, size = blob.generation, blob.size
        if type(generation) is not int or generation <= 0 or type(size) is not int or size <= 0:
            raise bbox.Incomparable('unverified_image')
        if size > bbox.MAX_IMAGE_BYTES:
            raise bbox.Error('BBOX_TOO_LARGE')
        with blob.open('rb', chunk_size=256 * 1024, if_generation_match=generation,
                       timeout=timeout, retry=None) as reader:
            data = bytearray()
            while len(data) < size:
                chunk = reader.read(min(256 * 1024, size - len(data)))
                if not isinstance(chunk, bytes) or not chunk or len(data) + len(chunk) > size:
                    raise bbox.Incomparable('unverified_image')
                data.extend(chunk)
            if reader.read(1) != b'':
                raise bbox.Incomparable('unverified_image')
        content = bytes(data)
        return content, bbox.image_identity(content), generation
    except (bbox.Error, bbox.Incomparable):
        raise
    except Exception as exc:
        raise bbox.Incomparable('unverified_image') from exc


def capture_managed_upload(reference, project):
    """Read a project-owned managed upload, rejecting replacement or mutation.

    The caller must independently authorize project/task preparation. This
    capture is not itself a durable pin or evidence of annotation-time use.
    """
    from data_export.snapshots import upload_for

    upload, path = upload_for(reference, project)
    try:
        no_links(path)
        if path.absolute() != path.resolve(strict=True):
            raise bbox.Incomparable('unverified_image')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as reader:
            before = os.fstat(reader.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
                raise bbox.Incomparable('unverified_image')
            if before.st_size > bbox.MAX_IMAGE_BYTES:
                raise bbox.Error('BBOX_TOO_LARGE')
            data = reader.read(before.st_size + 1)
            after = os.fstat(reader.fileno())
            current = path.stat()
            if (len(data) != before.st_size or
                    (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or
                    (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
                raise bbox.Incomparable('unverified_image')
            reader.seek(0)
            check = hashlib.sha256()
            remaining = before.st_size + 1
            while remaining:
                chunk = reader.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                check.update(chunk)
                remaining -= len(chunk)
            if remaining != 1 or check.digest() != hashlib.sha256(data).digest():
                raise bbox.Incomparable('unverified_image')
            final = os.fstat(reader.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    final.st_size, final.st_mtime_ns, final.st_ctime_ns):
                raise bbox.Incomparable('unverified_image')
        no_links(path)
        return data, bbox.image_identity(data), upload.pk
    except (bbox.Error, bbox.Incomparable):
        raise
    except (OSError, PrivateFileError) as exc:
        raise bbox.Incomparable('unverified_image') from exc
