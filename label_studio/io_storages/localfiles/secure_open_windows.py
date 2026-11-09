"""Windows handle-relative local file access with pinned directory handles."""
import ctypes
import msvcrt
import os
from ctypes import wintypes
from pathlib import Path

k = ctypes.WinDLL('kernel32', use_last_error=True)
n = ctypes.WinDLL('ntdll', use_last_error=True)
k.CreateFileW.argtypes = (
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
)
k.CreateFileW.restype = wintypes.HANDLE
k.CloseHandle.argtypes = (wintypes.HANDLE,)
k.GetFileType.argtypes = (wintypes.HANDLE,)
k.GetFileType.restype = wintypes.DWORD
k.SetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
k.SetFileInformationByHandle.restype = wintypes.BOOL
k.GetFinalPathNameByHandleW.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
k.GetFinalPathNameByHandleW.restype = wintypes.DWORD
n.RtlNtStatusToDosError.argtypes = (ctypes.c_long,)
n.RtlNtStatusToDosError.restype = wintypes.ULONG


class US(ctypes.Structure):
    _fields_ = [('Length', wintypes.USHORT), ('MaximumLength', wintypes.USHORT), ('Buffer', wintypes.LPWSTR)]


class OA(ctypes.Structure):
    _fields_ = [
        ('Length', wintypes.ULONG),
        ('RootDirectory', wintypes.HANDLE),
        ('ObjectName', ctypes.POINTER(US)),
        ('Attributes', wintypes.ULONG),
        ('SecurityDescriptor', ctypes.c_void_p),
        ('SecurityQualityOfService', ctypes.c_void_p),
    ]


class IO(ctypes.Structure):
    _fields_ = [('Status', ctypes.c_void_p), ('Information', ctypes.c_size_t)]


class INFO(ctypes.Structure):
    _fields_ = [
        ('attrs', wintypes.DWORD),
        ('created', wintypes.FILETIME),
        ('accessed', wintypes.FILETIME),
        ('written', wintypes.FILETIME),
        ('volume', wintypes.DWORD),
        ('size_hi', wintypes.DWORD),
        ('size_lo', wintypes.DWORD),
        ('links', wintypes.DWORD),
        ('index_hi', wintypes.DWORD),
        ('index_lo', wintypes.DWORD),
    ]


k.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.POINTER(INFO))
k.GetFileInformationByHandle.restype = wintypes.BOOL
n.NtCreateFile.argtypes = (
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.ULONG,
    ctypes.POINTER(OA),
    ctypes.POINTER(IO),
    ctypes.c_void_p,
    wintypes.ULONG,
    wintypes.ULONG,
    wintypes.ULONG,
    wintypes.ULONG,
    ctypes.c_void_p,
    wintypes.ULONG,
)
n.NtCreateFile.restype = ctypes.c_long


def _info(handle, directory):
    info = INFO()
    if k.GetFileType(handle) != 1:  # FILE_TYPE_DISK
        raise PermissionError('not a disk file')
    if not k.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    if info.attrs & 0x400 or bool(info.attrs & 0x10) != directory or (not directory and info.links != 1):
        raise PermissionError('reparse point, wrong type, or multiple hardlinks')
    return info


