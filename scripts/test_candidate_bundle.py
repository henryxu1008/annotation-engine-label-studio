"""Single-candidate archive contracts; fixtures never enter a Docker daemon."""
import copy
import hashlib
import io
import json
import pathlib
import tarfile
import tempfile
import unittest

import verify_candidate_bundle as subject


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


def fixture():
    parts = {}

    def blob(value, media_type):
        raw = value if isinstance(value, bytes) else encoded(value)
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        parts["blobs/sha256/" + digest.split(":")[1]] = raw
        return {"digest": digest, "size": len(raw), "mediaType": media_type}

    layers = [blob(raw, "application/vnd.oci.image.layer.v1.tar") for raw in [b"layer-one", b"layer-two"]]
    config = {"os": "linux", "architecture": "amd64", "rootfs": {"type": "layers", "diff_ids": [d["digest"] for d in layers]},
              "config": {"Labels": {"org.opencontainers.image.revision": "a" * 40,
                                    "org.opencontainers.image.version": "1.23.0+fork.fixture"}}}
    config_descriptor = blob(config, "application/vnd.oci.image.config.v1+json")
    manifest = {"schemaVersion": 2, "config": config_descriptor, "layers": layers}
    platform = blob(manifest, "application/vnd.oci.image.manifest.v1+json")
    platform["platform"] = {"os": "linux", "architecture": "amd64"}
    image_index = {"schemaVersion": 2, "manifests": [platform]}
    root = blob(image_index, "application/vnd.oci.image.index.v1+json")
    root["annotations"] = {"io.containerd.image.name": "docker.io/library/ls-candidate:fixture",
                           "org.opencontainers.image.ref.name": "fixture"}
    parts["oci-layout"] = encoded({"imageLayoutVersion": "1.0.0"})
    parts["index.json"] = encoded({"schemaVersion": 2, "manifests": [root]})
    parts["manifest.json"] = encoded([{"Config": "blobs/sha256/" + config_descriptor["digest"].split(":")[1],
                                      "RepoTags": ["ls-candidate:fixture"],
                                      "Layers": ["blobs/sha256/" + d["digest"].split(":")[1] for d in layers]}])
    expected = {"source_commit": "a" * 40, "version": "1.23.0+fork.fixture", "oci_index_digest": root["digest"],
                "platform_manifest_digest": platform["digest"], "image_config_digest": config_descriptor["digest"]}
    return parts, expected


