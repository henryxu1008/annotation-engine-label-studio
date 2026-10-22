"""Contract receiver fixture only. No Host DB, API, credentials or live Core calls."""

import hashlib
import json
import uuid

import rfc8785
from data_export.release_manifest import media_fields, validator


def sha(blob):
    return hashlib.sha256(blob).hexdigest()


class ContractRejected(Exception):
    pass


def validate_media_membership(manifest, records):
    """Bind all three representations before a synthetic Host commit."""
    expected, members = {}, {}
    for item, row in zip(manifest["content"]["items"], records, strict=True):
        try:
            fields = media_fields(row["result_snapshot"]["project"]["label_config"])
        except ValueError as exc:
            raise ContractRejected("SOURCE_PROTOCOL_CONFLICT") from exc
        sid = item["source"]["id"]
        for field in fields:
            expected[sid, field] = row["result_snapshot"]["task"]["data"].get(field)
        if len(item["media"]) != len(fields) or {p["field"] for p in item["media"]} != set(fields):
            raise ContractRejected("MEDIA_MEMBERSHIP_CONFLICT")
        for pin in item["media"]:
            members[sid, pin["field"]] = pin
    actual, blobs, uploads = {}, {}, {}
    for pin in manifest["media"]:
        key = pin["submission_id"], pin["field"]
        member = {k: pin[k] for k in ("field", "reference", "sha256", "size_bytes", "upload_id")}
        if (
            key in actual
            or key not in expected
            or pin["reference"] != expected[key]
            or member != members.get(key)
        ):
            raise ContractRejected("MEDIA_MEMBERSHIP_CONFLICT")
        actual[key] = member
        metadata = (pin["sha256"], pin["size_bytes"], pin["upload_id"])
        upload = (pin["blob_id"], pin["sha256"], pin["size_bytes"])
        if blobs.get(pin["blob_id"], metadata) != metadata or uploads.get(pin["upload_id"], upload) != upload:
            raise ContractRejected("MEDIA_MEMBERSHIP_CONFLICT")
        blobs[pin["blob_id"]], uploads[pin["upload_id"]] = metadata, upload
    profile = "managed-upload-capture/v1" if expected else "no-media/v1"
    if set(actual) != set(expected) or manifest["content"]["media_profile"] != profile:
        raise ContractRejected("MEDIA_MEMBERSHIP_CONFLICT")


