# #79 approved Submission batch export — design for confirmation

Status: **DESIGN_CONFIRMED / LOCAL_IMPLEMENTATION / NOT_MERGED / NOT_DEPLOYED**.
Prepared 2026-10-21 against `main@88809e618a34292c512dd75cb85ce24e52ddae59`.
The user confirmed this design and authorized isolated implementation/testing on
2026-10-21. The following is the confirmed design baseline; see the
[implementation and operating contract](approved-dataset-export.md) for delivered
behavior and its verification entrypoints. Core integration remains unauthorized.

## 1. Decision and ownership

Add a project-scoped, bounded `ApprovedDatasetRelease` aggregate in the existing
`data_export` app. It freezes exact approved Submission revisions and produces
private artifacts. It is an export receipt, **not a Host DatasetVersion or a Host
business release decision**. Preserve every existing mutable export/storage guard.

| Owner | Responsibility |
| --- | --- |
| #79 / Fork | Exact selection, approval checks, frozen copies, native JSON/JSONL, SDK conversion adapter, job recovery, authorization on each access, local manifest/checksums, bilingual download UI |
| #80 / Fork adapter + Host contract | Cross-system `ReleaseManifest v1`, instance identity provisioning/rotation, Host DatasetVersion mapping, version differences, durable media checksums/copies, retention/revocation handoff and reproducibility guarantees |
| Host | Tenant identity, final business publication, Gold/certification/delivery policy, dataset business version numbers |
| Core-only-review | Authorized ordinary Submission acquisition followed by Core review; no synthetic Fork approval, no external batch release through #79 |

#80 consumes #79's receipt; #79 must not wait for a full versioning service or
invent a second authoritative version number. Its local wire schema is named
`fork.approved-export-manifest/v1`, reserving `ReleaseManifest v1` for #80.

## 2. Baseline evidence and architectural gaps

Paths in this section refer to the fixed baseline above.

* [tasks/submissions.py](../../label_studio/tasks/submissions.py): the existing
  snapshot contains annotation results, task data/meta and exact label XML;
  `_snapshot_hash` is SHA-256 over sorted compact Python JSON. Approved/rejected
  historical revisions survive subsequent submissions; only pending ones are
  superseded. Therefore "all approved" is not necessarily one revision per task.
* [tasks/api.py](../../label_studio/tasks/api.py): `SubmissionReleaseAPI` checks
  Manager and approved status for one immutable revision. It is not a batch job,
  and does not recompute the snapshot hash or require a matching ReviewDecision.
* [data_export/api.py](../../label_studio/data_export/api.py) and
  [mixins.py](../../label_studio/data_export/mixins.py): old export, conversion and
  workers deliberately refuse projects with formal Submissions. Their input
  queries include mutable Annotation/Task state and conversion uses current
  project configuration. Do not subclass that extraction path for approved data.
* [core/redis.py](../../label_studio/core/redis.py):
  `start_job_async_or_sync` falls back to synchronous execution. That fallback
  does not satisfy the proposed bulk-job contract when Redis is unavailable.
* [projects/models.py](../../label_studio/projects/models.py):
  `label_config_hash` is a BigInteger assigned by Python `hash(str(label_config))`.
  It is legacy metadata, not a portable cryptographic configuration fingerprint.
* [tasks/models.py](../../label_studio/tasks/models.py): Submission and review
  persistence is not a write-once archive; assignment deletion cascades to its
  Submissions, and reviewer/author FKs can become null. A job containing IDs alone
  cannot promise a frozen historical receipt.
* [access_control/identity.py](../../label_studio/access_control/identity.py):
  `resolve_actor` supplies the trusted mapped, active local actor. A mutable
  request `actor`, `reviewer` or `created_by` field must never supply audit identity.
