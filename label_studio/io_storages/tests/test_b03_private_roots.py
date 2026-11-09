import os

import pytest
from django.urls import reverse
from io_storages.localfiles.models import LocalFilesImportStorage
from io_storages.tests.b03_test_helpers import create_directory_alias, remove_directory_alias
from projects.models import Project
from rest_framework.exceptions import PermissionDenied


@pytest.fixture
def boundary(settings, tmp_path):
    private = tmp_path / 'private'
    private.mkdir()
    data = private / 'data'
    data.mkdir()
    exports = private / 'approved'
    exports.mkdir()
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    settings.BASE_DATA_DIR = str(data)
    settings.APPROVED_EXPORT_ROOT = str(exports)
    settings.LOCAL_FILES_PROTECTED_ROOTS = [str(private)]
    settings.LOCAL_FILES_SERVING_ENABLED = True
    settings.LOCAL_FILES_DOCUMENT_ROOT = str(tmp_path)
    return private, data, exports, allowed


@pytest.mark.django_db
def test_storage_rejects_private_ancestor_descendant_and_symlink(boundary, project_id, tmp_path):
    private, data, exports, allowed = boundary
    project = Project.objects.get(pk=project_id)
    link = tmp_path / 'alias'
    create_directory_alias(link, private)
    try:
        for candidate in (private, data, exports, tmp_path, link):
            with pytest.raises(PermissionDenied):
                LocalFilesImportStorage.objects.create(project=project, path=str(candidate), use_blob_urls=True)
        assert LocalFilesImportStorage.objects.create(project=project, path=str(allowed), use_blob_urls=True).pk
    finally:
        remove_directory_alias(link)


@pytest.mark.django_db
def test_existing_broad_storage_cannot_read_or_scan_private(boundary, business_client, project_id, tmp_path):
    private, data, exports, allowed = boundary
    project = Project.objects.get(pk=project_id)
    secret = private / 'canary.txt'
    secret.write_text('private-synthetic')
    public = allowed / 'asset.txt'
    public.write_text('public-synthetic')
    storage = LocalFilesImportStorage.objects.create(project=project, path=str(allowed), use_blob_urls=True)
    url = reverse('storages:localfiles_data')
    ok = business_client.get(url, {'d': public.relative_to(tmp_path).as_posix()})
    assert ok.status_code == 200 and b''.join(ok.streaming_content) == b'public-synthetic'
    LocalFilesImportStorage.objects.filter(pk=storage.pk).update(path=str(tmp_path))
    storage.refresh_from_db()
    response = business_client.get(url, {'d': secret.relative_to(tmp_path).as_posix()})
    assert response.status_code == 404
    assert business_client.get(url, {'d': public.relative_to(tmp_path).as_posix()}).status_code == 404
    with pytest.raises(PermissionDenied):
        list(storage.iter_objects())
    with pytest.raises(PermissionDenied):
        storage.get_data(str(secret))


@pytest.mark.django_db
@pytest.mark.skipif(os.name == 'nt', reason='Windows file symlink privileges are unavailable; hardlinks are tested natively')
def test_safe_storage_rejects_symlink_to_private(boundary, business_client, project_id, tmp_path):
    private, data, exports, allowed = boundary
    project = Project.objects.get(pk=project_id)
    secret = private / 'canary.txt'
    secret.write_text('private-synthetic')
    link = allowed / 'alias.txt'
    link.symlink_to(secret)
    storage = LocalFilesImportStorage.objects.create(project=project, path=str(allowed), use_blob_urls=True)
    response = business_client.get(reverse('storages:localfiles_data'), {'d': link.relative_to(tmp_path).as_posix()})
    assert response.status_code == 404 and list(storage.iter_objects()) == []
    with pytest.raises(PermissionDenied):
        storage.get_data(str(link))


@pytest.mark.django_db
def test_invalid_protected_configuration_denies_local_storage(boundary, settings, project_id):
    _, _, _, allowed = boundary
    settings.LOCAL_FILES_PROTECTED_ROOTS = None
    with pytest.raises(PermissionDenied):
        LocalFilesImportStorage.objects.create(
            project=Project.objects.get(pk=project_id), path=str(allowed), use_blob_urls=True
        )
