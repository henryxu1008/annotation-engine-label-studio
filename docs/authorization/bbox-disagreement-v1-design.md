# #81 bbox/IoU V1 design (in progress)

This document defines a proposed bounded Fork-only slice. It is not an
acceptance record, deployment authorization, or a claim that #81 is complete.

## Scope and immutable media

Compare independent immutable Submission revisions of the same task and exact
label configuration. The supported control is one static single-label
RectangleLabels on one static Image. V1 is limited to axis-aligned rectangles:
image rotation and region rotation must both be zero. Unsupported configuration,
missing/invalid/cancelled results, mismatched inputs and unverifiable image bytes
are unavailable, not a zero score or a fabricated agreement.

Submission snapshots freeze task data and configuration but not externally hosted
image bytes. A matching URL, current ETag, or user-supplied digest therefore does
not establish image identity. V1 targets project-owned uploaded images and configured
object storage. A trusted server-controlled preparation step must capture a bounded
private immutable copy before any participant may access or annotate the task, then
bind the exact source, image hash, media type and dimensions to the task and each
subsequent submission. The editor and all other media reads for such a task must use
the pinned copy, with live scope checks. The comparison consumes the captured bytes
and bound immutable evidence, never an arbitrary request URL or a mutable current
object. Existing version-aware storage adapters may be used only after their actual
version/conditional-read and bounded-stream contracts are established. `#80` media
pins are captured after release selection and explicitly do not prove which bytes
were seen at annotation time; do not substitute them for the pre-annotation pin.

Pre-existing submissions without a trusted pre-annotation media binding are
unavailable. No historical snapshot or decision is rewritten. The first supported
object-storage types, activation/migration and exact media read/authorization
paths require further implementation and negative tests. The worktree contains
an opt-in experimental managed-upload, task-linked GCS and task-linked S3 path,
including private capture, authenticated reads and transactional submission
proofs; it is not enabled by default and is not an acceptance or deployment.
Rotated boxes, segmentation, video and multi-label boxes remain outside V1.

The current worktree is not ready to enable: legacy upload, LocalFiles and
task/project storage proxy reads have preliminary guards for pinned originals,
but the complete endpoint and concurrency audit is not finished. Expired or
abandoned candidates have a durable lease, fenced publication and repeatable
`reconcile_bbox_media` management command; operators must schedule that command,
and retention/deletion for already published artifacts is not implemented.
Bbox quality batch and local arbitration API prototypes now persist immutable
evidence and have initial server-side access checks, but their shared-workspace
UI integration, complete negative-path coverage, concurrency and browser
acceptance remain unfinished. The local storage tests substitute the trusted parent root, so they
do not establish that any actual Windows or shared deployment meets its ACL and
namespace requirements. PostgreSQL and SQLite tests do not replace hosted CI,
browser tests, provider integration, or Host acceptance.

The legacy-source guards deny a pinned original even when another task or project
references it; equal S3 bucket/key names on separate endpoints are also blocked
conservatively. Callers must migrate those tasks or accept the denial. Existing
presigned URLs issued before a pin cannot be revoked by this
fork, and local tests do not prove what an annotator actually viewed. The guards
also query JSON source evidence on media reads; index and load-test the lookup
before enabling it at scale. Do not claim that blocking the current server routes
revokes earlier third-party credentials, cached content or already-issued URLs.

### Candidate reconciliation deployment requirement

The cleanup command is a bounded one-shot sweep. The optional systemd timer in
`deploy/bbox-reconcile/` runs the existing Compose `app` service as a one-shot
every five minutes, so its environment and volumes come from the deployment's
Compose configuration. Install the service and timer under the same names,
copy and fill `/etc/label-studio/bbox-reconcile.env` from the example, run
`systemctl daemon-reload`, then enable the timer. Inspect the first service run
and timer status before treating cleanup as operational. A deployment using a
different orchestrator must schedule `python label_studio/manage.py
reconcile_bbox_media --limit 50` with the same database, private storage mount,
namespace binding and service identity as the writer. Monitor both the nonzero
exit status and the `checked`, `removed` and `failed` counters; size the interval
and limit for the candidate rate. Tombstones are deliberately revisited to catch
late writers, so a successful empty deletion does not mean the database backlog
can be discarded. The command selects only the namespace identified by its
validated local marker, so each isolated namespace needs its own schedule. A
sweep against the wrong mount is not evidence of cleanup. The repository has not
installed or verified a schedule in any running environment, and
the synchronous job fallback must not be used as a delayed recurring scheduler.

### Preparation and read boundary under design

For an explicitly enabled project, a Manager prepares each eligible task before
its first assignment or annotation. The server resolves the image field against
that project's exact managed upload or configured import-storage binding, streams
one bounded object into a private immutable copy, hashes and validates it, then
atomically binds the copy and source evidence to the unchanged task/configuration.
Do not hold database locks across remote I/O. Recheck project permission, task,
source binding, generation and absence of prior work before publication; on
uncertain publication retain the operation and artifacts for reconciliation.
Reject ambiguous storage matches, arbitrary external URLs, caller-supplied paths
or digests, redirects, oversized/truncated streams and unverifiable source data.

Every supported task/editor/media route must serve the exact pinned bytes with
current task and project authorization, and revalidate the binding at submission.
Old browser state, assignment changes, task edits, reimports, direct APIs and
storage downloads must not silently fall back to the mutable original. Do not
activate this workflow for an existing task whose previous access or submissions
cannot be ruled out. Compare only submissions carrying a verifiable reference to
the pre-existing immutable binding; the pure comparator is not that proof.

