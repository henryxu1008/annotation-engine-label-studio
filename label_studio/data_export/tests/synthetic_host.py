"""Contract receiver fixture only. No Host DB, API, credentials or live Core calls."""

import hashlib
import json
import uuid

import rfc8785
from data_export.release_manifest import validator


def sha(blob):
    return hashlib.sha256(blob).hexdigest()


class ContractRejected(Exception):
    pass


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
        # Synthetic local atomic commit; these fields are NOT a claim about Host implementation.
        if key in self.operations and any(
            self.objects.get(identity) != payload for identity, payload in payloads.items()
        ):
            raise ContractRejected("HOST_COPY_INTEGRITY_FAILED")
        if key not in self.operations:
            mapping_key = (target, self.connection, manifest["snapshot_id"], expected_sha)
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
