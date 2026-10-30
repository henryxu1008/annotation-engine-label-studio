import { sha256 } from "@noble/hashes/sha256";
import { bytesToHex } from "@noble/hashes/utils";
// Receipt keys are fixed ASCII protocol names. Recurse through the input array
// to match the server's sorted-key JSON, including every revision identity pin.
const canonicalManifest = (v) =>
  Array.isArray(v)
    ? v.map(canonicalManifest)
    : v && typeof v === "object"
      ? Object.fromEntries(
          Object.keys(v)
            .sort()
            .map((key) => [key, canonicalManifest(v[key])]),
        )
      : v;
export const fieldDigest = (v) => bytesToHex(sha256(JSON.stringify(canonicalManifest(v))));