The existing `data_export.snapshots.upload_for` supports local managed uploads;
the existing storage adapters provide streams for S3/GCS/Azure but do not by
themselves establish immutable annotation-time provenance. Their buffering,
timeout, identity and failure semantics require per-provider review. Pinning is
not an excuse to add an unbounded generic HTTP fetch or bypass current media ACLs.
In particular, the current GCS adapter reloads blob metadata separately from the
download without binding that download to the reloaded generation. The worktree
also fixes a self-recursive replacement of the response's `close`, with a
regression test. The adapter is still not suitable as-is for the pinning contract:
version consistency needs a provider-specific path and tests before delivery.
Google Cloud Storage's documented generation preconditions support a
single-version read; merely retaining metadata from `blob.reload()` is insufficient.
See https://cloud.google.com/storage/docs/request-preconditions. The experimental
`capture_gcs` uses the existing SDK reader with the reloaded generation as a
precondition for every underlying read, with no retry or fallback. The experimental
path binds this capture to the task's own configured GCS link, private artifact
and submission evidence; provider tests use SDK mocks and no real GCS was used.
The generic capture primitive rejects partial-response metadata including the
proxy's synthetic Content-Range. S3 uses a bounded single response and the
captured content hash, not an ETag as proof of byte identity.

### Durable proof and race boundaries

An immutable task media record needs a generation ID, exact project/task and
image field, frozen task/config hashes, trusted source-binding identity, private
artifact ID, content hash/size/format/dimensions and preparation actor/time.
The private source identity is server-derived; the public comparison must not
disclose credentials or private paths. A separate immutable per-Submission proof
must bind that generation and media hash to the exact assignment, Submission ID,
revision and result hash in the same transaction that creates the Submission.
The normal `result_snapshot` format and old hashes remain unchanged. Never infer
the proof retrospectively from the current task, current object or a client field.

Use a task/assignment generation fence for preparation, assignment, edits and
submission. A task whose data or config changes after preparation must stop using
the old pin; do not silently replace it. `BaseTaskSerializer` can bypass URI
resolution with `resolve_uri=false`, and `TaskSimpleSerializer` does not resolve
at all. The new contract must cover those routes, project next-task, data-manager
and all direct and bulk paths rather than only patching `Task.resolve_uri`. The
artifact endpoint must re-check current project,
task and assignment authority, enforce GET/HEAD/Range behavior and revalidate the
opened file against the pinned hash before releasing data. A UI-only rewrite is
not sufficient. Existing upload, storage-resolve and project proxy endpoints must
also be assessed so an authorized annotator cannot bypass the pinned artifact.
Stale pages and tokens must fail at the authoritative submit path.
These facts can show which bytes the service made available, not prove what a
person actually saw or prevent an authorized user from viewing another source.

## Comparison contract

Coordinates are finite decimal percentages in [0,100]; width and height are
positive and rectangles lie wholly in the image before quantization. Coordinates
are rounded to millionths of a percentage point using round-half-even; a box that
collapses after rounding is unavailable.
One box has one static configured label. Duplicate same-label rectangles are
unavailable. IDs and source order do not supply identity for matching.

For each unordered pair of independent participants, form edges only between
same-label boxes with IoU >= 1/2. Match one-to-one, prioritizing maximum match
count and then maximum sum of IoUs quantized downward to millionths. Sort input
boxes canonically and use pinned COCO API 2.0.11 for IoU and Munkres 1.1.4 for
assignment. Independently verify COCO's bounded float output against exact integer
geometry, failing closed on disagreement; use the exact fractions for threshold
and provenance. The engine versions, tie handling, work limit and quantization are
part of the rule.
F1 is the reduced fraction 2M/(|A|+|B|). An empty side is unavailable: the
existing snapshot normalizes both null and an empty result to [], so historical
evidence cannot prove that an empty result was deliberately submitted. Fewer
than two inputs are likewise unavailable. Preserve all participant pairs;
do not infer an overall score or transitive consensus. Every matched and unmatched
box must remain explainable against its frozen source revision.

## Security and delivery

Reuse the existing project/task scope, bounded immutable source loader, current
identity and server-side policy, final authority/source checks, frozen evidence,
idempotency and arbitration invariants. Annotators must not read peers' answers;
reviewer self-exclusion applies to the whole frozen cohort, including stale pages,
API, batch, arbitration, bulk, export and media paths. No client-supplied actor is
authoritative. Core-only-review must not acquire Fork review or release authority.
Implement and test negative paths before enabling any new endpoint or UI.

Host retains business review, certification and release authority. Host evidence
admission is a separate contract, not an automatic result of Fork arbitration.

## Acceptance required before merge

- Golden two/three participant cases, IoU boundary and competing matches,
  order invariance, tie replay, duplicate boxes, invalid/empty/rotated input,
  malformed and changed image/configuration, and bounded work.
- Frozen inputs, exact revisions and hashes, idempotent replay, unavailable source
  and fail-closed budget/engine failures; old snapshots and decisions unchanged.
- PostgreSQL/SQLite, role and revocation negative paths, direct API and stale UI,
  forged actor/IDs, cross-project, media/download/bulk/export paths and two-language
  real browser workflows for every delivered write/read feature.
- Immutable arbitration decision with actor/time/reason/source revision and hash;
  no automatic release or Host business approval. Explicitly distinguish what is
  implemented, exercised on the final commit, deferred and not run.
