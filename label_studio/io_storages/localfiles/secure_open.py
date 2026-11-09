"""Open Local Files through pinned directory handles, never following symlinks."""
import os
import secrets
import stat
from pathlib import Path

from data_export.boundary import is_private_local_path


def _open_local_file(storage_root, filename, *, write):
    """Open a regular file beneath an authorized root and verify opened handles.

    Linux /proc and dir_fd support are required: platforms without a way to
    validate the actual opened object fail closed rather than trusting a path
    that can be replaced after authorization.
    """
    fd = None
    current = None
    try:
        root = Path(os.path.abspath(os.fspath(storage_root)))
        target = Path(os.path.abspath(os.fspath(filename)))
        relative = target.relative_to(root)
        if not relative.parts or is_private_local_path(root, storage_root=True):
            raise PermissionError('invalid local storage boundary')
        if os.open not in os.supports_dir_fd or not all(hasattr(os, name) for name in ('O_NOFOLLOW', 'O_DIRECTORY')):
            raise PermissionError('safe local file access unavailable')
        if not Path('/proc/self/fd').is_dir():
            raise PermissionError('file descriptor validation unavailable')
        common = getattr(os, 'O_CLOEXEC', 0) | os.O_NOFOLLOW
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | common
        current = os.open(root.anchor, directory_flags)
        for part in root.parts[1:]:
            nxt = os.open(part, directory_flags, dir_fd=current)
            os.close(current)
            current = nxt
        actual_root = Path(os.readlink(f'/proc/self/fd/{current}'))
        if not actual_root.is_absolute() or is_private_local_path(actual_root, storage_root=True):
            raise PermissionError('unsafe opened storage root')
        # Keep a pinned root fd to verify the file is still beneath that same root.
        root_fd = os.dup(current)
        try:
            for part in relative.parts[:-1]:
                nxt = os.open(part, directory_flags, dir_fd=current)
                os.close(current)
                current = nxt
            access = (os.O_WRONLY | os.O_CREAT) if write else os.O_RDONLY
            fd = os.open(relative.parts[-1], access | common | getattr(os, 'O_NONBLOCK', 0), 0o600, dir_fd=current)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PermissionError('not an independently linked regular file')
            actual_root = Path(os.readlink(f'/proc/self/fd/{root_fd}'))
            actual_file = Path(os.readlink(f'/proc/self/fd/{fd}'))
            if (
                not actual_root.is_absolute()
                or not actual_file.is_absolute()
                or is_private_local_path(actual_root, storage_root=True)
                or is_private_local_path(actual_file)
                or not actual_file.is_relative_to(actual_root)
            ):
                raise PermissionError('opened file escaped authorized storage')
            if write:
                os.ftruncate(fd, 0)
            result = os.fdopen(fd, 'w' if write else 'rb')
            fd = None
            return result
        finally:
            os.close(root_fd)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise PermissionError('safe local file access denied') from exc
    finally:
        if fd is not None:
            os.close(fd)
        if current is not None:
            os.close(current)


def open_local_file_readonly(storage_root, filename):
    return _open_local_file(storage_root, filename, write=False)


def open_local_file_write(storage_root, filename):
    return _open_local_file(storage_root, filename, write=True)


def unlink_local_file(storage_root, filename):
    """Unlink a regular file relative to pinned, verified no-follow directories.

    Never resolve the final entry as a symlink. A missing path is reported as
    FileNotFoundError so existing idempotent cleanup behavior is preserved.
    """
    current = None
    root_fd = None
    moved = False
    try:
        root = Path(os.path.abspath(os.fspath(storage_root)))
        target = Path(os.path.abspath(os.fspath(filename)))
        relative = target.relative_to(root)
        if not relative.parts or is_private_local_path(root, storage_root=True):
            raise PermissionError('invalid local storage boundary')
        if (
            os.open not in os.supports_dir_fd
            or os.unlink not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
            or os.stat not in os.supports_follow_symlinks
            or any(fn not in os.supports_dir_fd for fn in (os.mkdir, os.rename, os.link, os.rmdir))
            or not all(hasattr(os, name) for name in ('O_NOFOLLOW', 'O_DIRECTORY'))
        ):
            raise PermissionError('safe local deletion unavailable')
        if not Path('/proc/self/fd').is_dir():
            raise PermissionError('directory descriptor validation unavailable')
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
        current = os.open(root.anchor, flags)
        for part in root.parts[1:]:
            nxt = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = nxt
        root_fd = os.dup(current)
        for part in relative.parts[:-1]:
            nxt = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = nxt
        actual_root = Path(os.readlink(f'/proc/self/fd/{root_fd}'))
        actual_parent = Path(os.readlink(f'/proc/self/fd/{current}'))
        if (
            not actual_root.is_absolute()
            or not actual_parent.is_absolute()
            or is_private_local_path(actual_root, storage_root=True)
            or is_private_local_path(actual_parent)
            or not actual_parent.is_relative_to(actual_root)
        ):
            raise PermissionError('opened directory escaped authorized storage')
        entry = os.stat(relative.parts[-1], dir_fd=current, follow_symlinks=False)
        if not stat.S_ISREG(entry.st_mode):
            raise PermissionError('not a regular file')
        # Move the selected name into a private, same-filesystem quarantine
        # before deleting. A replacement after stat is restored, never deleted.
        qname = '.ls-delete-' + secrets.token_hex(16)
        os.mkdir(qname, 0o700, dir_fd=current)
        qfd = None
        moved = False
        try:
            qfd = os.open(qname, flags, dir_fd=current)
            qstat = os.fstat(qfd)
            if qstat.st_uid != os.geteuid() or stat.S_IMODE(qstat.st_mode) & 0o077:
                raise PermissionError('unsafe deletion quarantine')
            os.rename(relative.parts[-1], 'item', src_dir_fd=current, dst_dir_fd=qfd)
            moved = True
            actual = os.stat('item', dir_fd=qfd, follow_symlinks=False)
            if (actual.st_dev, actual.st_ino, actual.st_mode) != (entry.st_dev, entry.st_ino, entry.st_mode):
                # link() is no-replace; on collision retain the moved item in
                # quarantine for explicit operator recovery.
                os.link('item', relative.parts[-1], src_dir_fd=qfd, dst_dir_fd=current, follow_symlinks=False)
                os.unlink('item', dir_fd=qfd)
                moved = False
                raise PermissionError('local deletion target changed')
            os.unlink('item', dir_fd=qfd)
            moved = False
        finally:
            if qfd is not None:
                os.close(qfd)
            if not moved:
                os.rmdir(qname, dir_fd=current)
    except FileNotFoundError as exc:
        if moved:
            raise PermissionError('quarantined deletion target disappeared') from exc
        raise
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise PermissionError('safe local deletion denied') from exc
    finally:
        if root_fd is not None:
            os.close(root_fd)
        if current is not None:
            os.close(current)