* [authorization-plan.md](authorization-plan.md#controlled-core-only-review-pilot-submission-protocol-v1)
  describes Core-only internal reads, but there is no explicit batch-release mode
  enforcement to reuse. New batch release must not infer permission from a query
  parameter, a UI route, the presence of approved rows, or the service account name.

Reuse Django transactions/constraints, existing identity and project authorization,
RQ/django-rq, the pinned Label Studio SDK Converter, and `RangedFileResponse`.
Do not build another queue, format converter, IAM or dataset versioning system.
Legacy single-release hash-integrity hardening is a separately identified gap;
this design adds the stricter predicate to all **new batch** paths without
claiming to retrofit or certify the legacy endpoint.

## 3. Scope and selection semantics

* One batch belongs to exactly one existing organization and project.
* Input is an explicit set of `(submission_id, revision, result_hash)` triples.
  No "whatever is latest when the worker runs" or asynchronous live-filter query.
* Exactly one revision per assignment is permitted. Multiple assignments for
  the same task are allowed and remain separate samples identified by Submission.
  No silent merging, adjudication or task-level deduplication. Older approved
  revisions remain explicitly selectable even if a newer revision is pending.
* Reject duplicate IDs and multiple revisions of one assignment, rather than
  silently deduplicating. Reject the whole selection on any invalid member.
* Every selected row must have `status=approved`, an actual approved
  ReviewDecision bound to that same Submission, and a snapshot whose recomputed
  hash matches the stored hash and the requested hash. Missing review evidence,
  missing frozen author/reviewer ID at creation, nonfinite numbers, inconsistent
  task/project/assignment IDs, or malformed snapshot fail closed.
* All snapshots must contain the same exact nonempty label XML. Compare UTF-8
  SHA-256 of the XML, then the exact XML bytes; ignore legacy hash differences
  for compatibility grouping. A mixed configuration batch is rejected in v1.
  Current Project XML or mutable Annotation never replaces frozen data.
* Defaults proposed for implementation: page size 50, maximum 100; 10,000 items,
  2 MiB request body, 64 MiB total canonical frozen record bytes, 256 MiB per
  generated artifact, 512 MiB attempt scratch quota, 30-minute worker timeout.
  Reject over-limit work explicitly; never truncate it. Lower operational limits
  may be configured, and effective limits appear in capabilities.
* The bounded input freeze is synchronous and transactional. Artifact generation
  and conversion are always asynchronous and streamed. Unbounded projects need
  separately confirmed chunked capture semantics; v1 does not claim to support them.

## 4. Server policy and authorization

### Trusted mode configuration

Propose a narrowly scoped server setting `APPROVED_EXPORT_POLICY`, separate from
client input and business versioning. It has a policy version and explicit
project entries keyed by `(source_namespace, organization_id, project_id)` with
mode `fork_approved`, `core_only_internal` or `disabled`. No entry, unknown mode,
missing namespace or invalid configuration means `disabled` for the new API.
The project operator must explicitly admit a standalone Fork project as well.
This draft does not change any deployed setting or existing integration mode.

`source_namespace` is an operator-provisioned stable UUID for the controlled
source connection/instance. Do not derive it from hostname, credentials or IDs;
do not generate a new value at process startup. Copying/restoring an instance
requires a namespace ownership decision before enabling exports. #80 owns the
eventual provisioning/rotation contract; #79 requires a supplied value.

`fork_approved` admits only the batch flow below. `core_only_internal` rejects all
new batch preview/create/status/manifest/download routes, even if accidentally
approved Fork records exist. It does not change the authorized ordinary
`GET /api/submissions/` and `GET /api/submissions/{id}/` acquisition APIs.
No `mode=...` override or Core decision field is accepted by #79. Changing a
policy entry affects every later worker/access check; a policy version change
invalidates existing batch access and requires a newly created batch.

### Effective predicates

All requests resolve `(principal, actor)` through `resolve_actor`. Fetch a visible,
nondeleted project within the trusted actor's current organization; require an
active organization membership (`OrganizationMember.deleted_at IS NULL`) and
effective Manager authority. The project creator's existing implicit Manager
role is retained, but cannot bypass inactive-account/organization checks.
Organization admin or superuser alone is not a new release permission.

Workers resolve the stored principal through the configured trusted identity
provider and reload the mapped active actor and memberships from the database;
they do not reuse request objects, cached roles or a queue-service superuser.
For the current local provider this is a fresh actor/Principal reconstruction.
An identity provider without a trusted background revalidation adapter must
disable this feature; do not manufacture a user-supplied request to the provider.
Current browser `active_organization` is request context, not a durable grant:
worker authorization uses the batch's frozen organization plus live membership.
Do not store the initiating session/token in the queue. Session logout alone
ends browser access; background work remains actor-scoped unless account,
membership or project policy authority is withdrawn.

| Operation / actor | Effective Manager | Reviewer / Annotator | Core internal reader | Wrong organization / disabled account |
| --- | --- | --- | --- | --- |
| Capabilities / candidate preview / create | Allow only `fork_approved` | Deny | Deny batch routes | Deny |
| Batch list / safe status | Project-scoped only | Deny | Deny | Deny |
| Frozen items / manifest / artifacts | Also revalidate original creator and source integrity | Deny | Deny | Deny |
| Retry failed attempt | Original creator only, still authorized | Deny | Deny | Deny |
| Ordinary Submission internal acquisition | Existing read policy; unchanged | Existing read policy | Existing authorized read policy | Existing denial policy |
| Host final business publication | No new authority conferred | No new authority conferred | Core/Host governs | No authority |

Revalidate before freeze, before job start, before publishing READY, and on every
frozen-content/manifest/download/Range/HEAD request. The requester and original
creator must both retain active account, organization membership and Manager
authority for artifact access. Other current Managers can inspect safe status;
they must create a new receipt if the original creator lost authority. Reviewer
revocation after a historically valid decision does not erase that decision;
missing/deleted review source or mismatched review evidence blocks delivery.
Assignment reassignment never substitutes a new author's data into a frozen item.

Checks use fresh DB state at each boundary. Revocation denies subsequent requests,
including resumed ranges, and prevents later publication. Bytes already received
or an already authorized in-flight response cannot be recalled; do not promise
continuous revocation of an open stream. No cached allow verdict grants access.
When a job observes lost creator permission, mark it `blocked_auth`; that receipt
cannot be retried. Restoring permission requires a new receipt. A READY receipt
can be denied on access without mutating its frozen input or claiming that it
detected every historical permission change between requests.

No direct storage URL, bearer download link, `FileField.url`, public MEDIA_ROOT
path or use of an organization owner's token is permitted. UI visibility has no
authorization effect. Collection queries and nested batch/artifact lookups must
all bind to the authorized project; opaque UUIDs are not an access boundary.

## 5. Proposed API v1

Base: `/api/projects/{project_id}/approved-dataset-releases/`.
These routes are additive and do not restore `/export`, `/exports` or storage
delivery. Authentication/CSRF follow existing DRF session/token policy.

| Method / suffix | Contract |
| --- | --- |
| `GET capabilities/` | Mode, limits, native formats, server-computed SDK capabilities for a selected frozen label config; no authority is granted by this response |
| `GET candidates/` | Approved candidate metadata with exact triples, task/assignment IDs, submission/review times, reviewer ID and config SHA-256; signed keyset cursor, maximum 100 per page |
| `POST preview/` | Validate the same body as create, without persistence/queueing; return normalized selection count, config fingerprint, format capability and conflicts; not a reservation or authorization token |
| `POST <base>` | Freeze valid exact inputs; required `Idempotency-Key`; `202` with receipt/status URL, `201` is not an artifact-ready claim |
| `GET <base>` | Cursor-paginated safe batch summaries in authorized project, excluding raw input/media/storage paths |
| `GET {release_id}/` | Safe state, counts, input/content digests, attempt number, timestamps and sanitized error code; no raw results |
| `GET {release_id}/items/` | Cursor-paginated frozen native records, with full content gate and integrity validation |
| `GET {release_id}/manifest/` | READY-only final manifest bytes with detached SHA-256 response header and full content gate |
| `GET/HEAD {release_id}/artifacts/{artifact_id}/download/` | READY-only authorized immutable bytes; single HTTP Range supported; no redirects to public storage |
| `POST {release_id}/retry/` | Empty body, same original actor, failed retryable job or dispatch-pending only; never reselect input or change format; `202` |

Candidate filters are bounded `task_id`, `assignment_id`, `submitted_from/to`
and `reviewed_from/to` (UTC, `[from,to)`). Candidates always mean approved with
matching review. Ordering is ascending Submission ID. Cursor binds project,
principal, normalized filters and last ID and expires after 15 minutes. A live
candidate traversal is discovery, not a consistent dataset snapshot: newly
approved lower IDs may require a fresh traversal. The frozen create set is the
only authoritative membership. No "select all now, rerun filters later" option.

Capabilities without an item returns only native formats/limits; with an
authorized `submission_id` it computes the SDK set from that snapshot's XML.
Preview/create always recompute capabilities across the exact selected input.
Body filters are recorded provenance only; create verifies selected IDs satisfy
them, but filters cannot add further rows or stand in for explicit selection.

Example create body (hashes here are abbreviated, **not valid request values**):

```json
{
  "items": [
    {"submission_id": 601, "revision": 2, "result_hash": "<64 lower-case hex>"},
    {"submission_id": 602, "revision": 1, "result_hash": "<64 lower-case hex>"},
    {"submission_id": 603, "revision": 3, "result_hash": "<64 lower-case hex>"}
  ],
  "output": {"format": "JSONL", "resource_mode": "references_only"},
  "filters": {"task_id": null}
}
```

Each successful batch produces both native JSON and JSONL as independently
verifiable artifacts. `output.format` chooses the primary download; a supported
standard format additionally produces its conversion artifact set. No lazy
conversion runs on download. Artifact byte limits apply to every output and the
scratch quota applies to the combined attempt.

Reject unknown body fields, including `actor`, `reviewer`, `approved`, `mode`,
`namespace`, `label_config`, file paths and supplied snapshot bodies. Empty sets
are invalid. A create response supplies `release_id`, `state`, `item_count`,
`content_sha256`, `input_sha256`, `retryable`, `status_url`; no download URL exists
until READY, and the eventual URL still requires authentication.

Use the existing error envelope plus stable `display_context.reason` and a
sanitized request ID. Error codes are shared by backend and bilingual UI:

| HTTP | Reason / conditions |
| --- | --- |
| `400` | `INVALID_REQUEST`, `DUPLICATE_SELECTION`, `AMBIGUOUS_ASSIGNMENT_REVISION`, `EMPTY_SELECTION` |
| `401/403` | Existing authentication semantics / `RELEASE_FORBIDDEN`, `RELEASE_MODE_FORBIDDEN`, `CREATOR_AUTH_REVOKED` |
| `404` | Invisible project, mismatched nested IDs or unresolved/foreign selection: `NOT_FOUND`; never reveal foreign metadata |
| `409` | `REVISION_MISMATCH`, `SNAPSHOT_HASH_MISMATCH`, `APPROVAL_REQUIRED`, `REVIEW_EVIDENCE_INVALID`, `MIXED_LABEL_CONFIG`, `SOURCE_UNAVAILABLE`, `IDEMPOTENCY_CONFLICT`, `RELEASE_NOT_READY`, `POLICY_VERSION_CHANGED`, `RUNTIME_VERSION_CHANGED`, `ARTIFACT_INTEGRITY_FAILED` |
| `413` | `BATCH_LIMIT_EXCEEDED` (bytes/items/artifact budget), with effective limit |
| `422` | `FORMAT_UNSUPPORTED`, `RESOURCE_MODE_UNSUPPORTED`, `SNAPSHOT_INVALID`; a listed SDK format is insufficient if the adapter cannot represent this input |
| `429` | Per-actor/project active-job quota; proposed maximum two active jobs per project, no more than one per actor/project |
| `503` | `QUEUE_UNAVAILABLE` before freeze; no synchronous generation fallback |

Authorization and project scope checks precede validation/conflict details. A
foreign/missing ID invalidates the whole body without reporting which foreign
object exists. Validation conflicts never create a partial READY artifact.
Retryability is a server classification, not something the client may override.

## 6. Freeze transaction, persistence and concurrency

Proposed additive tables (final migration names allocated only after design
approval; never take #76 migration ownership):

| Table | Immutable fields / mutable operational fields |
| --- | --- |
| `ApprovedDatasetRelease` | UUID, frozen org/project IDs, live project FK `PROTECT`, source namespace, original principal/actor, normalized request + request hash, idempotency key, exact label XML + SHA-256, frozen policy version, input/content digests, freeze receipt / state, last sanitized failure, attempt sequence and timestamps |
| `ApprovedDatasetReleaseItem` | Release, ordinal, source ID, task/assignment/revision IDs, original hash, full snapshot copy, frozen submitted_by/reviewer IDs and timestamps, ReviewDecision ID/decision, canonical record hash; no content updates after creation |
| `ApprovedDatasetReleaseAttempt` | Attempt number, fenced execution token, lease/heartbeat, phase/state, dispatch result, sanitized failure, start/finish / append-only receipts |
| `ApprovedDatasetReleaseArtifact` | Release, logical artifact UUID, format, private immutable storage key, exact byte SHA-256/size, item count, converter/mapping version, successful attempt ID; no public URL |
| `ApprovedDatasetReleaseAccessEvent` | Append-only phase, actor/principal, batch/project scope, policy version, allow/deny + reason and timestamp; no secrets/raw result bodies |

Unique constraints: `(org, project, actor, idempotency_key)`, `(release,
submission_id)`, `(release, assignment_id)`, `(release, ordinal)`, `(release,
attempt_number)` and `(release, artifact logical name)`. IDs for deleted humans
are retained as frozen scalar evidence; optional live user FK uses SET_NULL.
Items do not cascade through Submission/Assignment. Release-to-item cascades are
only for controlled, separately authorized retention; no delete API in v1.
Project PROTECT is intentional: hard deletion cannot silently destroy retained
receipts. Soft-deleted projects block all release access. Removing protected
receipts is future retention work, not an automatic workaround for deletion.

Freeze algorithm, all-or-nothing:

1. Authenticate, check live scope/mode/queue availability, body/limits and
   idempotency. Normalize triple order by Submission ID and compute request hash.
2. In a short bounded transaction lock the project, then selected Submissions in
   ascending ID using PostgreSQL `select_for_update(of=('self',))`. Do not lock
   nullable joined users/Annotation. Read their bound ReviewDecision after the
   Submission lock. Preserve existing project-before-Submission lock order;
   do not introduce a reverse Assignment lock or hold locks during conversion.
3. Validate every triple, project/assignment/task relation, hash, approval and
   configuration. Copy exact snapshots/review/author evidence into new items;
   compute record/content/input digests from frozen copies, not frontend JSON.
   Revalidate creator/policy before commit. All rows and freeze authorization
   receipt commit together. Request completion does not imply generation success.
4. `transaction.on_commit` dispatches to existing RQ. Persist `dispatch_pending`
   first so a queue race/crash cannot lose the input. Enqueue failure after commit
   leaves the receipt recoverable and returns `202` with `retryable=true`; it
   cannot honestly be reported as a rollback. UI surfaces this distinction.

Existing submission/review services already serialize with the relevant project
or Submission row. Membership changes are rechecked at each boundary; this design
does not claim that all historical role mutations share a global atomic lock.

Same key + same normalized request returns the existing authorized receipt,
including after a lost HTTP response. Same key + changed body returns `409`.
Replay always rechecks authorization; it is not a bearer capability and cannot
bypass actor/policy invalidation. A unique-constraint race resolves to the one
committed receipt. Keys are opaque 1–128 character strings, scoped as above and
retained with the receipt. Distinct keys deliberately permit distinct receipts.

State machine:

```text
dispatch_pending -> queued -> running -> ready
       |              |          |
       +--------------+----------+-> failed_retryable -> dispatch_pending
       +--------------+----------+-> failed_final
       +--------------+----------+-> blocked_auth
```

Only the current fenced attempt may publish READY. Claim and transition under
DB row lock; duplicate delivery of the same RQ job is a no-op if already claimed
or READY. Use a heartbeat and bounded lease; reclaim only after RQ is confirmed
finished/failed or the lease expires. A stale worker may continue computing but
cannot publish or overwrite another attempt's files. An authorized retry creates
a new attempt over the same sealed input; it never updates the selection.
UI retry or operator recovery uses the same authenticated endpoint for stranded
dispatch/expired attempts; no new recurring dispatcher is authorized by this
design. No dependency on #91's eventual general event Outbox is introduced.

Before run and publication validate source rows still exist and match frozen
IDs/revision/hash, snapshots and review metadata; mismatch/deletion is a final
failure, not permission to substitute a current Annotation. A new approved
revision does not invalidate the selected historical approved row. A selected
source later deleted cannot be downloaded even if a frozen copy survives.
The copy preserves evidence; #79 does not promise offline permanent availability.

Worker reads frozen copies in ordinal chunks, streams bytes and SHA-256, and
uses private attempt-scoped temporary files. Finish/close/fsync files, validate
counts/digests/converter output, recheck authority and source, then publish the
immutable artifact handles and final manifest in one DB transition. Storage
finalization failure cannot produce READY. Private orphan files are harmless
inaccessible leftovers; cleanup must use exact release/attempt ownership after
lease expiry, never a shared-volume prune. Retryable failures are queue/storage
transients or interrupted/expired execution; input/approval/hash/policy/format
errors are final. Operational failure does not mutate the sealed input.

## 7. Native records, Manifest and checksums

The adjacent [example](issue79-approved-export-example/README.md) contains three
entirely synthetic records and a fully hashed manifest. It demonstrates the wire
contract only; it is not database/API/job/converter/browser acceptance evidence.

Native JSON is an array of envelopes, one per selected Submission, containing:
`source {namespace, kind=submission, id}`, `project_id`, `task_id`, `assignment_id`,
`revision`, `result_hash`, frozen submitter/time, frozen approved review evidence,
and the **entire original `result_snapshot`**. Do not flatten native data into
mutable Label Studio Task serialization or lose lead_time/task meta/config XML.
`JSONL` is exactly one compact envelope per LF-terminated line, including final
LF. JSON is `[` + comma-separated compact envelopes + `]`; UTF-8, no BOM or
trailing LF. These two deterministic encodings define artifact checksums.

Canonicalization IDs:

* `python-json-snapshot-v1`: existing `_snapshot_hash` sorted keys, compact
  separators, `ensure_ascii=False`, UTF-8. Preserve the historical digest exactly;
  reject nonfinite numbers instead of repairing historical payloads.
* `python-json-export-v1`: same encoder on finite JSON values, with
  `allow_nan=False`, CPython 3.11 JSON numeric rendering. This is **not RFC 8785**;
  other runtimes must reproduce these exact bytes or verify supplied raw bytes.
  No Unicode normalization, trimming, float coercion or XML reformatting.

Use a dedicated SHA-256 of exact XML UTF-8 bytes for `label_config_sha256`.
Retain legacy `label_config_hash` inside the unmodified snapshot, never treat it
as SHA-256 or recompute it with the current process's Python `hash()` seed.
Encode IDs as positive JSON integers; clients must use lossless integer parsing
for frozen payloads, including legacy 64-bit hashes. Clients submit only triples,
never parsed/reserialized snapshots.

The final manifest has:

| Field | Meaning |
| --- | --- |
| `schema` | `fork.approved-export-manifest/v1` |
| `input.content` | `fork.approved-export-content/v1`, namespace/project/org, ordering, one-per-assignment revision policy, exact XML/SHA-256, native mapping version and ordered item fingerprint list |
| `input.content_sha256` | SHA-256 of canonical `input.content`; independent of batch UUID, requesting actor, generation time, chosen output format and request presentation |
| `input` remainder | Receipt UUID, normalized request/filters/output profile, actor/principal, frozen UTC timestamp, access-policy mode/version, frozen authorization receipt, snapshot/record canonicalization IDs |
| `input_sha256` | SHA-256 of canonical complete `input`; binds this particular authorized export request |
| `generated_at` | UTC generation completion timestamp, outside sealed input |
| `artifacts[]` | Logical ID/name/format, bytes, record count, SHA-256 and mapping/converter provenance; no storage key or credential |

Each `content.items[]` fingerprint holds source identity, assignment/task,
revision/result hash, frozen submitter/time and review, plus `record_sha256` of
the full native envelope. Items sort by numeric Submission ID. This distinguishes
same-task samples and verifies full native metadata as well as annotation result.
Stable source identity is **namespace + kind + Submission ID**. Project/task,
assignment/revision/hash/config/review are assertions; changing one cannot
legalize an identity collision. Scope is consistent across content and items.

Manifest is canonical UTF-8 JSON without a self-hash field. Its SHA-256 is a
detached API receipt/header `X-Manifest-SHA256`; artifact digests are inside the
manifest. No circular "manifest hashes archive containing itself" construction.
Content digest stability is not a promise that a ZIP from a third-party Converter
has stable bytes: record its actual artifact hash and exact converter provenance.
SHA-256 proves byte integrity, not author identity/signature or Host approval.

The frozen authorization receipt records server decision, actor, effective role,
policy version, project and time, but it is historical evidence only. Later
allow/deny decisions append separate access events and never alter the manifest.
If recording an allow decision fails, fail closed before serving content; log a
sanitized deny diagnostic without exposing results if audit storage is unavailable.

## 8. Converter and media boundary

Use the existing pinned SDK `Converter` (baseline declaration 2.0.18 and locked
source revision in `poetry.lock`); record actual installed distribution version,
locked source identity and the native-to-SDK mapping version. Fail retry on a
different runtime/adapter version instead of silently generating a different
artifact. No additional converter implementation or new token is needed.

Build transient SDK input solely from each frozen native envelope: one Task-like
record with frozen task data/meta and exactly its selected annotation result,
original snapshot annotation ID, frozen submitter, lead_time, cancelled and
ground_truth fields. Multiple assignments for a task are kept as separate
samples; an output format that cannot preserve their identity/count is rejected.
The mapping must maintain a source-to-output index in the conversion artifact
set, so a converted result maps back to its native fingerprint. All output files
are enumerated/hashes recorded; ZIP archives include only validated expected
members and safe relative names. Never call the old mutable export serializer.

Instantiate Converter from the **frozen XML**, using its actual supported format
set intersected with implemented/tested adapters and selected input compatibility.
Never use a global UI format list or current parsed Project config as evidence.
Proposed first standard acceptance: a real frozen text NER fixture converted to
the SDK's CoNLL capability, if runtime probing confirms it. COCO/VOC/YOLO and
other formats are advertised only after corresponding adapter/sample validation;
unsupported/unverified cases return a reason, not an empty successful artifact.
The #79 completion gate still requires at least one real standard-format fixture.

V1 accepts only `resource_mode=references_only`: JSON/JSONL retain snapshot media
references verbatim with `media_integrity=unverified_reference`. They do not claim
to freeze the remote bytes. Standard conversion cannot fetch network resources,
use an organization-owner token, read arbitrary local paths or use current task
media to fill missing frozen metadata. Reject conversion needing unavailable
dimensions/resources or unsupported media integrity. Embedded immutable metadata
may be used if the adapter validates it. `download_resources=true`, arbitrary
paths and signed media URLs supplied in the request are rejected. Original media
endpoints retain their existing independent checks. Full media pinning/checksum
and missing-media reproducibility policy are #80 work, not implied by this receipt.

## 9. Download and recoverability

Private artifacts live outside publicly served MEDIA_ROOT/STATIC_ROOT with no
anonymous storage URL. Reuse ranged-response support against an opened authorized
file handle, not the old download route. Verify the immutable artifact's full
size/hash before response; corruption yields no bytes and an integrity failure.
Concurrent replacement is forbidden: storage keys are attempt-specific and
published bytes are write-once. Do not reopen an unverified user-selected path.

`GET` returns `200`; one satisfiable Range returns `206`; invalid/unsatisfiable
range returns `416`. Strong ETag is the exact artifact SHA-256;
`Accept-Ranges: bytes`, stable Content-Length, safe attachment filename,
`Cache-Control: private, no-store`, `X-Content-Type-Options: nosniff`.
Authorize before handling conditional headers, HEAD, Range, 304 or 416, so cached
metadata/If-Range cannot bypass revocation. Matching If-Range resumes; mismatching
If-Range receives the full authorized file. Every resumed request repeats the
content gate. Do not introduce X-Accel/offload until an internal-only location
and per-request access control are independently validated.

Resume means re-requesting bytes of the same READY artifact, not continuing a
partially generated file. Transient generation failures retry frozen inputs;
READY artifacts are never overwritten. A corrupt READY artifact is quarantined
and requires a new receipt; no silent regeneration under the same artifact ID.

## 10. UI contract and rollout

Extend the existing release workspace with a distinct batch-export action after
service capabilities authorize it. Display exact revisions/reviewers/config,
selected sample count versus distinct task count, multi-assignment explanation,
format capability and references-only media notice. Selection across pages stores
triples, not mutable annotation bodies. Preview is followed by server-side create
validation; stale selection errors preserve the user's selection for correction.
Show dispatch-pending, queued, generating, failed/retryable, denied and READY
states. Permit authenticated resume/download only on READY. All text, conflicts
and disabled-format reasons require en-US/zh-CN entries. No Host "published" or
Core "approved" badge is inferred from batch READY.

After design confirmation and implementation authorization, use an isolated #79
worktree and fresh synthetic PostgreSQL/files/ports for the agreed tests. Do not reuse #76
candidate Compose, containers, volumes, DB, images, browser profiles or delivery
bundles. This design creates none of those resources. Proposed additive migrations
must be reviewed for source deletion PROTECT effects, indexes and rollback.
Rollback disables the new routes/jobs while preserving receipt/audit tables and
files. Do not reverse-migrate/drop data in a retained environment. Reverting to
legacy export does not become a release fallback. No merge, deployment, live
provider use or Core integration is authorized by design confirmation alone.

## 11. Required implementation acceptance (results recorded separately)

| Area | Required evidence |
| --- | --- |
| PostgreSQL positive path | At least 3 approved samples in one JSON and JSONL batch; exact manifests, record/item/artifact hashes and counts |
| Direct API / collection scope | Reviewer/Annotator, cross-project/org IDs, missing IDs, disabled account, removed org/member, creator-implicit Manager, superuser without project scope, nested release/artifact substitution |
| Authority fields / integration | Spoofed actor/reviewer/mode/config/snapshot rejected; Core-only denies every new batch route including old links after policy change; existing authorized ordinary internal reads still work, no auto-approval/reviewer grant |
| Approval / integrity | Pending/rejected/superseded, forged approved status without review, review mismatch, deleted review, corrupted snapshot/hash, changed XML, legacy Python hash seed differences, nonfinite numbers, duplicate ID and two revisions of one assignment |
| Historical source | New pending/approved revision and mutable task/annotation/prediction/config edits never replace selected bytes; source deletion fails closed; frozen evidence survives source FK cascades; reassignment preserves source identity |
| Revocation timing | Before freeze; queued creator removal/disable; removal during worker before READY; wrong current org; reader/creator revocation before GET/HEAD/Range/conditional request; no unauthenticated private-storage path |
| Retry / concurrency | Duplicate HTTP response loss, key/body conflict, concurrent keys/quota, duplicate RQ delivery, queue unavailable, enqueue-after-commit failure, worker crash/lease expiry/stale completion, artifact finalization/audit failure and orphan cleanup ownership |
| Scaling / download | Hard size/count/time limits, chunked memory usage, no queue-to-sync fallback, exact Range resume/ETag/length, corrupt/truncated files, file replacement/path traversal, no READY for partial output |
| Format capability | Probe actual frozen-config supported_formats; at least one real standard fixture, exact source-to-output/count mapping, frozen config/SDK provenance, unsupported formats/mixed schema/media access rejected |
| Bilingual browser | en-US/zh-CN preview, page selection, revisions, failure/retry, creator/member revocation with stale page, progress and native + actual standard download; existing single release and mutable-export denial regressions |
| Upgrade / rollback | Existing authorization negative matrix retained; additive migrations forward/reverse on disposable PostgreSQL; source deletion protection, routes-off rollback retaining receipts; PR CI, merge/main CI and runtime acceptance remain separate |

## 12. Confirmed scope

The user confirmed this bounded #79 design: (1) exact selection with one
revision per assignment and separate same-task samples; (2) explicit server
allowlist/default-disabled batch API and strict Core-only separation; (3) private
async jobs with frozen-copy/protected-project retention and limits; (4) the local
manifest schema/digest rules, references-only media and actual SDK capability
gates, with #80 owning cross-system versions and durable media.

Implementation/testing authorization does not grant merge, deployment or Core integration authority.
