"""Audit adapter for the existing private root and checked descriptor helper."""
import os
import re
import secrets
import shutil
import stat
from pathlib import Path

from core.utils.private_file import PrivateFileError, ancestor_gate, directory_gate
from core.utils.private_file import no_links as path_links
from django.conf import settings

from . import approved
from . import audit_contract as c


def secure_new_directory(path):
    """A newly created private directory gets an explicit protected Windows DACL."""
    if os.name != 'nt':
        return
    import ctypes
    from ctypes import wintypes as w

    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    adv.GetTokenInformation.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        w.LPCWSTR,
        w.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    adv.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(w.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(w.BOOL),
    ]
    adv.SetNamedSecurityInfoW.argtypes = [
        w.LPWSTR,
        w.DWORD,
        w.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    token = w.HANDLE()
    text = w.LPWSTR()
    descriptor = ctypes.c_void_p()
    try:
        if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            c.fail('PRIVATE_STORAGE_REQUIRED', 503)
        size = w.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            c.fail('PRIVATE_STORAGE_REQUIRED', 503)
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            c.fail('PRIVATE_STORAGE_REQUIRED', 503)
        sddl = f'D:P(A;OICI;FA;;;{text.value})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)'
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            c.fail('PRIVATE_STORAGE_REQUIRED', 503)
        present, defaulted = w.BOOL(), w.BOOL()
        dacl = ctypes.c_void_p()
        if not adv.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
        ):
            c.fail('PRIVATE_STORAGE_REQUIRED', 503)
        if adv.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None):
            c.fail('PRIVATE_STORAGE_REQUIRED', 503)
    finally:
        if text:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        if descriptor:
            kernel.LocalFree(descriptor)
        if token:
            kernel.CloseHandle(token)


