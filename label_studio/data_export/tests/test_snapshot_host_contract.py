import json
from pathlib import Path

import pytest
import rfc8785
from data_export.tests import test_release_snapshots as fixtures
from data_export.tests.synthetic_host import ContractRejected, SyntheticHost, sha
from django.test import override_settings
from jsonschema import ValidationError
from rest_framework.test import APITestCase


class TestSnapshotHostContract(APITestCase):
    setUpTestData = classmethod(fixtures.TestReleaseSnapshots.setUpTestData.__func__)
    setUp = fixtures.TestReleaseSnapshots.setUp
    body = fixtures.TestReleaseSnapshots.body
    create = fixtures.TestReleaseSnapshots.create
    frozen = fixtures.TestReleaseSnapshots.frozen
    generate = fixtures.TestReleaseSnapshots.generate
    capture = fixtures.TestReleaseSnapshots.capture
    snapshot_url = fixtures.TestReleaseSnapshots.snapshot_url
    frozen_from = fixtures.TestReleaseSnapshots.frozen_from
    make_source = fixtures.TestReleaseSnapshots.make_source
    media_snapshot = fixtures.TestReleaseSnapshots.media_snapshot

    def fixture(self, snapshot=None):
        snapshot = snapshot or self.capture()
        url = self.snapshot_url(snapshot.release, snapshot)

        def manifest():
            response = self.client.get(url + "manifest/")
            if response.status_code != 200:
                raise ContractRejected("SOURCE_ACCESS_DENIED")
            return response.content

        def asset(identity):
            response = self.client.get(url + f"assets/{identity}/download/")
            if response.status_code != 200:
                raise ContractRejected("SOURCE_ACCESS_DENIED")
            result = b"".join(response.streaming_content)
            for closer in response._resource_closers:
                closer()
            return result

        host = SyntheticHost(self.policy["source_namespace"], self.project.pk)
        return snapshot, host, manifest, asset

    def test_acceptance_mapping_replay_and_lost_response(self):
        snapshot, host, manifest, asset = self.fixture()
        with pytest.raises(TimeoutError):
            host.accept("operation", snapshot.manifest_sha256, manifest, asset, lose_response=True)
        result = host.lookup("operation")
        assert result["state"] == "ACCEPTED_SOURCE"
        assert not result["gold_certified"] and not result["published"]
        assert host.accept("operation", snapshot.manifest_sha256, manifest, asset) == result
        assert len(host.operations) == len(host.bindings) == 1
        assert host.lookup("never-sent")["state"] == "UNKNOWN"
        with pytest.raises(ContractRejected, match="IDEMPOTENCY_CONFLICT"):
            host.accept("operation", snapshot.manifest_sha256, manifest, asset, target="other-dataset")

    def test_hash_byte_integrity_and_namespace_fail_closed(self):
        snapshot, host, manifest, asset = self.fixture()
        with pytest.raises(ContractRejected, match="MANIFEST_HASH_MISMATCH"):
            host.accept("bad-hash", "0" * 64, manifest, asset)
        with pytest.raises(ContractRejected, match="PAYLOAD_INTEGRITY_FAILED"):
            host.accept("bad-payload", snapshot.manifest_sha256, manifest, lambda _: b"forged")
        host.namespace = "00000000-0000-4000-8000-000000000080"
        with pytest.raises(ContractRejected, match="SOURCE_SCOPE_MISMATCH"):
            host.accept("wrong-connection", snapshot.manifest_sha256, manifest, asset)
        assert not host.operations

    def test_replay_checks_both_host_authority_and_source_revocation(self):
        snapshot, host, manifest, asset = self.fixture()
        host.accept("operation", snapshot.manifest_sha256, manifest, asset)
        host.allowed = False
        with pytest.raises(ContractRejected, match="CURRENT_AUTHORITY_DENIED"):
            host.accept("operation", snapshot.manifest_sha256, manifest, asset)
        host.allowed = True
        self.manager.is_active = False
        self.manager.save(update_fields=["is_active"], session_actor=self.admin)
        with pytest.raises(ContractRejected, match="SOURCE_ACCESS_DENIED"):
            host.accept("operation", snapshot.manifest_sha256, manifest, asset)
        assert len(host.objects) == 2  # Historical copy retained, no implicit deletion/recall.

    def test_core_decision_cannot_turn_internal_read_into_fork_approval(self):
        snapshot, host, manifest, asset = self.fixture()
        changed = json.loads(manifest())
        changed["origin_mode"] = "core_only_internal"
        blob = rfc8785.dumps(changed)
        with pytest.raises(ValidationError):
            host.accept("forged", sha(blob), lambda: blob, asset)
        changed = json.loads(manifest())
        changed["core_review_decision"] = "approved"
        blob = rfc8785.dumps(changed)
        with pytest.raises(ValidationError):
            host.accept("forged", sha(blob), lambda: blob, asset)
        assert not host.bindings

    def test_same_submission_cannot_hide_fingerprint_conflict_behind_new_version(self):
        snapshot, host, manifest, asset = self.fixture()
        host.accept("operation", snapshot.manifest_sha256, manifest, asset)
        source_key = next(iter(host.sources))
        host.sources[source_key] = "0" * 64
        with pytest.raises(ContractRejected, match="SOURCE_IDENTITY_CONFLICT"):
            host.accept("new-operation", snapshot.manifest_sha256, manifest, asset)
        assert len(host.operations) == 1

    def test_managed_media_handoff_preserves_bytes_and_declares_provenance_limit(self):
        with override_settings(MEDIA_ROOT=self.root.name + "/uploads"):
            snapshot, upload = self.media_snapshot()
            snapshot, host, manifest, asset = self.fixture(snapshot)
            Path(upload.file.path).unlink()
            result = host.accept("media", snapshot.manifest_sha256, manifest, asset)
            pin = json.loads(manifest())["media"][0]
            assert sha(host.objects[pin["blob_id"]]) == pin["sha256"]
            assert result["state"] == "ACCEPTED_SOURCE" and not result["gold_certified"]
            host.requires_submission_media_identity = True
            with pytest.raises(ContractRejected, match="MEDIA_PROVENANCE_UNSUPPORTED"):
                host.accept("stronger-proof", snapshot.manifest_sha256, manifest, asset)

    def test_replay_does_not_silently_repair_a_corrupt_host_copy(self):
        snapshot, host, manifest, asset = self.fixture()
        host.accept("operation", snapshot.manifest_sha256, manifest, asset)
        host.objects[next(iter(host.objects))] = b"corrupted host copy"
        with pytest.raises(ContractRejected, match="HOST_COPY_INTEGRITY_FAILED"):
            host.accept("operation", snapshot.manifest_sha256, manifest, asset)

    def test_actual_conll_converter_artifact_and_provenance_survive_handoff(self):
        snapshot = self.capture(self.frozen(self.body(fmt="CONLL2003")))
        assert snapshot.state == "ready", snapshot.error_code
        snapshot, host, manifest, asset = self.fixture(snapshot)
        document = json.loads(manifest())
        assert {a["format"] for a in document["artifacts"]} == {"JSON", "JSONL", "CONLL2003"}
        assert document["content"]["runtime"]["sdk_version"] == "2.0.18"
        result = host.accept("standard-format", snapshot.manifest_sha256, manifest, asset)
        assert result["state"] == "ACCEPTED_SOURCE" and len(host.objects) == 3
