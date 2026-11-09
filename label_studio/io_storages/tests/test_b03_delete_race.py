import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from io_storages.localfiles import models


def test_swap_storage_parent_before_delete_preserves_private_file(settings, tmp_path):
    private = tmp_path / 'private'
    private.mkdir()
    secret = private / 'victim.txt'
    secret.write_text('SYNTHETIC-PRIVATE')
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    (allowed / 'victim.txt').write_text('SYNTHETIC-ALLOWED')
    settings.BASE_DATA_DIR = str(private)
    settings.APPROVED_EXPORT_ROOT = str(private / 'exports')
    settings.LOCAL_FILES_PROTECTED_ROOTS = [str(private)]
    storage = models.LocalFilesExportStorage(path=str(allowed))
    annotation = MagicMock()
    real = Path.resolve
    swapped = []

    def resolve_and_swap(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        caller = inspect.currentframe().f_back.f_code.co_name
        if self == allowed and caller == 'delete_annotation' and not swapped:
            allowed.rename(tmp_path / 'original-allowed')
            allowed.symlink_to(private, target_is_directory=True)
            swapped.append(True)
        return result

    with patch.object(Path, 'resolve', resolve_and_swap), patch.object(
        models.LocalFilesExportStorageLink, 'get_key', return_value='victim.txt'
    ), patch.object(models.LocalFilesExportStorageLink.objects, 'filter') as links:
        try:
            storage.delete_annotation(annotation)
        except PermissionError:
            pass
    assert swapped, 'test did not reach check-to-delete boundary'
    assert secret.exists(), 'private synthetic file was deleted'
    assert secret.read_text() == 'SYNTHETIC-PRIVATE'
    links.assert_not_called()


def test_regular_and_missing_delete_keep_existing_semantics(settings, tmp_path):
    private = tmp_path / 'private'
    private.mkdir()
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    target = allowed / 'victim.txt'
    target.write_text('SYNTHETIC-ALLOWED')
    settings.BASE_DATA_DIR = str(private)
    settings.APPROVED_EXPORT_ROOT = str(private / 'exports')
    settings.LOCAL_FILES_PROTECTED_ROOTS = [str(private)]
    storage = models.LocalFilesExportStorage(path=str(allowed))
    annotation = MagicMock()
    with patch.object(models.LocalFilesExportStorageLink, 'get_key', return_value='victim.txt'), patch.object(
        models.LocalFilesExportStorageLink.objects, 'filter'
    ) as links:
        storage.delete_annotation(annotation)
        assert not target.exists()
        storage.delete_annotation(annotation)
    assert links.call_count == 2


def test_symlink_entry_does_not_remove_target(settings, tmp_path):
    from io_storages.localfiles.secure_open import unlink_local_file

    private = tmp_path / 'private'
    private.mkdir()
    secret = private / 'secret.txt'
    secret.write_text('SYNTHETIC-PRIVATE')
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    link = allowed / 'victim.txt'
    link.symlink_to(secret)
    settings.BASE_DATA_DIR = str(private)
    settings.APPROVED_EXPORT_ROOT = str(private / 'exports')
    settings.LOCAL_FILES_PROTECTED_ROOTS = [str(private)]
    with pytest.raises(PermissionError):
        unlink_local_file(allowed, link)
    assert secret.read_text() == 'SYNTHETIC-PRIVATE' and link.is_symlink()


def test_swap_root_after_helper_validation_denied(settings, tmp_path):
    import os

    from io_storages.localfiles import secure_open

    private = tmp_path / 'private'
    private.mkdir()
    secret = private / 'victim.txt'
    secret.write_text('SYNTHETIC-PRIVATE')
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    (allowed / 'victim.txt').write_text('SYNTHETIC-ALLOWED')
    settings.BASE_DATA_DIR = str(private)
    settings.APPROVED_EXPORT_ROOT = str(private / 'exports')
    settings.LOCAL_FILES_PROTECTED_ROOTS = [str(private)]

    class Proxy:
        def __getattr__(self, name):
            return getattr(os, name)

        def open(self, name, *a, **kw):
            if name == allowed.name and not swapped:
                allowed.rename(tmp_path / 'original-allowed')
                allowed.symlink_to(private, target_is_directory=True)
                swapped.append(True)
            return os.open(name, *a, **kw)

        @property
        def supports_dir_fd(self):
            return os.supports_dir_fd | {self.open}

    swapped = []
    with patch.object(secure_open, 'os', Proxy()), pytest.raises(PermissionError):
        secure_open.unlink_local_file(allowed, allowed / 'victim.txt')
    assert swapped and secret.read_text() == 'SYNTHETIC-PRIVATE'


def test_swap_entry_before_rename_never_follows_link(settings, tmp_path):
    import os

    from io_storages.localfiles import secure_open

    private = tmp_path / 'private'
    private.mkdir()
    secret = private / 'victim.txt'
    secret.write_text('SYNTHETIC-PRIVATE')
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    target = allowed / 'victim.txt'
    target.write_text('SYNTHETIC-ALLOWED')
    settings.BASE_DATA_DIR = str(private)
    settings.APPROVED_EXPORT_ROOT = str(private / 'exports')
    settings.LOCAL_FILES_PROTECTED_ROOTS = [str(private)]

    class Proxy:
        def __getattr__(self, name):
            return getattr(os, name)

        def rename(self, name, *a, **kw):
            if name == 'victim.txt' and not swapped:
                target.unlink()
                target.symlink_to(secret)
                swapped.append(True)
            return os.rename(name, *a, **kw)

        @property
        def supports_dir_fd(self):
            return os.supports_dir_fd | {self.rename}

    swapped = []
    with patch.object(secure_open, 'os', Proxy()), pytest.raises(PermissionError):
        secure_open.unlink_local_file(allowed, target)
    assert swapped and secret.read_text() == 'SYNTHETIC-PRIVATE' and target.is_symlink()


def test_swap_regular_entry_before_rename_is_restored(settings, tmp_path):
    import os

    from io_storages.localfiles import secure_open

    private = tmp_path / 'private'
    private.mkdir()
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    target = allowed / 'victim.txt'
    target.write_text('ORIGINAL-SYNTHETIC')
    settings.BASE_DATA_DIR = str(private)
    settings.APPROVED_EXPORT_ROOT = str(private / 'exports')
    settings.LOCAL_FILES_PROTECTED_ROOTS = [str(private)]
    swapped = []

    class Proxy:
        def __getattr__(self, name):
            return getattr(os, name)

        def rename(self, name, *args, **kwargs):
            if name == 'victim.txt' and not swapped:
                target.rename(allowed / 'original-held.txt')
                target.write_text('REPLACEMENT-SYNTHETIC')
                swapped.append(True)
            return os.rename(name, *args, **kwargs)

        @property
        def supports_dir_fd(self):
            return os.supports_dir_fd | {self.rename}

    with patch.object(secure_open, 'os', Proxy()), pytest.raises(PermissionError):
        secure_open.unlink_local_file(allowed, target)
    assert (
        swapped
        and target.read_text() == 'REPLACEMENT-SYNTHETIC'
        and (allowed / 'original-held.txt').read_text() == 'ORIGINAL-SYNTHETIC'
    )


def test_restore_collision_preserves_both_and_quarantine(settings, tmp_path):
    import os

    from io_storages.localfiles import secure_open

    private = tmp_path / 'private'
    private.mkdir()
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    target = allowed / 'victim.txt'
    target.write_text('ORIGINAL-SYNTHETIC')
    settings.BASE_DATA_DIR = str(private)
    settings.APPROVED_EXPORT_ROOT = str(private / 'exports')
    settings.LOCAL_FILES_PROTECTED_ROOTS = [str(private)]
    swapped = []

    class Proxy:
        def __getattr__(self, name):
            return getattr(os, name)

        def rename(self, name, *args, **kwargs):
            if name == 'victim.txt' and not swapped:
                target.rename(allowed / 'original-held.txt')
                target.write_text('REPLACEMENT-SYNTHETIC')
                swapped.append(True)
                result = os.rename(name, *args, **kwargs)
                target.write_text('COLLISION-SYNTHETIC')
                return result
            return os.rename(name, *args, **kwargs)

        @property
        def supports_dir_fd(self):
            return os.supports_dir_fd | {self.rename}

    with patch.object(secure_open, 'os', Proxy()), pytest.raises(PermissionError):
        secure_open.unlink_local_file(allowed, target)
    quarantine = list(allowed.glob('.ls-delete-*'))
    assert swapped and len(quarantine) == 1 and (quarantine[0] / 'item').read_text() == 'REPLACEMENT-SYNTHETIC'
    assert (
        target.read_text() == 'COLLISION-SYNTHETIC'
        and (allowed / 'original-held.txt').read_text() == 'ORIGINAL-SYNTHETIC'
    )
