# Issue 145: version-pinned deletion preparation

## Product outcome and current boundary

The requested outcome is to remove the specific pinned object version without
making an older object version visible. A newer replacement must not be removed
or hidden. Adding a delete marker to a key with a newer replacement would hide
that replacement; permanently deleting the current version can expose an
older object. Neither action is a safe general substitute for the other.

The current `check_s3_deletion_target` is a **read-only, non-authoritative observation**.
It does not dispatch a delete, create an execution claim, send `DeleteObject`,
or make an unknown request terminal. It verifies the
recorded AWS owner, storage binding, exact version and export token, inspects
the available history, and reports `automatic_execution: BLOCKED`. The result is
always non-snapshot and does not prove #144 isolation or authorize deletion.
It also reports Object Lock as unverified; reading a version does not prove it
can be permanently removed or that bypassing a retention policy is authorized.
After provider reads, the command checks that the pending database record still
has the same state, project, revision, storage and target fields. A changed
record is rejected. This recheck does not lock AWS or detect every ABA change,
and cannot establish a stable history or isolation.
An observed noncurrent target preserves the observed current version only if
history stays stable. A current target with other object versions is flagged
because removal might reveal an old object; a current target without other
observed objects is also blocked because in-flight requests can change history.

**Legacy behavior changed:** current code rejects key-only S3 annotation
deletion even when `ANNOTATION_STORAGE_ADMISSION_ENABLED` is false. If a
storage has `can_delete_objects` enabled, deleting the annotation now fails
and preserves its source transaction instead of issuing an unversioned DELETE.
When external deletion is disabled, annotation deletion remains available.
Already deployed old binaries retain their old behavior, so this change does
not isolate those binaries or replace #144. Other legacy pre-delete handlers
may already have performed external effects before this rejection; a database
rollback cannot undo them. This is not a cross-provider transaction or a full
legacy side-effect fence. Enabling the admission flag requires the
deployment-specific lifecycle, worker and cloud isolation prerequisites.

## Dormant durable worker

The version-pinned worker and bounded replay command are implemented but their
first operation is an unconditional, non-configurable isolation gate. There is
no production flag, manual attestation or API to pass it; neither normal calls,
old queued jobs, nor the replay command can submit a cloud deletion today. The
existing general outbox timer does not dispatch these deletion intents.
Replacing the gate requires a separately reviewed #144 implementation which
holds a deployment-bound isolation guarantee across submission and recovery,
not merely a point-in-time observation or a user-provided boolean.

For isolated tests that replace this gate, dispatch reserves a durable epoch
before enqueue. A lost enqueue response leaves the intent queued; a later
reservation advances the epoch and makes an older job unable to claim it. The
worker locks project, key and operation, validates owner, credentials binding,
revision, exact key/version/token and unresolved same-key work, then commits a
unique pre-effect claim. Only after target and history preflight does it commit
the submitting phase and issue a single-attempt, version-specific DELETE with
expected owner. It refuses a current version when another object version could
be revealed. A noncurrent target never creates a delete marker or hides the
current replacement. Provider response validation requires a matching version,
HTTP 204 and a bounded request ID before recording completion.

Pre-effect recovery requires a project manager, reason and matching claim; it
records an audit row, advances the dispatch epoch and fences the old claimant.
It does not automatically dispatch while the gate is closed. Once submitting,
worker death, response loss, access denial, malformed receipts or a failed DB
write remain RUNNING or UNCERTAIN and cannot be automatically replayed or
reconciled by this code. A future solution must verify the original request's
terminal outcome before releasing that blocker. Object Lock is not bypassed.
Existing rows are retained; no schema migration or automatic deletion is added.

The opt-in PostgreSQL state-machine tests and the synthetic MinIO tests can be
run on a Windows Docker host with the existing disposable fixture scripts:

```powershell
./scripts/issue86-isolated-postgres-recovery.ps1 -DependencyVenv <linux-venv-path> -TestVersionDelete
./scripts/issue86-isolated-legacy-worker.ps1 -DependencyVenv <linux-venv-path> -MinioClient <linux-mc-path> -TestVersionDelete
```

These fixtures replace the guard and, for MinIO only, the AWS endpoint check
inside the disposable test process. They use synthetic data and fixed loopback
services; their outcomes are not evidence of AWS identity, bucket-owner
enforcement, network isolation or provider request drain.

## Work required before execution

1. Pass #144 for the actual deployment: fence all old identities, sessions,
   processes, direct SDK clients, network paths, presigned requests, proxies,
   retries and multipart completion; account for every submitted operation.
   Unknown writers or requests block automatic deletion.
2. Preserve and validate the bucket owner, endpoint, storage binding, project,
   lifecycle generation, target key, immutable version and original export
   token. Changed identity, missing pins, null versions, wrong scope or
   unsupported providers must fail closed before provider mutations.
3. Serialize deletion against exports and other deletion intents, including
   cross-project aliases and unknown keys. Recheck the target immediately
   before submission. The database lock covers participating writers only;
   it is not an AWS lock and cannot replace #144 isolation.
4. Independently review the implemented claim, submission boundary, original
   request identity, receipt and recovery protocol against the actual #144
   isolation boundary. Do not blindly retry, mark success from absence, or let
   an SDK retry a delete implicitly. Post-submission reconciliation still
   requires a trusted original-request terminal-evidence mechanism.
5. Define and test current/noncurrent targets, replacements, delete markers,
   object lock, multiple intents, and failure between each durable transition.
   If the desired visibility cannot be guaranteed without mutating a replacement
   or revealing an old version, retain the pending operation for review.
6. Add server authorization, durable dispatch, monitoring, audit and rollback
   only alongside those gates. Run mixed-version, concurrency, loss-of-response
   and real isolated AWS acceptance before any production enablement. #145
   stays open; no real cloud evidence is asserted here.

The S3 API distinguishes [deleting a specified version](https://docs.aws.amazon.com/AmazonS3/latest/userguide/versioning-workflows.html)
from creating a delete marker. [Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)
can prevent permanent removal of a protected version.
