from unittest.mock import patch

import pytest
from django.urls import reverse
from io_storages.localfiles import views
from io_storages.localfiles.models import LocalFilesImportStorage
from projects.models import Project


@pytest.mark.parametrize('write', [False, True])
def test_hardlinked_private_file_is_not_read_or_truncated(settings, tmp_path, write):
    import os

    from io_storages.localfiles import secure_open

    private = tmp_path / 'private'
    private.mkdir()
    secret = private / 'secret.txt'
    secret.write_text('SYNTHETIC-PRIVATE')
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    alias = allowed / 'alias.txt'
    os.link(secret, alias)
    settings.BASE_DATA_DIR = str(private)
    settings.APPROVED_EXPORT_ROOT = str(private / 'exports')
    settings.LOCAL_FILES_PROTECTED_ROOTS = [str(private)]

    opener = secure_open.open_local_file_write if write else secure_open.open_local_file_readonly
    with pytest.raises(PermissionError):
        with opener(allowed, alias):
            pass
    assert secret.read_text() == 'SYNTHETIC-PRIVATE'


@pytest.mark.django_db
def test_swap_to_private_after_authorization_fails_closed(settings, tmp_path, business_client, project_id):
    private = tmp_path / 'private'
    private.mkdir()
    protected = private / 'protected.txt'
    protected.write_text('SYNTHETIC-PRIVATE-RACE')
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    target = allowed / 'document.txt'
    target.write_text('SYNTHETIC-ALLOWED')
    settings.BASE_DATA_DIR = str(private)
    settings.APPROVED_EXPORT_ROOT = str(private / 'exports')
    settings.LOCAL_FILES_PROTECTED_ROOTS = [str(private)]
    settings.LOCAL_FILES_SERVING_ENABLED = True
    settings.LOCAL_FILES_DOCUMENT_ROOT = str(tmp_path)
    LocalFilesImportStorage.objects.create(
        project=Project.objects.get(pk=project_id), path=str(allowed), use_blob_urls=True
    )
    real = views.build_localfile_response
    swapped = []

    def replace_after_authorization(*args, **kwargs):
        target.unlink()
        target.symlink_to(protected)
        swapped.append(True)
        return real(*args, **kwargs)

    with patch.object(views, 'build_localfile_response', side_effect=replace_after_authorization):
        response = business_client.get(
            reverse('storages:localfiles_data'), {'d': target.relative_to(tmp_path).as_posix()}
        )
    assert swapped, 'test did not reach check-to-open boundary'
    assert response.status_code == 404


@pytest.mark.django_db
def test_replace_storage_directory_after_authorization_denied(settings, tmp_path, business_client, project_id):
    private = tmp_path / 'private'
    private.mkdir()
    (private / 'document.txt').write_text('SYNTHETIC-PRIVATE-RACE')
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    (allowed / 'document.txt').write_text('SYNTHETIC-ALLOWED')
    settings.BASE_DATA_DIR = str(private)
    settings.APPROVED_EXPORT_ROOT = str(private / 'exports')
    settings.LOCAL_FILES_PROTECTED_ROOTS = [str(private)]
    settings.LOCAL_FILES_SERVING_ENABLED = True
    settings.LOCAL_FILES_DOCUMENT_ROOT = str(tmp_path)
    LocalFilesImportStorage.objects.create(
        project=Project.objects.get(pk=project_id), path=str(allowed), use_blob_urls=True
    )
    real = views.build_localfile_response
    swapped = []

    def replace_after_authorization(*args, **kwargs):
        allowed.rename(tmp_path / 'original-allowed')
        allowed.symlink_to(private, target_is_directory=True)
        swapped.append(True)
        return real(*args, **kwargs)

    with patch.object(views, 'build_localfile_response', side_effect=replace_after_authorization):
        response = business_client.get(reverse('storages:localfiles_data'), {'d': 'allowed/document.txt'})
    assert swapped
    assert response.status_code == 404


@pytest.mark.django_db
def test_response_close_releases_verified_file(settings, tmp_path, business_client, project_id):
    private = tmp_path / 'private'
    private.mkdir()
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    (allowed / 'document.txt').write_text('SYNTHETIC-ALLOWED')
    settings.BASE_DATA_DIR = str(private)
    settings.APPROVED_EXPORT_ROOT = str(private / 'exports')
    settings.LOCAL_FILES_PROTECTED_ROOTS = [str(private)]
    settings.LOCAL_FILES_SERVING_ENABLED = True
    settings.LOCAL_FILES_DOCUMENT_ROOT = str(tmp_path)
    LocalFilesImportStorage.objects.create(
        project=Project.objects.get(pk=project_id), path=str(allowed), use_blob_urls=True
    )
    real = views.open_local_file_readonly
    handles = []

    def track(*args, **kwargs):
        h = real(*args, **kwargs)
        handles.append(h)
        return h

    with patch.object(views, 'open_local_file_readonly', side_effect=track):
        response = business_client.get(reverse('storages:localfiles_data'), {'d': 'allowed/document.txt'})
    assert response.status_code == 200 and len(handles) == 1 and not handles[0].closed
    response.close()
    assert handles[0].closed


def test_export_open_does_not_follow_replaced_file(settings, tmp_path):
    from io_storages.localfiles.secure_open import open_local_file_write

    private = tmp_path / 'private'
    private.mkdir()
    secret = private / 'secret.txt'
    secret.write_text('SYNTHETIC-PRIVATE')
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    target = allowed / 'export.json'
    target.symlink_to(secret)
    settings.BASE_DATA_DIR = str(private)
    settings.APPROVED_EXPORT_ROOT = str(private / 'exports')
    settings.LOCAL_FILES_PROTECTED_ROOTS = [str(private)]
    with pytest.raises(PermissionError):
        open_local_file_write(allowed, target)
    assert secret.read_text() == 'SYNTHETIC-PRIVATE'
