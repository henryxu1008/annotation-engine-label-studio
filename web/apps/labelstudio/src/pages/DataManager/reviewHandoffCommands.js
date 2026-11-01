import { sha256 } from "@noble/hashes/sha256";
import { bytesToHex } from "@noble/hashes/utils";

const canonical = (value) =>
  value && typeof value === "object" && !Array.isArray(value)
    ? Object.fromEntries(
        Object.keys(value)
          .sort()
          .map((key) => [key, canonical(value[key])]),
      )
    : value;

export const handoffCommandHash = (schema, projectId, source, body) =>
  bytesToHex(
    sha256(
      JSON.stringify(
        canonical({
          schema,
          action: "handoff",
          project_id: Number(projectId),
          batch_id: source.batch,
          source_id: source.source,
          body,
        }),
      ),
    ),
  );