class ArchiveContracts(unittest.TestCase):
    def append_attestation(self, parts, expected):
        def blob(value, media_type):
            raw = value if isinstance(value, bytes) else encoded(value)
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
            parts["blobs/sha256/" + digest.split(":")[1]] = raw
            return {"digest": digest, "size": len(raw), "mediaType": media_type}

        config = blob({"os": "unknown", "architecture": "unknown", "rootfs": {"type": "layers", "diff_ids": []},
                       "config": {}}, "application/vnd.oci.image.config.v1+json")
        layer = blob(b"synthetic-attestation", "application/vnd.in-toto+json")
        attestation = blob({"schemaVersion": 2, "config": config, "layers": [layer]},
                           "application/vnd.oci.image.manifest.v1+json")
        attestation["platform"] = {"os": "unknown", "architecture": "unknown"}
        attestation["annotations"] = {"vnd.docker.reference.type": "attestation-manifest",
                                      "vnd.docker.reference.digest": expected["platform_manifest_digest"]}
        root_index = json.loads(parts["index.json"])
        root = root_index["manifests"][0]
        image_index = json.loads(parts["blobs/sha256/" + root["digest"].split(":")[1]])
        image_index["manifests"].append(attestation)
        new_root = blob(image_index, "application/vnd.oci.image.index.v1+json")
        root["digest"], root["size"] = new_root["digest"], new_root["size"]
        expected["oci_index_digest"] = new_root["digest"]
        parts["index.json"] = encoded(root_index)
        return "blobs/sha256/" + layer["digest"].split(":")[1]

    def append_platform(self, parts, expected, architecture):
        root_index = json.loads(parts["index.json"])
        root = root_index["manifests"][0]
        image_index = json.loads(parts["blobs/sha256/" + root["digest"].split(":")[1]])
        extra = copy.deepcopy(image_index["manifests"][0])
        extra["platform"]["architecture"] = architecture
        image_index["manifests"].append(extra)
        raw = encoded(image_index)
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        parts["blobs/sha256/" + digest.split(":")[1]] = raw
        root["digest"], root["size"] = digest, len(raw)
        expected["oci_index_digest"] = digest
        parts["index.json"] = encoded(root_index)

    def check(self, parts, expected):
        with tempfile.TemporaryDirectory(prefix="ls76-archive-test-") as directory:
            root = pathlib.Path(directory).resolve()
            self.assertTrue(root.is_relative_to(pathlib.Path(tempfile.gettempdir()).resolve()))
            archive = root / "candidate.tar"
            with tarfile.open(archive, "w") as output:
                for name, raw in parts.items():
                    item = tarfile.TarInfo(name)
                    item.size = len(raw)
                    output.addfile(item, io.BytesIO(raw))
            expected = dict(expected, archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
            return subject.verify(archive, expected)

    def test_consistent_docker_and_oci_candidate(self):
        self.assertEqual(self.check(*fixture())["result"], "PASS")

    def test_linked_buildkit_attestation_is_accepted(self):
        parts, expected = fixture()
        self.append_attestation(parts, expected)
        self.assertEqual(self.check(parts, expected)["result"], "PASS")

    def test_missing_linked_attestation_layer_is_rejected(self):
        parts, expected = fixture()
        layer_path = self.append_attestation(parts, expected)
        del parts[layer_path]
        with self.assertRaises(ValueError):
            self.check(parts, expected)

    def test_missing_layout_cannot_select_a_different_docker_config(self):
        parts, expected = fixture()
        del parts["oci-layout"]
        config = json.loads(parts["blobs/sha256/" + expected["image_config_digest"].split(":")[1]])
        config["config"]["Labels"]["org.opencontainers.image.revision"] = "b" * 40
        raw = encoded(config)
        other_path = "blobs/sha256/" + hashlib.sha256(raw).hexdigest()
        parts[other_path] = raw
        docker = json.loads(parts["manifest.json"])
        docker[0]["Config"] = other_path
        parts["manifest.json"] = encoded(docker)
        with self.assertRaises(ValueError):
            self.check(parts, expected)

    def test_invalid_layout_version(self):
        parts, expected = fixture()
        parts["oci-layout"] = encoded({"imageLayoutVersion": "2.0.0"})
        with self.assertRaises(ValueError):
            self.check(parts, expected)

    def test_docker_config_must_match_even_when_oci_layout_is_present(self):
        parts, expected = fixture()
        docker = json.loads(parts["manifest.json"])
        raw = encoded({"os": "linux", "architecture": "amd64", "config": {"Labels": {}}})
        name = "blobs/sha256/" + hashlib.sha256(raw).hexdigest()
        parts[name] = raw
        docker[0]["Config"] = name
        parts["manifest.json"] = encoded(docker)
        with self.assertRaises(ValueError):
            self.check(parts, expected)

    def test_docker_layer_order_must_match(self):
        parts, expected = fixture()
        docker = json.loads(parts["manifest.json"])
        docker[0]["Layers"].reverse()
        parts["manifest.json"] = encoded(docker)
        with self.assertRaises(ValueError):
            self.check(parts, expected)

    def test_additional_docker_image_is_rejected(self):
        parts, expected = fixture()
        docker = json.loads(parts["manifest.json"])
        docker.append(copy.deepcopy(docker[0]))
        parts["manifest.json"] = encoded(docker)
        with self.assertRaises(ValueError):
            self.check(parts, expected)

    def test_additional_oci_root_is_rejected(self):
        parts, expected = fixture()
        index = json.loads(parts["index.json"])
        index["manifests"].append(copy.deepcopy(index["manifests"][0]))
        parts["index.json"] = encoded(index)
        with self.assertRaises(ValueError):
            self.check(parts, expected)

    def test_duplicate_candidate_platform_is_rejected(self):
        parts, expected = fixture()
        self.append_platform(parts, expected, "amd64")
        with self.assertRaises(ValueError):
            self.check(parts, expected)

    def test_additional_runnable_platform_is_rejected(self):
        parts, expected = fixture()
        self.append_platform(parts, expected, "arm64")
        with self.assertRaises(ValueError):
            self.check(parts, expected)

    def test_docker_tag_must_match_oci_name(self):
        parts, expected = fixture()
        docker = json.loads(parts["manifest.json"])
        docker[0]["RepoTags"] = ["unrelated-image:latest"]
        parts["manifest.json"] = encoded(docker)
        with self.assertRaises(ValueError):
            self.check(parts, expected)

    def test_additional_docker_tag_is_rejected(self):
        parts, expected = fixture()
        docker = json.loads(parts["manifest.json"])
        docker[0]["RepoTags"].append("unrelated-image:latest")
        parts["manifest.json"] = encoded(docker)
        with self.assertRaises(ValueError):
            self.check(parts, expected)

    def test_conflicting_oci_alias_is_rejected(self):
        parts, expected = fixture()
        index = json.loads(parts["index.json"])
        index["manifests"][0]["annotations"]["org.opencontainers.image.ref.name"] = "other"
        parts["index.json"] = encoded(index)
        with self.assertRaises(ValueError):
            self.check(parts, expected)

    def test_path_alias_cannot_hide_another_index(self):
        parts, expected = fixture()
        parts["./index.json"] = parts["index.json"]
        with self.assertRaises(ValueError):
            self.check(parts, expected)

    def test_leading_parent_directory_is_rejected(self):
        parts, expected = fixture()
        parts["../payload"] = b"synthetic-extra"
        with self.assertRaises(ValueError):
            self.check(parts, expected)

    def test_parent_directory_entry_is_rejected(self):
        parts, expected = fixture()
        parts[".."] = b"synthetic-extra"
        with self.assertRaises(ValueError):
            self.check(parts, expected)

    def test_current_directory_entry_is_rejected(self):
        parts, expected = fixture()
        parts["."] = b"synthetic-extra"
        with self.assertRaises(ValueError):
            self.check(parts, expected)

    def test_docker_manifest_is_required_for_the_hybrid_profile(self):
        parts, expected = fixture()
        del parts["manifest.json"]
        with self.assertRaises(ValueError):
            self.check(parts, expected)


if __name__ == "__main__":
    unittest.main()
