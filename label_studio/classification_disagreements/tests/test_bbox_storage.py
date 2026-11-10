from hashlib import sha256
from uuid import uuid4

import pytest
from classification_disagreements import bbox_storage
from core.utils.private_file import PrivateFileError


@pytest.fixture
def trusted_root(settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = str(tmp_path / 'public')
    settings.STATIC_ROOT = str(tmp_path / 'static')
    settings.APPROVED_EXPORT_ROOT = str(tmp_path / 'private')
    audit_root = tmp_path / 'private' / 'audit-packages'
    audit_root.mkdir(parents=True)
    # Isolate bbox behavior from host-specific ancestor ACLs, already tested by
    # audit_files. The returned path stands in for its validated private root.
    monkeypatch.setattr(bbox_storage.audit_files, 'private_root', lambda: audit_root)


def test_private_artifact_checks_namespace_and_contents(trusted_root):
    token = uuid4()
    content = b'private image bytes'
    key, namespace = bbox_storage.publish(token, content)
    assert key == str(token)
    with bbox_storage.open_verified(token, namespace, sha256(content).hexdigest(), len(content)) as stream:
        assert stream.read() == content
    with pytest.raises(PrivateFileError):
        with bbox_storage.open_verified(token, '0' * 64, sha256(content).hexdigest(), len(content)):
            pass
    (bbox_storage.root() / key).write_bytes(b'changed')
    with pytest.raises(PrivateFileError):
        with bbox_storage.open_verified(token, namespace, sha256(content).hexdigest(), len(content)):
            pass


def test_candidate_cannot_be_replaced(trusted_root):
    token = uuid4()
    bbox_storage.publish(token, b'first')
    with pytest.raises(FileExistsError):
        bbox_storage.publish(token, b'second')