def _final(handle):
    size = k.GetFinalPathNameByHandleW(handle, None, 0, 0)
    if not size or size > 32768:
        raise PermissionError('cannot determine opened path')
    buf = ctypes.create_unicode_buffer(size + 1)
    if not k.GetFinalPathNameByHandleW(handle, buf, len(buf), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    text = buf.value
    if not text.startswith('\\\\?\\') or text.startswith('\\\\?\\UNC\\'):
        raise PermissionError('unsupported final path')
    return Path(text[4:])


def _relative(parent, name, *, directory, write=False, delete=False):
    if not name or name in ('.', '..') or any(x in name for x in ('/', '\\', ':', '\x00')):
        raise PermissionError('unsafe name')
    raw = name.encode('utf-16-le')
    if len(raw) > 65532:
        raise PermissionError('unsafe name length')
    buffer = ctypes.create_unicode_buffer(name)
    u = US(len(raw), len(raw) + 2, ctypes.cast(buffer, wintypes.LPWSTR))
    a = OA(ctypes.sizeof(OA), parent, ctypes.pointer(u), 0x1040, None, None)
    io = IO()
    handle = wintypes.HANDLE()
    access = 0x110080 if delete else (0x100081 if directory else (0x100182 if write else 0x100081))
    options = 0x200020 | (1 if directory else 0x40)
    status = n.NtCreateFile(
        ctypes.byref(handle),
        access,
        ctypes.byref(a),
        ctypes.byref(io),
        None,
        0,
        1,
        3 if write else 1,
        options,
        None,
        0,
    )
    if status < 0:
        raise ctypes.WinError(n.RtlNtStatusToDosError(status))
    try:
        _info(handle, directory)
        return handle.value
    except BaseException:
        k.CloseHandle(handle)
        raise


class PinnedFile:
    def __init__(self, file, handles):
        self.file = file
        self.handles = handles

    def __getattr__(self, name):
        return getattr(self.file, name)

    def __iter__(self):
        return iter(self.file)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        try:
            self.file.close()
        finally:
            for handle in reversed(self.handles):
                k.CloseHandle(handle)
            self.handles.clear()


def open_file(root, target, *, write, is_private):
    handles = []
    fh = None
    fd = None
    try:
        root, target = Path(os.path.abspath(root)), Path(os.path.abspath(target))
        try:
            relative = target.relative_to(root)
        except ValueError as exc:
            raise PermissionError('file outside local storage root') from exc
        if len(root.drive) != 2 or root.drive[1] != ':' or not relative.parts or is_private(root, storage_root=True):
            raise PermissionError('invalid root')
        anchor = k.CreateFileW(root.anchor, 0x100081, 1, None, 3, 0x02200000, None)
        if anchor == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        handles.append(anchor)
        _info(anchor, True)
        for part in root.parts[1:]:
            handles.append(_relative(handles[-1], part, directory=True))
        root_handle = handles[-1]
        for part in relative.parts[:-1]:
            handles.append(_relative(handles[-1], part, directory=True))
        resolved_root = _final(root_handle)
        if is_private(resolved_root, storage_root=True):
            raise PermissionError('private root')
        fh = _relative(handles[-1], relative.parts[-1], directory=False, write=write)
        actual = _final(fh)
        if is_private(actual, storage_root=False) or not actual.is_relative_to(resolved_root):
            raise PermissionError('outside authorized root')
        fd = msvcrt.open_osfhandle(fh, (os.O_WRONLY if write else os.O_RDONLY) | os.O_BINARY)
        fh = None
        if write:
            os.ftruncate(fd, 0)
        stream = os.fdopen(fd, 'w' if write else 'rb')
        fd = None
        result = PinnedFile(stream, handles)
        handles = []
        return result
    finally:
        if fd is not None:
            os.close(fd)
        if fh is not None:
            k.CloseHandle(fh)
        for handle in reversed(handles):
            k.CloseHandle(handle)


def unlink_file(root, target, *, is_private):
    """Delete only the opened file while its ancestor directory handles are pinned."""
    handles = []
    fh = None
    try:
        root, target = Path(os.path.abspath(root)), Path(os.path.abspath(target))
        try:
            relative = target.relative_to(root)
        except ValueError as exc:
            raise PermissionError('file outside local storage root') from exc
        if len(root.drive) != 2 or root.drive[1] != ':' or not relative.parts or is_private(root, storage_root=True):
            raise PermissionError('invalid root')
        anchor = k.CreateFileW(root.anchor, 0x100081, 1, None, 3, 0x02200000, None)
        if anchor == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        handles.append(anchor)
        _info(anchor, True)
        for part in root.parts[1:]:
            handles.append(_relative(handles[-1], part, directory=True))
        root_handle = handles[-1]
        for part in relative.parts[:-1]:
            handles.append(_relative(handles[-1], part, directory=True))
        resolved_root = _final(root_handle)
        if is_private(resolved_root, storage_root=True):
            raise PermissionError('private root')
        fh = _relative(handles[-1], relative.parts[-1], directory=False, delete=True)
        actual = _final(fh)
        if is_private(actual, storage_root=False) or not actual.is_relative_to(resolved_root):
            raise PermissionError('outside authorized root')
        disposition = wintypes.BOOL(True)
        if not k.SetFileInformationByHandle(fh, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if fh is not None:
            k.CloseHandle(fh)
        for handle in reversed(handles):
            k.CloseHandle(handle)
