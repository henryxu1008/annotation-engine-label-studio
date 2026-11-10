"""Private bbox artifact primitives; no task or source authorization here."""

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from core.utils.private_file import PrivateFileError, checked_file, directory_gate
from data_export import audit_files

DOMAIN = 'bbox-media-storage/v1'
MARKER = '.bbox-media-storage-v1'


def root(*, create=False):
    """Reuse the already-validated private export root, with a separate namespace."""
    base = audit_files.private_root().parent
    path = base / 'bbox-media'
    if create:
        created = not path.exists()
        path.mkdir(mode=0o700, exist_ok=True)
        if created:
            audit_files.secure_new_directory(path)
    audit_files.no_links(path)
    if not path.is_dir():
        raise PrivateFileError()
    if os.name == 'nt':
        audit_files.windows_acl(path)
    else:
        directory_gate(path, owner_id=os.geteuid())
    return path


def binding(path, *, create=False):
    return audit_files.storage_binding(root=path, create=create, domain=DOMAIN, marker_name=MARKER)


def reserve_namespace():
    directory = root(create=True)
    return binding(directory, create=True)


def publish(token, data, *, expected_binding=None):
    """Publish immutable candidate bytes; callers must fence DB publication.

    Do not remove candidates on an uncertain database commit. Reconciliation
    must decide whether a candidate is referenced before any cleanup.
    """
    if not isinstance(token, UUID) or not isinstance(data, bytes) or not data:
        raise ValueError('Invalid artifact')
    directory = root(create=True)
    storage_id = binding(directory, create=True)
    if expected_binding is not None and storage_id != expected_binding:
        raise PrivateFileError()
    path = directory / str(token)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as writer:
        writer.write(data)
        writer.flush()
        os.fsync(writer.fileno())
    if os.name == 'nt':
        audit_files.windows_acl(path)
    else:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return str(token), storage_id


def discard_candidate(token, expected_binding):
    """Remove only a fenced candidate in its original private namespace."""
    if not isinstance(token, UUID) or not isinstance(expected_binding, str):
        raise PrivateFileError()
    directory = root()
    if binding(directory) != expected_binding:
        raise PrivateFileError()
    path = directory / str(token)
    audit_files.no_links(path)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise PrivateFileError()
        path.unlink()
    except FileNotFoundError:
        return False
    if os.name != 'nt':
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return True


@contextmanager
def open_verified(token, storage_id, expected_hash, expected_size):
    if not isinstance(token, UUID) or not isinstance(storage_id, str):
        raise PrivateFileError()
    directory = root()
    if binding(directory) != storage_id:
        raise PrivateFileError()
    path = directory / str(token)
    if path.parent != directory or Path(str(token)).name != str(token):
        raise PrivateFileError()
    audit_files.no_links(path)
    with checked_file(path, expected_hash, expected_size) as reader:
        yield reader