class SyntheticHost:
    def __init__(self, namespace, project_id, connection="synthetic-workspace-connection"):
        self.namespace, self.project_id, self.connection = namespace, str(project_id), connection
        self.operations, self.bindings, self.sources, self.objects = {}, {}, {}, {}
        self.allowed = True
        self.requires_submission_media_identity = False

    def accept(
        self, key, expected_sha, fetch_manifest, fetch_asset, *, target="synthetic-dataset", lose_response=False
    ):
        if not self.allowed:
            raise ContractRejected("CURRENT_AUTHORITY_DENIED")
        # Even a logical replay must revalidate the authorized source and exact bytes.
        blob = fetch_manifest()
        if sha(blob) != expected_sha:
            raise ContractRejected("MANIFEST_HASH_MISMATCH")
        manifest = json.loads(blob)
        validator().validate(manifest)
        if rfc8785.dumps(manifest) != blob or manifest["origin_mode"] != "fork_approved":
            raise ContractRejected("SOURCE_PROTOCOL_CONFLICT")
        content = manifest["content"]
        if manifest["media"] and self.requires_submission_media_identity:
            raise ContractRejected("MEDIA_PROVENANCE_UNSUPPORTED")
        if content["source_namespace"] != self.namespace or content["project_id"] != self.project_id:
            raise ContractRejected("SOURCE_SCOPE_MISMATCH")
        if (
            content["native_mapping_version"] != "submission-envelope/v1"
            or manifest["access_policy"]["source_namespace"] != self.namespace
        ):
            raise ContractRejected("SOURCE_PROTOCOL_CONFLICT")
        if sha(rfc8785.dumps(content)) != manifest["content_digest"]:
            raise ContractRejected("CONTENT_HASH_MISMATCH")
        fingerprint = (target, expected_sha, manifest["content_digest"])
        if key in self.operations and self.operations[key]["fingerprint"] != fingerprint:
            raise ContractRejected("IDEMPOTENCY_CONFLICT")
        items = content["items"]
        if [int(i["source"]["id"]) for i in items] != sorted({int(i["source"]["id"]) for i in items}) or len(
            {i["assignment_id"] for i in items}
        ) != len(items):
            raise ContractRejected("MEMBERSHIP_CONFLICT")
        expected_formats = {"JSON": "records.json", "JSONL": "records.jsonl"}
        if content["output"]["format"] == "CONLL2003":
            expected_formats["CONLL2003"] = "conll2003.zip"
        artifacts = manifest["artifacts"]
        if (
            len(artifacts) != len(expected_formats)
            or {a["format"] for a in artifacts} != set(expected_formats)
            or len({a["id"] for a in artifacts}) != len(artifacts)
            or {a["id"] for a in artifacts} & {p["blob_id"] for p in manifest["media"]}
            or any(a["name"] != expected_formats[a["format"]] or a["record_count"] != len(items) for a in artifacts)
        ):
            raise ContractRejected("ARTIFACT_MEMBERSHIP_CONFLICT")
        payloads = {}
        for asset in manifest["artifacts"] + manifest["media"]:
            identity = asset.get("id", asset.get("blob_id"))
            payload = fetch_asset(identity)
            if sha(payload) != asset["sha256"] or len(payload) != int(asset["size_bytes"]):
                raise ContractRejected("PAYLOAD_INTEGRITY_FAILED")
            payloads[identity] = payload
        native = next(a for a in manifest["artifacts"] if a["format"] == "JSONL")
        records = [json.loads(line) for line in payloads[native["id"]].splitlines()]
        if len(records) != len(items):
            raise ContractRejected("MEMBERSHIP_CONFLICT")
        native_json = next(a for a in artifacts if a["format"] == "JSON")
        legacy_options = dict(sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if json.dumps(json.loads(payloads[native_json["id"]]), **legacy_options) != json.dumps(records, **legacy_options):
            raise ContractRejected("NATIVE_ARTIFACT_CONFLICT")
        source_updates = {}
        for item, row in zip(items, records):
            legacy = json.dumps(
                row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode()
            raw_snapshot = json.dumps(
                row["result_snapshot"], sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode()
            xml = row["result_snapshot"]["project"]["label_config"].encode()
            if (
                sha(raw_snapshot) != row["result_hash"]
                or sha(xml) != content["label_config_sha256"]
                or sha(xml) != content["schema_sha256"]
                or item["project_id"] != content["project_id"]
                or str(row["result_snapshot"]["project"]["id"]) != content["project_id"]
                or str(row["result_snapshot"]["task"]["id"]) != item["task_id"]
                or row["source"]["namespace"] != self.namespace
                or item["source"]["namespace"] != self.namespace
                or row["source"]["kind"] != "submission"
                or row["review"]["decision"] != "approved"
            ):
                raise ContractRejected("SOURCE_FINGERPRINT_CONFLICT")
            if (
                str(row["source"]["id"]),
                str(row["project_id"]),
                str(row["task_id"]),
                str(row["assignment_id"]),
                str(row["revision"]),
                row["result_hash"],
                sha(legacy),
            ) != (
                item["source"]["id"],
                content["project_id"],
                item["task_id"],
                item["assignment_id"],
                item["revision"],
                item["result_hash"],
                item["record_sha256"],
            ):
                raise ContractRejected("SOURCE_FINGERPRINT_CONFLICT")
            source_key = (self.connection, self.namespace, "IMMUTABLE_SUBMISSION", item["source"]["id"])
            if source_key in self.sources and self.sources[source_key] != item["record_sha256"]:
                raise ContractRejected("SOURCE_IDENTITY_CONFLICT")
            source_updates[source_key] = item["record_sha256"]
        validate_media_membership(manifest, records)
        # Synthetic local atomic commit; these fields are NOT a claim about Host implementation.
        mapping_key = (target, self.connection, manifest["snapshot_id"], expected_sha)
        if (key in self.operations or mapping_key in self.bindings) and any(
            self.objects.get(identity) != payload for identity, payload in payloads.items()
        ):
            raise ContractRejected("HOST_COPY_INTEGRITY_FAILED")
        if key not in self.operations:
            mapping = self.bindings.get(mapping_key) or str(uuid.uuid4())
            self.bindings[mapping_key] = mapping
            self.operations[key] = {
                "operation_id": str(uuid.uuid4()),
                "fingerprint": fingerprint,
                "dataset_version_id": mapping,
                "state": "ACCEPTED_SOURCE",
                "gold_certified": False,
                "published": False,
            }
            self.sources.update(source_updates)
            self.objects.update(payloads)
        if lose_response:
            raise TimeoutError("Synthetic committed receipt response lost; query the original operation")
        return self.operations[key]

    def lookup(self, key):
        if not self.allowed:
            raise ContractRejected("CURRENT_AUTHORITY_DENIED")
        return self.operations.get(key, {"state": "UNKNOWN"})
