"""Tests for capture only; source authority needs separate integration tests."""

from io import BytesIO
from random import Random
from types import SimpleNamespace

import pytest
from classification_disagreements import bbox, bbox_media
from PIL import Image


class Stream:
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False

    def iter_chunks(self, chunk_size):
        yield from self.chunks

    def close(self):
        self.closed = True


def image():
    output = BytesIO()
    Image.new('RGB', (4, 5)).save(output, format='PNG')
    return output.getvalue()


def test_complete_image_is_captured_and_closed():
    data = image()
    stream = Stream([data[:10], data[10:]])
    captured, identity = bbox_media.capture_stream(stream, {'ContentLength': len(data), 'StatusCode': 200})
    assert captured == data and identity['width'] == 4 and identity['height'] == 5
    assert stream.closed


@pytest.mark.parametrize(('chunks', 'metadata'), [
    ([b'ab'], {'ContentLength': 3, 'StatusCode': 200}),
    ([b'abcd'], {'ContentLength': 3, 'StatusCode': 200}),
    ([b'abc'], {'ContentLength': 3, 'StatusCode': 206}),
    ([b'abc'], {'ContentLength': 3, 'StatusCode': 200, 'ContentRange': 'bytes 0-2/3'}),
    ([b'abc'], {'ContentLength': True, 'StatusCode': 200}),
    (['abc'], {'ContentLength': 3, 'StatusCode': 200}),
])
def test_incomplete_partial_or_malformed_stream_is_rejected_and_closed(chunks, metadata):
    stream = Stream(chunks)
    with pytest.raises(bbox.Incomparable):
        bbox_media.capture_stream(stream, metadata)
    assert stream.closed


def test_over_budget_does_not_read_and_closes():
    stream = Stream([b'unused'])
    with pytest.raises(bbox.Error, match='BBOX_TOO_LARGE'):
        bbox_media.capture_stream(stream, {'ContentLength': bbox.MAX_IMAGE_BYTES + 1, 'StatusCode': 200})
    assert stream.closed


def test_read_failure_is_unavailable_and_closes():
    class Broken(Stream):
        def iter_chunks(self, chunk_size):
            raise OSError('source disconnected')

    stream = Broken([])
    with pytest.raises(bbox.Incomparable, match='unverified_image'):
        bbox_media.capture_stream(stream, {'ContentLength': 10, 'StatusCode': 200})
    assert stream.closed


class Blob:
    def __init__(self, data):
        self.data, self.generation, self.size = data, 123, len(data)
        self.reader = BytesIO(data)

    def reload(self, *, timeout, retry):
        assert timeout == 30 and retry is None

    def open(self, mode, **kwargs):
        assert mode == 'rb' and kwargs['if_generation_match'] == 123
        assert kwargs['retry'] is None
        return self.reader


def test_gcs_capture_pins_and_returns_generation():
    data = image()
    blob = Blob(data)
    captured, identity, generation = bbox_media.capture_gcs(blob)
    assert captured == data and generation == 123 and identity['width'] == 4
    assert blob.reader.closed


def test_gcs_generation_change_does_not_fall_back():
    class ChangedBlob(Blob):
        def open(self, mode, **kwargs):
            assert kwargs['if_generation_match'] == 123
            raise OSError('generation precondition failed')

    with pytest.raises(bbox.Incomparable, match='unverified_image'):
        bbox_media.capture_gcs(ChangedBlob(image()))


def test_gcs_sdk_reader_applies_generation_to_each_download(monkeypatch):
    from google.cloud import storage

    output = BytesIO()
    Image.frombytes('RGB', (400, 400), Random(81).randbytes(400 * 400 * 3)).save(output, format='PNG')
    data = output.getvalue()
    client = storage.Client.create_anonymous_client()
    blob = client.bucket('test-bucket').blob('image.png')
    requests = []

    def reload(**kwargs):
        blob._properties = {'generation': '123', 'size': str(len(data))}

    def download_as_bytes(*, start, end, **kwargs):
        requests.append(kwargs)
        return data[start:end + 1] if end is not None else data[start:]

    monkeypatch.setattr(blob, 'reload', reload)
    monkeypatch.setattr(blob, 'download_as_bytes', download_as_bytes)
    captured, _, generation = bbox_media.capture_gcs(blob)
    assert captured == data and generation == 123
    assert len(requests) >= 2 and all(request['if_generation_match'] == 123 for request in requests)


def test_gcs_sdk_generation_failure_midstream_does_not_return_partial_data(monkeypatch):
    from google.api_core.exceptions import PreconditionFailed
    from google.cloud import storage

    blob = storage.Client.create_anonymous_client().bucket('test-bucket').blob('image.png')
    calls = []

    def reload(**kwargs):
        blob._properties = {'generation': '123', 'size': str(300_000)}

    def download_as_bytes(*, start, end, **kwargs):
        calls.append(kwargs['if_generation_match'])
        if len(calls) > 1:
            raise PreconditionFailed('generation changed')
        return b'x' * (end - start + 1)

    monkeypatch.setattr(blob, 'reload', reload)
    monkeypatch.setattr(blob, 'download_as_bytes', download_as_bytes)
    with pytest.raises(bbox.Incomparable, match='unverified_image'):
        bbox_media.capture_gcs(blob)
    assert calls == [123, 123]


def test_managed_capture_returns_the_verified_owned_upload(monkeypatch, tmp_path):
    from data_export import snapshots

    data = image()
    source = tmp_path / 'upload.png'
    source.write_bytes(data)
    project = object()

    def owned_upload(reference, selected_project):
        assert reference == '/data/upload/image.png' and selected_project is project
        return SimpleNamespace(pk=42), source

    monkeypatch.setattr(snapshots, 'upload_for', owned_upload)
    captured, identity, source_id = bbox_media.capture_managed_upload('/data/upload/image.png', project)
    assert captured == data and source_id == 42 and identity['width'] == 4


def test_managed_capture_rejects_file_over_budget(monkeypatch, tmp_path):
    from data_export import snapshots

    source = tmp_path / 'upload.png'
    source.write_bytes(b'123')
    monkeypatch.setattr(snapshots, 'upload_for', lambda *_: (SimpleNamespace(pk=42), source))
    monkeypatch.setattr(bbox, 'MAX_IMAGE_BYTES', 2)
    with pytest.raises(bbox.Error, match='BBOX_TOO_LARGE'):
        bbox_media.capture_managed_upload('/data/upload/image.png', object())
