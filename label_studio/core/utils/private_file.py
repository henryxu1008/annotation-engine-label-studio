"""Descriptor integrity mechanics shared by private snapshots and audit exports."""
import hashlib
import os
import stat
from contextlib import contextmanager


class PrivateFileError(Exception):
    pass


def no_links(path):
    for item in (path, *path.parents):
        if not item.exists():
            continue
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise PrivateFileError()


def directory_gate(path, public_roots=(), *, owner_id=None):
    no_links(path)
    for public in public_roots:
        if public:
            target = public.resolve()
            if target == path or target in path.parents or path in target.parents:
                raise PrivateFileError()
    if owner_id is not None:
        info = path.stat()
        if info.st_uid != owner_id or info.st_mode & 0o022:
            raise PrivateFileError()


def ancestor_gate(path, *, owner_id):
    """Parents must not let another OS principal replace protected children.

    Root/current-user owned sticky directories (for example /tmp) protect an
    already-owned child; a non-sticky world/group writable parent does not.
    """
    no_links(path)
    for item in (path, *path.parents):
        info = item.stat()
        if info.st_uid not in (0, owner_id) or info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise PrivateFileError()


@contextmanager
def checked_file(path, expected_hash, size):
    """Verify and stream the same FD, under trusted immutable OS publication."""
    try:
        if path.absolute() != path.resolve(strict=True):
            raise PrivateFileError()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != size:
                raise PrivateFileError()
            if hashlib.file_digest(handle, 'sha256').hexdigest() != expected_hash:
                raise PrivateFileError()
            after = os.fstat(handle.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise PrivateFileError()
            handle.seek(0)
            yield handle
    except OSError as error:
        raise PrivateFileError() from error
