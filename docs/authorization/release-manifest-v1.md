# ReleaseManifest v1 and reproducible source snapshots (#80)

Design confirmed by the user on 2026-10-22. Implemented in an isolated worktree
from `main@c2758fed05665c1258e5cf50ea63db809eaca9b5` (#79 / PR #98).
This is a Fork source-evidence feature. No Host implementation, deployment or
Core integration is authorized or certified by this document.

## Ownership and existing gaps

| Owner | Contract |
| --- | --- |
| #79 | Approved exact Submission/revision/hash set, historical review evidence, native/Converter artifacts and `fork.approved-export-manifest/v1` |
| #80 | A separately frozen `ReleaseManifest/v1`, content identity, managed media copies, authenticated reads, exact differences and synthetic Host handoff tests |
| Host | Workspace-owned connection, source registration, business DatasetVersion identity/number, typed source/production bindings, current rights, quality, certification and publication |
| Core-only-review | Ordinary authorized Submission acquisition followed by independent Core acceptance/review; no Fork approval or approved export access |

The Host main inspected for the design was
`3e1b0639a8fc2616fafed77df095865ec2ed35a7`. Its reference adapter still fetches
ordinary annotations; its controlled-Fork contract explicitly records incomplete
source-binding/replay/snapshot-integrity enforcement. Do not substitute the generic
DatasetVersion upload endpoint for verified source acceptance, or reuse the
historical official-CE Pilot PASS as controlled-Fork integration evidence.

DatasetVersion and Gold are not Fork models. In particular a READY source snapshot
does not create a business version, choose a Core reviewer, certify Gold or publish
anything. Host IDs are never accepted by these Fork endpoints or inserted back
into the immutable Manifest.

## Reuse check

Reuse #79 permission/identity/authority locks, frozen rows, artifact validation,
private root, RQ queue, audit table, range reader, existing FileUpload storage and
existing client download helpers. There is no new queue, converter, IAM, governance
platform, Host database dependency or third-party network collector.

New cross-language envelope encoding uses RFC 8785/JCS through `rfc8785==0.1.4`
(Trail of Bits, Apache-2.0, small dependency with no service/operations component).
JSON Schema validation reuses the already locked `jsonschema==4.23.0`, now an
explicit direct dependency. Do not implement a second handwritten JCS serializer.
The old Python snapshot/export encoding remains unchanged and is carried by name
and exact native artifact bytes; JCS must not round its large integers or floats.

## API and authority

The base path is
`/api/projects/{project}/approved-dataset-releases/{release}/snapshots/`.

| Method and suffix | Behavior |
| --- | --- |
| POST base, body `{}`, `Idempotency-Key` | Create one capture request for this exact READY #79 receipt |
| GET base, optional signed `cursor` | Descending created-time/UUID history, 50 records and explicit continuation |
| GET `{snapshot}/` | State, error, hashes and verified READY asset metadata |
| POST `{snapshot}/retry/`, body `{}` | Resume dispatch; reclaim an expired unstarted queued claim; or fence an interrupted running capture as failed without recapturing |
| GET `{snapshot}/manifest/` | Original JCS bytes; detached `X-Manifest-SHA256` |
| GET `{snapshot}/assets/{asset}/download/` | Native/standard #79 artifact or pinned media; per-request authenticated ranges |
| GET `{snapshot}/diff/?against={other}&offset=0` | Exact A-to-B counts and at most 50 changed members; `next_offset` |

Every route/method inherits the Manager/project/organization/active-account gate
and explicit `fork_approved` policy. Unknown/missing policy, disabled mode and
`core_only_internal` deny, including old URLs, HEAD and OPTIONS. Every content
read verifies both the original #79 creator and the #80 capture creator's live
authority, the policy and complete source/artifact graph. Actor/reviewer/mode,
snapshots, media paths, Host IDs and Core decisions are not request inputs.

Idempotency is project + authenticated actor + key. Same receipt/key returns the
original snapshot; changing receipt under that key is `IDEMPOTENCY_CONFLICT`.
Concurrent creation shares the Project fence and database unique constraint.
History cursors bind project, receipt and principal. Diff authorization checks
both snapshots before returning counts or membership. No bulk or generic LocalFiles
download path is added; the existing private-root reservation protects the new
`snapshots/` subtree as well.

This does not retrofit the separately governed legacy single-Submission release
endpoint or certify a mixed review workflow. Core adapters must not fall back to
that endpoint, mutable exports or annotations when ordinary Submission acquisition
or the configured source protocol fails.

An operator owns the stable source namespace/incarnation UUID. Preserve it across
verified same-owner restarts/restores; provision a different namespace for an
independent clone or reset that can reuse IDs. Never enable two independent writers
under the same identity or use the synthetic `...000080` namespace in a real
connection. Namespace rotation requires a new explicit Host connection/binding;
it does not rewrite existing Manifests. No automatic registration or live rotation
is performed here.

## Capture, freeze and recovery

The only new domain storage is `ReleaseSnapshot`, with a PROTECT source receipt
and Project and scalar creator/policy evidence. The request commits before queue
dispatch. A worker atomically claims queued state/token, verifies actual #79 native
and standard bytes, captures media, constructs the Manifest and finalizes under
Project -> sorted creator authority rows -> snapshot row locks. Both creators'
revocations serialize with final publication. There is no database lock held
across media copying or remote I/O (no remote I/O is performed by this feature).

PostgreSQL migration 0013 makes request/source/authority columns immutable on every
UPDATE. It rejects direct READY insertion, incomplete READY transition and deletion.
After READY, sealed fields/token cannot change; only quarantine disposition is
permitted, with no transition back to READY. Read-time checks additionally bind
Manifest/content/artifacts/media to saved identities and the #79 source graph.

Duplicate RQ delivery cannot claim the same running/READY token twice. Queue loss
leaves `dispatch_pending`; retry does not reselect source input. An unstarted
queued request older than 30 minutes **since its most recent successful enqueue**
may receive a fresh token. Request creation time is not queue age: an outage must
not make a newly dispatched claim immediately reclaimable. Migration 0014 adds
`queued_at`, initializes old queued claims conservatively at migration time, and
preserves all existing READY bytes/hashes. The timestamp is immutable after READY;
retained snapshots also prohibit reversing this migration. A running request
older than 31 minutes can be explicitly fenced as `failed/CAPTURE_INTERRUPTED`.
A late worker then cannot publish. Capturing failures and interrupted captures
require a new key/snapshot, not recapture under an old identity. Incomplete files
are retained privately for diagnosis; this feature adds no orphan deletion job.

## Content and envelope hashes

The machine-readable schema is
`label_studio/data_export/release-manifest-v1.schema.json`.

`content_digest = SHA256(JCS(content))`. Content includes namespace, organization,
project, numeric-ascending Submission membership, assignment/revision/task identity,
legacy result and complete record hashes, exact XML/schema hashes, mapping/runtime
and output profile, canonical filters, and media reference/checksum/size membership.
IDs in the new content are decimal strings, including values above JavaScript's
safe-integer range. Null filters are absent; dates normalize to UTC. Exact XML
SHA-256 is explicitly `label-studio-exact-xml/v1`, not the legacy Python integer
hash or an assertion about a Host schema version.

Generation time, initiating principal, authorization/policy evidence, snapshot ID,
#79 receipt/hash, actual artifact inventory and media blob IDs are frozen in the
envelope. They do not introduce clock/UUID noise into content identity. The complete
original envelope has its own detached SHA-256. Same content from another receipt
has the same content digest but can have a different envelope hash. Content
determinism does not promise deterministic third-party ZIP bytes or a signature.
All historical author/reviewer/snapshot evidence remains in the verified native
records and referenced #79 Manifest, rather than being rederived from current roles.

The exact finalization authorization receipt ID/time is bound into the envelope
in the READY transaction and rechecked against the saved audit event on reads.
Audit failure aborts publication. Private file bytes and parent directories are
fsynced before READY; a missing retained object still fails closed after restore.

## Managed media profile

First version supports inline Text and direct Image/Audio/AudioPlus/Video/Pdf
references to native `/data/upload/...` FileUploads owned by the exact Project.
The configured `FORCE_SCRIPT_NAME` prefix is also accepted at its exact path
boundary; arbitrary prefixes remain denied. The original reference is preserved
in the native record and Manifest, including the prefix. The portable schema
allows prefixed native paths, while the resolver enforces the live configuration.
Resolve references through FileUpload membership, not a client path or URL fetch.
Files must be literal regular local files under MEDIA_ROOT; symlink components,
foreign uploads, traversal, arbitrary HTTP/LocalFiles, URL-valued Text, HyperText,
time-series and unknown transforms are unsupported and fail closed. No resolver
silently falls back to references-only while claiming a complete media snapshot.

Stream from one opened source descriptor into an exclusive private blob. Enforce
64 MiB/file, 256 MiB total and at most 10,000 media references. Verify length,
checksum, source metadata and a second read before capture succeeds. Flush/fsync
file bytes and check private copies before finalization. Blob IDs are UUIDs derived
only by the server; no user filename controls a private path.

The explicit guarantee is **capture-time bytes**, not submission-time media
provenance. Old Submissions only retained URLs, so this feature cannot prove their
media had the same bytes when a human annotated them. A Host requiring that
stronger proof must reject this profile until a separately validated ingestion /
submission-time media identity contract exists.

After successful capture, source upload mutation/deletion does not change the
private snapshot. A missing/corrupt private blob or source artifact blocks the
whole snapshot's content and quarantines corrupt snapshot payloads; no in-place
rebuild occurs. Download checks the same descriptor that is subsequently streamed;
HEAD/Range/If-Range never skip current authorization or integrity checks. Private
bytes remain no-store attachments, never static, signed public URLs or UI embeds.
Source receipt/hash/inventory failure also quarantines a previously READY
snapshot; it must not continue advertising READY after its evidence chain fails.
The shared source gate verifies native/standard bytes for history and object
reads; source corruption terminally quarantines the #79 receipt and every READY
dependent snapshot, including when discovered through history. Pinned-media
corruption remains scoped to its snapshot. Already quarantined #79 sources also
invalidate dependent READY dispositions. No source or Manifest is regenerated.
Current authority denial remains distinct and does not mark sound bytes corrupt.
UI polling survives transient network/server/body failures, but stops on authority
denial, selection changes or unmount; stale responses cannot restore controls.
Integrity/quarantine responses clear stale READY download/comparison controls
and continuations as well; comparability or request-input errors do not revoke
access to otherwise valid content.

## Difference semantics

Source identity is namespace/incarnation + kind + Submission ID; a different
fingerprint under the same source ID is a conflict, not a new version. Host enforces
namespace + Submission ID uniqueness across connections and workspaces (the first
profile accepts only kind `submission`). A connection is an authorization binding,
not an extra identity component; replacing it cannot bypass a source conflict. Project/task/assignment/hash
are assertions, not key components that disguise a source conflict.

For sample comparison only, align by assignment within the same namespace/org/
project. One revision per assignment is already enforced by #79. A new Submission
revision for the same assignment is revised; separate assignments for the same
task remain separate samples. Assignment replacement is removed + added. Media
checksum changes count as revised. XML configuration change is separately reported
and marks common members revised. Counts cover the complete bounded membership;
`offset` only pages the detailed changes. Different namespaces/projects cannot be
compared. No latest-task selection, task-level merging or silent deduplication.

## Proposed Host handoff (contract, not a new live Host API)

Host's server-side adapter receives an explicit operation key, registered connection,
source snapshot ID and expected Manifest SHA-256, and an authorized target Dataset.
Connection -> namespace/incarnation and project binding come from trusted Host
configuration. Actor/workspace/consumer are authenticated server context, never
body fields. Adapter chooses a configured Fork endpoint; input cannot choose a URL.

The adapter authenticates and pulls exact Manifest/native/media bytes, validates
schema/profile/canonical hash, complete membership and source fingerprints, then
stores a Host-controlled immutable copy before committing typed source bindings
and DatasetVersion using existing Host Commands/producer/rights gates. Mapping is
out of band: target Dataset + registered connection + snapshot ID/Manifest hash ->
Host DatasetVersion ID and content digest. Host chooses whether identical content
reuses a version; Fork does not allocate or overwrite business version numbers.

Same operation/key/fingerprint replays its receipt after current authority and
source validation. Changed target/digest conflicts. Response loss after commit is
UNKNOWN to the caller: query the same operation; do not blindly create again.
Hash mismatch, source identity conflict, unsupported profile, missing media and
scope/authorization failure are terminal admission blockers. Transient transport
failure is distinct and never proves source absence or remote rollback.

Fork revocation stops subsequent source reads, including replay. It cannot recall
delivered bytes or silently delete a Host-retained copy. Host source dispositions,
rights/current-delivery authorization and retention remain Host facts. If Host
policy requires a live upstream dependency and cannot freshly validate it, Host
must fail closed; this first version does not claim a distributed revocation
transaction, automatic recall or permission to reuse an old allow decision.

`tests/synthetic_host.py` is only a synthetic contract receiver. It validates
authenticated source reads, canonical/schema/hash/membership, immutable copies,
same-source conflict, mapping replay, lost response/lookup and revocation. Its
synthetic `ACCEPTED_SOURCE` receipt explicitly has `gold_certified=false` and
`published=false`. It is not imported by production code or connected to a Host
database. Real controlled-Fork adapter/source-binding gaps remain independent.

PR #100 review tightened the synthetic admission contract: member project/task
assertions and both native JSON representations must agree; the artifact inventory
must contain every declared output with exact member counts. The shared v1 XML
profile grammar defines required media fields. Native references, per-member pins
and the envelope inventory must form one complete graph, with no missing, duplicate
or conflicting bindings. Self-consistent hashes alone do not establish this.
Reusing an existing target/snapshot binding under a new operation key must validate
the retained Host copy; it cannot silently repair a missing/corrupt object. These
are synthetic contract checks, not fixes or acceptance evidence for the real Host.

## Rollback and verification

Disable the explicit project policy / stop the new authorized worker entrypoints;
preserve receipt, snapshot, audit and private files. Never restore mutable export.
Migration 0013 reverse holds an exclusive table fence and refuses rollback if any
snapshot exists. Only fresh disposable databases may reverse/drop the new tables.
No retention/delete command is added.

Required verification is real PostgreSQL safety/concurrency plus media integrity,
exact diff, synthetic Host contract, bilingual component/race tests and real
en-US/zh-CN Cypress journeys. The existing Approved Dataset Export CI job includes
the new suites and both browser specs; its source configuration is not a claim
that GitHub CI has executed. Local acceptance, PR CI, main CI, deployed runtime,
real Host integration and product acceptance remain separate.

#76 worktrees, its 110 pre-existing deleted binaries, retained containers, mounts,
images, database, credentials and browser profiles are outside the write set.
