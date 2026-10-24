# Durable text classification queues (#81)

An opt-in Fork analysis batch freezes an explicit set of tasks, participants,
Submission identities and classification results. The queue is read-only with
respect to annotation, assignment, review and arbitration. Only creation of the
analysis record writes new data. #81 and #84 remain open.

## Workflow and source selection

Reuse Data Manager's modal, locale controls and classification result display:
list → separate create/preview → frozen batch → filtered queue → exact revision
comparison. Returning preserves the queue filter/page and focus. The existing
manual single-task comparison remains available with its original contract.

Preview accepts a name and either `scope={kind:project}` or
`scope={kind:tasks,task_ids:[...]}`. Project scope enumerates tasks once; its signed
preview fixes that exact task list. New tasks are not silently appended. Do not
interpret the current Data Manager page/search as task scope.

Selection `active-assignment-latest/v1` includes every active Assignment on each
selected task, choosing its greatest revision independently. Never compare
revision numbers across assignments, select two inputs from one participant, or
fall back from a malformed/skipped/latest-unusable revision to an older approval.
Pending, approved and rejected latest revisions are eligible; review status is
not a classification metric. A participant with no Submission remains a missing
input. Fewer than two independent participants or any missing Submission makes
the whole task unavailable. Cancelled assignments do not enter new batches;
cancellation after freezing does not erase intact historical evidence.

Reuse `classification-exact-sets/v1` from classification-disagreement-v1.md:
inline Text without resolvers, static global Choices, exact frozen XML and task
data equality, unordered participant pairs and per-control set equality.
Unsupported configurations/missing values are unavailable, never perfect scores.
Invalid snapshot hashes or cross-scope source graphs reject preview/creation.

## Persistent boundary

ClassificationBatch stores a versioned manifest, SHA-256, creator principal and
actor pins, request hash and unique project/UUID key. The manifest fixes the
task/assignment/assignee cohort (including missing inputs), exact
Submission/revision/submitter/result/config/task-data hashes, rule versions,
enumeration/calculation times, outcome/reason, integer pair counts, control
locations and calculation hash. No complete text, raw snapshot or answer sets
are copied. Authorized detail reads recover answers only from verified exact
sources and check the frozen calculation hash.

PostgreSQL/SQLite UPDATE triggers guard the primary key and all frozen fields, including bulk/SQL
updates. Creator FK may become null but cannot be rebound; frozen creator pins
remain. Source deletion does not cascade to the batch. Existing project deletion
cascades still apply: this is durable local work history, not permanent audit
retention or a delivery Manifest. No update/delete API or editable admin exists.

Input limits are inclusive: 100 tasks, 20 active participants per task, 2,000 total
participants, 8,388,608 cumulative canonical snapshot JSON bytes. Reject the whole
request with 422; do not truncate, sample, split automatically or enqueue jobs.
Snapshot loading uses a one-row iterator and checks canonical bytes before
retaining each row or decoding later rows. It never reads live Task.data/meta or
review comments, which are not frozen inputs. A single snapshot must still be
decoded to measure its exact canonical bytes; this is not a wire-size preflight.
If changed live sources exceed the frozen budget on a read, hide the answer set
as unavailable and stop loading later sources.

## API and idempotency

Under `/api/projects/{project}/classification-disagreements/batches/`:

- GET `capability/`: current read/create capability, versions and limits.
- POST `preview/`: no database writes; returns statistics, exact task IDs,
  plan_hash and an independently salted signed preview (15 minutes, 16 KiB cap).
- POST collection: accepts only preview_token/expected_plan_hash plus UUID
  Idempotency-Key; creates once or recovers the original request.
- GET collection and `{batch}/`: visible metadata and current display counts.
- GET `{batch}/items/?state=all|agreement|disagreement|unavailable`: stable task-ID
  order, page/page_size (50 default, 100 maximum); default disagreement filter.
- GET `{batch}/items/{task}/`: fixed sources and read-only comparison.

Strict inputs reject actor/reviewer/result/status fields, duplicate/foreign task
IDs, unknown queries and ambiguous duplicate parameters. Creation reuses the
Project → live membership/User/ProjectMember → Task → Assignment → Submission
lock order. Rebuild and compare the plan under the project fence; changed
participants/revisions require a new preview (409). Keep the transaction through
response rendering and final actor/mode/operation/source revalidation. No partial
batch or building/failed ledger is persisted.

Same key/request/principal/actor returns the original batch (200, replayed), even
after preview expiry, key rotation, new revisions or source deletion. It returns
metadata only and does not substitute a new computation. Different request or
identity is 409. Current create authority and manifest integrity are always
required. New creation returns 201 with its matching idempotency key.

Unknown UI acknowledgements retain the original request/key for explicit recovery
within the open workspace. Reading/reloading the queue never repeats creation.
Confirmed creation remains visible as a saved notice even if later reads fail.

## Authority and live availability

Require live trusted session/token actor, current organization/project membership,
Manager/Reviewer role and READ_WORK. Manager creation additionally requires the
independent ORGANIZE_CLASSIFICATION operation. Legacy policy offers it only when
the new feature is enabled; custom/#86 providers must explicitly allow it. It is
not REVIEW, ORGANIZE_REVIEW or release permission.

Reviewer access excludes the entire frozen task when its cohort contains that
actor as assignee or submitter. Apply this before SQL pagination/counts and object
reads; do not silently recompute with fewer people. Reviewer receives visible
subset counts/projection_hash, never full manifest/hash or hidden totals. Batches
with no visible tasks are absent from the list and return 404. Annotators and
foreign/revoked scopes have no batch access. IDs, keys and signatures grant none.

Repeat identity resolution after materialization/rendering and bind the same
principal, local actor and role, including same-role remapping. Repeat source and
manifest checks; discard late inconsistent results. Responses are private/no-store
and no-referrer. UI generations clear evidence on scope change, blur or failure.

Stored results never change. `observed_at` and `display_counts` describe current
authorized availability: a missing/corrupt source masks scores, pins and answer
locations as unavailable without dropping that task or selecting a replacement.
Later completion cannot upgrade an originally unavailable batch. Manifest
corruption rejects the batch. New submissions/legitimate review status changes
do not change intact frozen results or manifest hash.

## Activation, migration and validation

Default ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=false; also require the existing
classification feature flag and exact org:project `fork_review` mode. Unknown,
missing and Core-only modes deny every new route, including original-key recovery.
Other database vendors reject activation because no frozen guard is installed.

Registering the new app exposed an upstream migration-order dependency: projects
0028 writes core.AsyncMigrationStatus without declaring its schema dependency.
Declare core.0001_initial there, before projects 0028 can run; fresh PostgreSQL and
SQLite migration tests exercise the actual order. Existing applied migrations
are not re-run. Do not work around this with a disabled migration or pre-created
table; preserve the dependency during upgrades.

Tests cover durable freezes, source/identity tampering, no latest fallback,
missing participants, true 100/101 and 20/21 limits and exact 8 MiB/+1 bytes,
viewer-specific counts, independent operation withdrawal, original-key recovery,
serialization rollback and real PostgreSQL lock/read interleavings. Separate
synthetic users/projects support en-US/zh-CN browser acceptance, including a
committed response whose acknowledgement is deliberately lost then recovered.

This feature does not write ReviewDecision, arbitration, sampling rules,
Task.precomputed_agreement, release/export policy or Host data. Existing Core
internal reads retain their contract; this feature does not enable integration.
No retained environment is activated by migrations, tests or default settings.
