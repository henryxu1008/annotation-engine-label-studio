"""Integration checks for the project-owned upload capture primitive."""

from io import BytesIO

import pytest
from classification_disagreements.bbox_media import capture_managed_upload
from data_import.models import FileUpload
from django.core.files.base import ContentFile
from PIL import Image
from projects.tests.factories import ProjectFactory
from rest_framework.exceptions import APIException


@pytest.mark.django_db
def test_only_the_project_owned_managed_upload_can_be_captured(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / 'public')
    settings.APPROVED_EXPORT_ROOT = str(tmp_path / 'private')
    project, foreign = ProjectFactory(), ProjectFactory()
    output = BytesIO()
    Image.new('RGB', (10, 20), 'red').save(output, format='PNG')
    original = output.getvalue()
    upload = FileUpload.objects.create(
        project=project, user=project.created_by, file=ContentFile(original, name='image.png')
    )

    captured, identity, upload_id = capture_managed_upload(upload.url, project)
    assert captured == original and identity['width'] == 10 and identity['height'] == 20
    assert upload_id == upload.pk
    with pytest.raises(APIException):
        capture_managed_upload(upload.url, foreign)
    with pytest.raises(APIException):
        capture_managed_upload('https://example.invalid/image.png', project)
