import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows native handles')


@pytest.fixture(autouse=True)
def load_windows_module():
    global m
    from io_storages.localfiles import secure_open_windows as m


@pytest.fixture
def paths(tmp_path):
    private = tmp_path / 'private'
    private.mkdir()
    allowed = tmp_path / 'allowed'
    allowed.mkdir()

    def guard(path, *, storage_root=False):
        p = Path(path).resolve()
        q = private.resolve()
        return p == q or q in p.parents or (storage_root and p in q.parents)

    return private, allowed, guard


def test_normal_read_write(paths):
    _, allowed, guard = paths
    target = allowed / 'data.txt'
    with m.open_file(allowed, target, write=True, is_private=guard) as f:
        f.write('SYNTHETIC')
    with m.open_file(allowed, target, write=False, is_private=guard) as f:
        assert f.read() == b'SYNTHETIC'


def test_private_root_denied(paths):
    private, _, guard = paths
    target = private / 'x'
    with pytest.raises(PermissionError):
        m.open_file(private, target, write=True, is_private=guard)
    assert not target.exists()


def test_hardlink_denied(paths):
    private, allowed, guard = paths
    secret = private / 'secret'
    secret.write_text('PRIVATE')
    target = allowed / 'hard'
    os.link(secret, target)
    for write in (False, True):
        with pytest.raises(PermissionError):
            m.open_file(allowed, target, write=write, is_private=guard)
    assert secret.read_text() == 'PRIVATE'


def test_pinned_directory_cannot_move_during_write(paths):
    private, allowed, guard = paths
    target = allowed / 'data'
    with m.open_file(allowed, target, write=True, is_private=guard) as stream:
        with pytest.raises(PermissionError):
            allowed.rename(private / 'moved')
        stream.write('SYNTHETIC')
    assert target.read_text() == 'SYNTHETIC' and not (private / 'moved').exists()


def test_junction_denied(paths):
    import subprocess

    private, allowed, guard = paths
    (private / 'secret').write_text('PRIVATE')
    junction = allowed / 'alias'
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(private)], check=True, capture_output=True)
    try:
        with pytest.raises(OSError):
            m.open_file(allowed, junction / 'secret', write=False, is_private=guard)
    finally:
        os.rmdir(junction)


def test_existing_file_is_truncated_and_writable(paths):
    _, allowed, guard = paths
    target = allowed / 'existing'
    target.write_text('LONG-ORIGINAL-CONTENT')
    with m.open_file(allowed, target, write=True, is_private=guard) as stream:
        stream.write('NEW')
    assert target.read_text() == 'NEW'


def test_handles_are_released_on_close(paths):
    private, allowed, guard = paths
    target = allowed / 'data'
    target.write_text('DATA')
    f = m.open_file(allowed, target, write=False, is_private=guard)
    f.close()
    f.close()
    allowed.rename(private / 'moved')
    assert (private / 'moved' / 'data').read_text() == 'DATA'


def test_preexisting_delete_handle_denies_before_creation(paths):
    private, allowed, guard = paths
    h = m.k.CreateFileW(str(allowed), 0x10000, 7, None, 3, 0x02200000, None)
    assert h != m.ctypes.c_void_p(-1).value
    try:
        with pytest.raises(OSError):
            m.open_file(allowed, allowed / 'new', write=True, is_private=guard)
        assert not (allowed / 'new').exists() and list(private.iterdir()) == []
    finally:
        m.k.CloseHandle(h)


def test_parent_reparse_after_pin_fails_closed(paths, monkeypatch):
    import ctypes
    import struct
    from ctypes import wintypes

    private, allowed, guard = paths
    (private / 'secret').write_text('PRIVATE')
    child = allowed / 'child'
    child.mkdir()
    real = m._relative
    swapped = []
    m.k.DeviceIoControl.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    )
    m.k.DeviceIoControl.restype = wintypes.BOOL

    def inject(parent, name, **kwargs):
        if name == 'secret' and not swapped:
            h = m.k.CreateFileW(str(child), 0x100, 7, None, 3, 0x02200000, None)
            assert h != ctypes.c_void_p(-1).value
            try:
                substitute = ('\\??\\' + str(private)).encode('utf-16-le')
                printable = str(private).encode('utf-16-le')
                data = (
                    struct.pack(
                        '<IHHHHHH',
                        0xA0000003,
                        8 + len(substitute) + 2 + len(printable) + 2,
                        0,
                        0,
                        len(substitute),
                        len(substitute) + 2,
                        len(printable),
                    )
                    + substitute
                    + b'\0\0'
                    + printable
                    + b'\0\0'
                )
                buf = ctypes.create_string_buffer(data)
                ret = wintypes.DWORD()
                assert m.k.DeviceIoControl(h, 0x900A4, buf, len(data), None, 0, ctypes.byref(ret), None)
                swapped.append(True)
            finally:
                m.k.CloseHandle(h)
        return real(parent, name, **kwargs)

    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(m, '_relative', inject)
            with pytest.raises(OSError):
                m.open_file(allowed, child / 'secret', write=False, is_private=guard)
        assert swapped and (private / 'secret').read_text() == 'PRIVATE'
    finally:
        if swapped:
            os.rmdir(child)