def windows_acl(path, *, ancestor=False):
    """Reject writable ACL trustees outside server user, SYSTEM and Administrators."""
    import ctypes
    from ctypes import wintypes as w

    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    adv.GetNamedSecurityInfoW.argtypes = [
        w.LPWSTR,
        w.DWORD,
        w.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    adv.GetNamedSecurityInfoW.restype = w.DWORD
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    adv.GetTokenInformation.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
    adv.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    adv.ConvertStringSidToSidW.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    adv.LookupAccountNameW.argtypes = [
        w.LPCWSTR,
        w.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(w.DWORD),
        w.LPWSTR,
        ctypes.POINTER(w.DWORD),
        ctypes.POINTER(w.DWORD),
    ]
    adv.GetAce.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    token = w.HANDLE()
    trusted = []
    try:
        if (
            adv.GetNamedSecurityInfoW(
                str(path), 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
            )
            or not dacl
        ):
            c.fail('PRIVATE_STORAGE_REQUIRED', 503)
        if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            c.fail('PRIVATE_STORAGE_REQUIRED', 503)
        size = w.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            c.fail('PRIVATE_STORAGE_REQUIRED', 503)
        user = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        for text in ('S-1-5-18', 'S-1-5-32-544'):
            pointer = ctypes.c_void_p()
            if not adv.ConvertStringSidToSidW(text, ctypes.byref(pointer)):
                c.fail('PRIVATE_STORAGE_REQUIRED', 503)
            trusted.append(pointer)
        allowed = [user, *trusted]
        # The Windows volume root is normally owned by TrustedInstaller.
        sid_size, domain_size, usage = w.DWORD(), w.DWORD(), w.DWORD()
        adv.LookupAccountNameW(
            None,
            'NT SERVICE\\TrustedInstaller',
            None,
            ctypes.byref(sid_size),
            None,
            ctypes.byref(domain_size),
            ctypes.byref(usage),
        )
        service = ctypes.create_string_buffer(sid_size.value)
        domain = ctypes.create_unicode_buffer(domain_size.value)
        if sid_size.value and adv.LookupAccountNameW(
            None,
            'NT SERVICE\\TrustedInstaller',
            service,
            ctypes.byref(sid_size),
            domain,
            ctypes.byref(domain_size),
            ctypes.byref(usage),
        ):
            allowed.append(ctypes.cast(service, ctypes.c_void_p))
        if not any(adv.EqualSid(owner, sid) for sid in allowed):
            c.fail('PRIVATE_STORAGE_REQUIRED', 503)
        # ACL header: revision,u8,size,u16,aceCount,u16,sbz2,u16.
        count = ctypes.c_ushort.from_address(dacl.value + 4).value
        for index in range(count):
            ace = ctypes.c_void_p()
            if not adv.GetAce(dacl, index, ctypes.byref(ace)):
                c.fail('PRIVATE_STORAGE_REQUIRED', 503)
            kind = ctypes.c_ubyte.from_address(ace.value).value
            flags = ctypes.c_ubyte.from_address(ace.value + 1).value
            if flags & 0x8:   # inherit-only ACE does not apply to this directory
                continue
            if kind == 0:
                mask = ctypes.c_uint32.from_address(ace.value + 4).value
                unsafe = 0x40 | 0x10000 | 0x40000 | 0x80000 | 0x10000000 | 0x40000000
                if not ancestor:
                    unsafe |= 0x2 | 0x4 | 0x10 | 0x100
                if mask & unsafe:
                    if not any(adv.EqualSid(ctypes.c_void_p(ace.value + 8), sid) for sid in allowed):
                        c.fail('PRIVATE_STORAGE_REQUIRED', 503)
            elif kind not in (1, 2):
                # Object/callback allow ACEs require a richer trusted ACL adapter.
                c.fail('PRIVATE_STORAGE_REQUIRED', 503)
    finally:
        for sid in trusted:
            kernel.LocalFree(sid)
        if descriptor:
            kernel.LocalFree(descriptor)
        if token:
            kernel.CloseHandle(token)


def no_links(path):
    try:
        path_links(path)
    except PrivateFileError:
        c.fail('FILE_INVALID')


def private_root():
    configured = Path(settings.APPROVED_EXPORT_ROOT).absolute()
    no_links(configured)
    created = not configured.exists()
    base = approved.private_root()
    if created:
        secure_new_directory(base)
    root = base / 'audit-packages'
    for public in (settings.MEDIA_ROOT, settings.STATIC_ROOT):
        if public:
            target = Path(public).resolve()
            if target == root or target in root.parents or root in target.parents:
                c.fail('PRIVATE_STORAGE_REQUIRED', 503)
    created = not root.exists()
    root.mkdir(mode=0o700, exist_ok=True)
    if created:
        secure_new_directory(root)
    no_links(root)
    for item in (root, root.parent):
        if os.name == 'nt':
            windows_acl(item)
        else:
            try:
                directory_gate(item, owner_id=os.geteuid())
            except (PrivateFileError, OSError):
                c.fail('PRIVATE_STORAGE_REQUIRED', 503)
    for item in root.parent.parents:
        if os.name == 'nt':
            windows_acl(item, ancestor=True)
    if os.name != 'nt':
        try:
            ancestor_gate(root.parent, owner_id=os.geteuid())
        except (PrivateFileError, OSError):
            c.fail('PRIVATE_STORAGE_REQUIRED', 503)
    return root


def storage_binding(*, root=None, create=False):
    """Bind workers to the private storage namespace captured by the web process.

    The random marker lives on the shared filesystem; only capture may create
    it. A worker never initializes a missing namespace. Its value is kept out
    of the public package and only a domain-separated digest is frozen.
    """
    root = root or private_root()
    marker = root / '.review-audit-storage-v1'
    try:
        no_links(marker)
        if create:
            try:
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(secrets.token_hex(32).encode('ascii'))
                    handle.flush()
                    os.fsync(handle.fileno())
        fd = os.open(marker, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 64:
                c.fail('RUNTIME_CHANGED')
            if os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
                c.fail('PRIVATE_STORAGE_REQUIRED', 503)
            if os.name == 'nt':
                windows_acl(marker)
            value = handle.read(65)
            if not re.fullmatch(b'[0-9a-f]{64}', value):
                c.fail('RUNTIME_CHANGED')
            current = marker.stat()
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                c.fail('RUNTIME_CHANGED')
        root_info = root.stat()
        # A copied marker must not make two separate directories equivalent.
        # Shared volumes must expose stable directory identities to both
        # processes; a mismatched mount fails closed instead of publishing.
        return c.digest(c.exact({'domain': 'review-audit-storage/v1', 'marker': value.decode('ascii'),
                                 'path': os.path.normcase(str(root.resolve())),
                                 'device': root_info.st_dev, 'inode': root_info.st_ino}))
    except FileNotFoundError:
        c.fail('RUNTIME_CHANGED')
    except OSError:
        c.fail('PRIVATE_STORAGE_REQUIRED', 503)


def directory(package):
    return private_root() / str(package.pk) / str(package.token)


def artifact_path(package, artifact):
    expected = directory(package) / 'review-audit.zip'
    path = private_root() / artifact.storage_key
    if path != expected or artifact.attempt_id != package.token or artifact.package_id != package.pk:
        c.fail('FILE_INVALID')
    no_links(path)
    if path.absolute() != path.resolve(strict=True):
        c.fail('FILE_INVALID')
    return path


class ScratchLease:
    """Two fixed exclusive slots retain unknown writers' byte reservations.

    A lease timeout changes a producer token, not the fact that an old OS writer
    might resume. Never release its reservation on that assumption.
    """

    def __init__(self, package, token):
        self.root = private_root()
        self.parent = self.root / str(package.pk)
        self.parent.mkdir(mode=0o700, exist_ok=True)
        no_links(self.parent)
        self.token = str(token)
        self.path = None
        self.capacity = min(c.limits()['artifact'], (c.limits()['scratch'] - 72) // 2)
        if self.capacity < 1:
            c.fail('SCRATCH_LIMIT', 422)
        for number in range(2):
            path = self.parent / f'.scratch-{number}'
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            with os.fdopen(fd, 'wb') as stream:
                stream.write(self.token.encode('ascii'))
                stream.flush()
                os.fsync(stream.fileno())
            self.path = path
            return
        c.fail('SCRATCH_LIMIT', 422)

    def release(self):
        no_links(self.path)
        if self.path.read_bytes() != self.token.encode('ascii'):
            c.fail('FILE_INVALID')
        self.path.unlink()

    def discard(self, path):
        # Explicit absolute, root-contained allowlist before recursive removal.
        expected = self.parent / self.token
        no_links(path)
        if path != expected or path.resolve() != expected or not path.resolve().is_relative_to(self.root):
            c.fail('FILE_INVALID')
        if path.exists():
            for item in path.iterdir():
                no_links(item)
                if not item.is_file() or item.name != 'review-audit.zip':
                    c.fail('FILE_INVALID')
                item.chmod(0o600)
            shutil.rmtree(path)
        self.release()
