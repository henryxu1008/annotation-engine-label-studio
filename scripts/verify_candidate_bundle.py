"""Verify a Docker/OCI offline archive before loading it; no registry access.

Usage: python verify_candidate_bundle.py IMAGE.tar candidate.json
The candidate manifest must arrive through the same approved release channel.
Checksums provide integrity comparison, not a signature or proof of provenance.
The admitted profile is the single linux/amd64 Docker-save hybrid archive:
OCI layout/index and Docker manifest must identify the same candidate. BuildKit
attestations linked to that platform are allowed; other images and aliases are not.
"""
import hashlib
import json
import posixpath
import sys
import tarfile
from pathlib import Path, PurePosixPath


def verify(archive, expected):
    with archive.open("rb") as stream:
        archive_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    if archive_hash != expected["archive_sha256"]:
        raise ValueError("Archive SHA-256 mismatch")
    with tarfile.open(archive) as contents:
        members = {m.name: m for m in contents.getmembers()}
        if len(members) != len(contents.getmembers()):
            raise ValueError("Duplicate archive entries")
        for name, member in members.items():
            if (name in {"", "."} or name.startswith("/") or "\\" in name
                    or ".." in PurePosixPath(name).parts or posixpath.normpath(name) != name):
                raise ValueError("Noncanonical archive entry: " + name)
            if not (member.isfile() or member.isdir()):
                raise ValueError("Archive links and special entries are not admitted: " + name)

        def metadata(name):
            if name not in members or not members[name].isfile():
                raise ValueError("Missing regular archive metadata: " + name)
            return json.load(contents.extractfile(members[name]))

        if metadata("oci-layout") != {"imageLayoutVersion": "1.0.0"}:
            raise ValueError("Unsupported OCI layout")
        verified = {}
        for name, member in members.items():
            if name.startswith("blobs/sha256/") and member.isfile():
                with contents.extractfile(member) as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if digest != name.removeprefix("blobs/sha256/"):
                    raise ValueError("OCI blob digest mismatch: " + name)
                verified["sha256:" + digest] = member.size

        def read(descriptor):
            digest = descriptor["digest"]
            if digest not in verified or verified[digest] != descriptor["size"]:
                raise ValueError("Missing blob or descriptor size mismatch: " + digest)
            return json.load(contents.extractfile("blobs/sha256/" + digest.split(":")[1]))

        index = metadata("index.json")
        roots = index["manifests"]
        if len(roots) != 1 or roots[0]["digest"] != expected["oci_index_digest"]:
            raise ValueError("Archive must contain exactly the admitted candidate root")
        root = roots[0]
        image_index = read(root)
        platforms = [d for d in image_index["manifests"]
                     if d.get("platform") == {"os": "linux", "architecture": "amd64"}]
        if len(platforms) != 1:
            raise ValueError("Candidate must have exactly one linux/amd64 platform")
        platform = platforms[0]
        if platform["digest"] != expected["platform_manifest_digest"]:
            raise ValueError("Platform manifest mismatch")
        for descriptor in image_index["manifests"]:
            if descriptor is platform:
                continue
            annotations = descriptor.get("annotations", {})
            if (descriptor.get("platform") != {"os": "unknown", "architecture": "unknown"}
                    or annotations.get("vnd.docker.reference.type") != "attestation-manifest"
                    or annotations.get("vnd.docker.reference.digest") != platform["digest"]):
                raise ValueError("Additional runnable or unrelated manifests are not admitted")
            attestation = read(descriptor)
            read(attestation["config"])
            for layer in attestation["layers"]:
                if verified.get(layer["digest"]) != layer["size"]:
                    raise ValueError("Missing or incorrectly sized attestation layer")
        manifest = read(platform)
        if manifest["config"]["digest"] != expected["image_config_digest"]:
            raise ValueError("Image config mismatch")
        config = read(manifest["config"])
        if config["os"] != "linux" or config["architecture"] != "amd64":
            raise ValueError("Unexpected image platform")
        labels = config["config"]["Labels"]
        for name, value in (("revision", expected["source_commit"]), ("version", expected["version"])):
            if labels["org.opencontainers.image." + name] != value:
                raise ValueError("OCI label mismatch: " + name)
        for layer in manifest["layers"]:
            if verified.get(layer["digest"]) != layer["size"]:
                raise ValueError("Missing or incorrectly sized layer")
        # Classic Docker consumes manifest.json; containerd prefers OCI layout.
        # Certify both consumption paths rather than one graph alongside another.
        docker_manifests = metadata("manifest.json")
        if not isinstance(docker_manifests, list) or len(docker_manifests) != 1:
            raise ValueError("Docker manifest must contain exactly one candidate")
        docker = docker_manifests[0]
        blob_path = lambda digest: "blobs/sha256/" + digest.removeprefix("sha256:")
        if docker["Config"] != blob_path(manifest["config"]["digest"]):
            raise ValueError("Docker and OCI config identities differ")
        if docker["Layers"] != [blob_path(layer["digest"]) for layer in manifest["layers"]]:
            raise ValueError("Docker and OCI layer identities/order differ")
        tags = docker.get("RepoTags") or []
        annotations = root.get("annotations", {})
        image_name = annotations.get("io.containerd.image.name")
        if image_name:
            aliases = {image_name}
            if image_name.startswith("docker.io/"):
                short_name = image_name.removeprefix("docker.io/")
                aliases.add(short_name)
                if short_name.startswith("library/"):
                    aliases.add(short_name.removeprefix("library/"))
            if len(tags) != 1 or tags[0] not in aliases:
                raise ValueError("Docker and OCI image tags differ")
            if annotations.get("org.opencontainers.image.ref.name", image_name.rsplit(":", 1)[-1]) != image_name.rsplit(":", 1)[-1]:
                raise ValueError("Conflicting OCI image alias")
        elif tags or annotations.get("org.opencontainers.image.ref.name"):
            raise ValueError("Named Docker or OCI alias lacks a shared image identity")
    return {"result": "PASS", "archive_sha256": archive_hash, "verified_blobs": len(verified),
            "oci_index_digest": root["digest"], "platform_manifest_digest": platform["digest"],
            "image_config_digest": manifest["config"]["digest"], "source_commit": expected["source_commit"]}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    try:
        print(json.dumps(verify(Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text(encoding="utf-8-sig"))), indent=2))
    except (ValueError, KeyError, StopIteration, tarfile.TarError) as error:
        raise SystemExit("Candidate verification failed: " + str(error))