def test_dispatch_with_private_settings(settings, tmp_path):
    from io_storages.localfiles import secure_open

    private = tmp_path / 'private'
    private.mkdir()
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    settings.BASE_DATA_DIR = str(private)
    settings.APPROVED_EXPORT_ROOT = str(private / 'exports')
    settings.LOCAL_FILES_PROTECTED_ROOTS = []
    secret = private / 'secret'
    secret.write_text('PRIVATE')
    target = allowed / 'asset'
    with secure_open.open_local_file_write(allowed, target) as stream:
        stream.write('PUBLIC')
    with secure_open.open_local_file_readonly(allowed, target) as stream:
        assert stream.read() == b'PUBLIC'
    with pytest.raises(PermissionError):
        secure_open.open_local_file_readonly(private, secret)
    secure_open.unlink_local_file(allowed, target)
    assert not target.exists()
    with pytest.raises(FileNotFoundError):
        secure_open.unlink_local_file(allowed, target)
    assert secret.read_text() == 'PRIVATE'


def test_alternate_stream_and_outside_target_denied(paths):
    private, allowed, guard = paths
    outside = private / 'secret'
    outside.write_text('PRIVATE')
    with pytest.raises(PermissionError):
        m.open_file(allowed, outside, write=True, is_private=guard)
    with pytest.raises(PermissionError):
        m.open_file(allowed, allowed / 'item:stream', write=True, is_private=guard)
    assert outside.read_text() == 'PRIVATE'


def test_delete_regular_and_missing(paths):
    _, allowed, guard = paths
    target = allowed / 'data'
    target.write_text('DATA')
    m.unlink_file(allowed, target, is_private=guard)
    assert not target.exists()
    with pytest.raises(FileNotFoundError):
        m.unlink_file(allowed, target, is_private=guard)


def test_delete_private_and_hardlink_denied(paths):
    private, allowed, guard = paths
    secret = private / 'secret'
    secret.write_text('PRIVATE')
    link = allowed / 'link'
    os.link(secret, link)
    with pytest.raises(PermissionError):
        m.unlink_file(private, secret, is_private=guard)
    with pytest.raises(PermissionError):
        m.unlink_file(allowed, link, is_private=guard)
    assert secret.read_text() == 'PRIVATE' and link.exists()


def test_delete_name_cannot_be_replaced_after_open(paths, monkeypatch):
    private, allowed, guard = paths
    target = allowed / 'data'
    target.write_text('ORIGINAL')
    real = m._final
    attempted = []

    def inject(handle):
        p = real(handle)
        if p == target and not attempted:
            try:
                target.rename(allowed / 'moved')
                attempted.append('replaced')
            except OSError as e:
                attempted.append(e.winerror)
        return p

    with monkeypatch.context() as patcher:
        patcher.setattr(m, '_final', inject)
        m.unlink_file(allowed, target, is_private=guard)
    assert (
        attempted == [32]
        and not target.exists()
        and not (allowed / 'moved').exists()
        and list(private.iterdir()) == []
    )


def test_delete_parent_move_denied_while_handle_open(paths, monkeypatch):
    private, allowed, guard = paths
    target = allowed / 'data'
    target.write_text('DATA')
    real = m._final
    attempted = []

    def inject(handle):
        result = real(handle)
        if result == target and not attempted:
            try:
                allowed.rename(private / 'moved')
                attempted.append('moved')
            except OSError as e:
                attempted.append(e.winerror)
        return result

    with monkeypatch.context() as patcher:
        patcher.setattr(m, '_final', inject)
        m.unlink_file(allowed, target, is_private=guard)
    assert attempted == [32] and not target.exists() and not (private / 'moved').exists()


def test_delete_junction_target_denied(paths):
    import subprocess

    private, allowed, guard = paths
    secret = private / 'secret'
    secret.write_text('PRIVATE')
    junction = allowed / 'alias'
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(private)], check=True, capture_output=True)
    try:
        with pytest.raises(OSError):
            m.unlink_file(allowed, junction / 'secret', is_private=guard)
        assert secret.read_text() == 'PRIVATE'
    finally:
        os.rmdir(junction)


@pytest.mark.django_db
def test_http_rejects_junction_into_private_root(settings, tmp_path, project_id, business_client):
    import subprocess

    from django.urls import reverse
    from io_storages.localfiles.models import LocalFilesImportStorage
    from projects.models import Project

    private = tmp_path / 'private'
    private.mkdir()
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    secret = private / 'secret.txt'
    secret.write_text('PRIVATE')
    public = allowed / 'public.txt'
    public.write_text('PUBLIC')
    hardlink = allowed / 'private-hardlink.txt'
    os.link(secret, hardlink)
    settings.BASE_DATA_DIR = str(private)
    settings.APPROVED_EXPORT_ROOT = str(private / 'exports')
    settings.LOCAL_FILES_PROTECTED_ROOTS = []
    settings.LOCAL_FILES_SERVING_ENABLED = True
    settings.LOCAL_FILES_DOCUMENT_ROOT = str(tmp_path)
    LocalFilesImportStorage.objects.create(project=Project.objects.get(pk=project_id), path=str(allowed))
    junction = allowed / 'alias'
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(private)], check=True, capture_output=True)
    try:
        url = reverse('storages:localfiles_data')
        response = business_client.get(url, {'d': 'allowed/public.txt'})
        assert response.status_code == 200
        assert b''.join(response.streaming_content) == b'PUBLIC'
        response.close()
        assert business_client.get(url, {'d': 'allowed/alias/secret.txt'}).status_code == 404
        assert business_client.get(url, {'d': 'allowed/private-hardlink.txt'}).status_code == 404
        assert secret.read_text() == 'PRIVATE'
    finally:
        os.rmdir(junction)
