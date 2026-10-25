# Authorization integration plan

## #84 local review sampling (first slice)

[Review sampling v1](review-sampling-v1.md) freezes deterministic bounded work
plans over exact existing Submissions, reuses the existing ReviewDecision/Editor
rework chain, and keeps unchecked work outside approved release authority.
Preserve project-first decision/create fences, serialized-source hash checks,
frozen plan database guards and current actor/scope checks across upgrades.
It defaults off and requires explicit per-project Fork-review activation.
Progress/follow-up UI derives counts from the authorized detail projection and
keeps frozen original decisions separate from live successor status. Preserve
the saved-write/read-failure distinction and batch/project response guards when
upgrading ReviewerWorkspace; progress retries must never resubmit review commands.
Core-only-review, Host policy/implementation, #81 arbitration, #86 lifecycle and
retained environment activation are outside this slice.

The isolated real Fork → Host RAW trial uses the Python 3.11 API/RQ image in
`deploy/fork-host-live/Dockerfile`, one editable source root, dedicated synthetic
PostgreSQL/Redis/export storage and explicitly enabled organization Token policy.
Release creator, snapshot creator and fetch account remain independently subject
to current Manager checks. Token is not a read-only project credential. Host
enforces source namespace + Submission ID globally across connections; connection
replacement cannot bypass a frozen source conflict. Core-only-review, #76 and
#307 environments/credentials remain outside this trial's write set. No deployment,
Core review, Gold/certification, publication, delivery or deletion is authorized.
Synthetic control files are not an authority: validate their marker, canonical
namespace against operator policy, and exact organization/project/active-user
membership graph before changing a role or Token policy. Lock that graph and
require exactly one membership update. Create credential files exclusively as
0600 before writing any bytes; never rely on a later chmod or overwrite a file.
The initial seed also requires empty user/organization/project/task/annotation/
assignment tables, including soft-deleted projects and unscoped retained rows.
Hold PostgreSQL table fences through seeding so an uncommitted ordinary writer
cannot cross that emptiness check. Both fixture and policy files use exclusive
creation; existing files and symlinks are refused before database writes.
The policy basename is reserved so credential and policy destinations stay distinct.

Issue #80 adds an independently frozen [ReleaseManifest v1 source snapshot](release-manifest-v1.md)
on top of #79 READY receipts. Preserve its exact source/media hashes, both creators'
live authority, PostgreSQL freeze guards and Core-only denial across upgrades.
This is source evidence, not a Host DatasetVersion, Core review or business publication.
Keep the shared v1 XML/media profile grammar and the synthetic Host graph checks
in sync: hashes do not replace native/member/media scope and completeness checks.
Existing Host bindings must fail closed on copy loss or corruption, including
when a different operation key requests the same mapping. Real Host enforcement
and acceptance remain outside this Fork's synthetic contract tests.
Queue recovery uses `queued_at` from the latest successful enqueue, not request
creation; migration 0014 preserves READY evidence and retains the rollback fence.
History/object source checks quarantine shared corrupt source evidence and all
READY dependents; a private media failure remains snapshot-scoped. The UI must
remove stale READY actions after integrity failures, as well as authority denial.

## #79 approved Submission batch export (merged PR #98)

The [approved Submission batch export design](approved-dataset-export-design.md)
was confirmed against `main@88809e61`. The [local implementation](approved-dataset-export.md) adds a separate,
default-disabled project-scoped batch path, exact approved immutable inputs,
server-side authority checks at freeze/job/download boundaries, private artifacts
and a local frozen manifest. It preserves existing mutable export/storage guards
and keeps Core-only-review ordinary internal acquisition separate from external
Fork-approved export. Host DatasetVersion and cross-system ReleaseManifest remain
owned by #80/Host. PostgreSQL/Converter/browser acceptance is recorded separately.
Local implementation authorization grants no merge, deployment or Core integration
authority. Frozen receipts also retain the legacy mutable-delivery boundary if
source Submissions are later deleted.

The [PR #98 security review](issue79-pr98-security-review.md) records the
shared organization-authority, manifest/artifact graph and publication/revocation
race fixes. Shared project-role resolution checks active organization membership
and local account state before ownership or a retained project role can grant
authority; keep legacy review/release from bypassing organization revocation.
The selected UI organization is not a background job's authority. Keep the complete
sealed inventory, attempt/private-key binding and same-request metadata snapshot
checks across upgrades. Freeze/publication hold the Project fence and local
authority rows through commit; lock organization membership before User to match
soft deletion. Verify real PostgreSQL revocation races, not only mocked stale
permission checks. These local authority locks do not define an external identity
provider's consistency protocol or authorize Core integration.

Reserve the configured approved-export filesystem root from generic LocalFiles
configuration, scans, JSON/blob imports and HTTP media delivery even with release
policy disabled. Resolve symlinks, check real directory containment instead of a
lexical prefix, and deny private paths before conditional/Range processing. An
import storage owned by another project is not an artifact download capability.

## Objective

Use Label Studio Community Edition as the annotation execution engine while the host platform remains the source of truth for business identity, project membership, task assignment, review policy, and release decisions.

The implementation must support controlled multi-user annotation without assuming that Label Studio CE's default authenticated-user model provides project/task isolation.

## Architecture boundary

```text
Host platform
  ├─ identity / tenant
  ├─ project membership and roles
  ├─ task assignment
  ├─ review policy
  ├─ immutable submission metadata
  └─ release / dataset version
          │
          │ trusted identity + authorization decision
          ▼
Label Studio CE fork
  ├─ labeling UI
  ├─ task rendering
  ├─ drafts / annotations
  ├─ predictions
  └─ server-side enforcement adapters
```

The host platform owns policy. The Label Studio fork owns enforcement at Label Studio resource boundaries that cannot safely be protected only from outside the process.

### Controlled Core-only-review Pilot: submission protocol v1

The dedicated, finite synthetic DPP Pilot uses the user-confirmed C1 policy: Core is the sole business reviewer. A formal Submission is an immutable source for authorized internal acquisition, not a Core review decision, Gold certification or delivery authorization. This is an additional host-integration mode; the existing fork reviewer workflow and historical DPP #205/#208 official CE acceptance remain valid in their original scope.

The server-side adapter uses ordinary `GET /api/submissions/?project=P&page=N&page_size=100` and `GET /api/submissions/{id}/` under the existing authenticated organization/project permissions. Internal acquisition does not require fork approval. It must not use `reviewable=true`, `POST .../review/` or `GET .../release/` as substitutes, grant itself Reviewer authority, or auto-approve on a Core decision. The dedicated Pilot does not run a second fork human review; unexpected fork human review is recorded and quarantined as a mixed workflow. Automatic superseded revisions remain source history.

This authorized ordinary read is distinct from fork release/export delivery. `/release/` still requires an effective Manager and an approved exact Submission; mutable export/storage delivery after formal Submission remains denied as specified below. Core independently authorizes its reviewers and retains its existing Gold production, certification and CurrentDeliveryGate. Internal reads cannot be exposed as a substitute public download/delivery path.

The versioned adapter must be verified against B's fixed [#72 handoff](https://github.com/qq550723504/annotation-engine-label-studio/pull/72): documentation merge commit `ab7b76a4a36b19060c527659e2a994ead05cc5e8`, [candidate handoff](https://github.com/qq550723504/annotation-engine-label-studio/blob/ab7b76a4a36b19060c527659e2a994ead05cc5e8/docs/authorization/issue48-release-candidate.md), [manifest](https://github.com/qq550723504/annotation-engine-label-studio/blob/ab7b76a4a36b19060c527659e2a994ead05cc5e8/docs/authorization/issue48-rc48/manifest.json), [assertion receipts](https://github.com/qq550723504/annotation-engine-label-studio/blob/ab7b76a4a36b19060c527659e2a994ead05cc5e8/docs/authorization/issue48-rc48/assertions.json) and [recorded Dockerfile](https://github.com/qq550723504/annotation-engine-label-studio/blob/ab7b76a4a36b19060c527659e2a994ead05cc5e8/docs/authorization/issue48-rc48/Dockerfile.recorded). The image source remains `90153bb6450a160ed6a1a9129adce65b7c4b42f8`, with fork version `1.23.0+fork.rc48.90153bb6` and local-only reference `annotation-engine-label-studio@sha256:d0876462957eebd2608223c4af6ec5f894eae7008f2fb3e236124dca5c3d36ce`; the documentation merge commit is not the image source. The manifest distinguishes OCI index, platform manifest and image config digests. This candidate exists only in the builder's local Docker image store and has not been published to a registry. Its synthetic service-side PASS does not complete cross-repository integration or the user-deferred trusted-browser acceptance. This three-document contract is design-admitted; implementation, image distribution, deployment and starting D remain unauthorized. Source identity is the controlled connection/instance namespace plus source kind and Submission.id; task/project, assignment/revision, content hash, author and configuration are fingerprint assertions, not extra identity components that legalize same-ID conflicts. The host must freeze trusted submitted_by, assignment/revision, snapshot/hash and mapping/config/normalizer provenance, atomically bind accepted Results, and verify all replay and snapshot-integrity paths under the authoritative [DPP source contract](https://github.com/qq550723504/data-product-platform/blob/main/docs/architecture/annotation-domain.md) and [adapter mapping contract](https://github.com/qq550723504/data-product-platform/blob/main/docs/architecture/annotation-engine-integration.md).

This mode adds no fork reviewer or release bypass. Assignment versions remain write tokens; the current API does not prove the token at submission time. The first Pilot uses drained, finite writes followed by quiescent full pagination and exact-ID reads; it does not promise complete enumeration during continuous writes. This paragraph specifies a design contract, not completed cross-repository acceptance or deployment approval.

## Phase 1 — trusted identity

Target branch: `feature/platform-auth`

Requirements:
- Map a platform user to a distinct Label Studio user.
- Keep service-account identity separate from the human actor.
- Derive annotation author/update actor from trusted server-side identity.
- Do not trust client-supplied `completed_by`, `updated_by`, reviewer, or equivalent actor fields.
- Define session revocation behavior.

Exit criteria:
- Two users produce distinguishable audit identities.
- Actor spoofing through request payloads fails.
- Revoked access cannot continue performing protected writes after revalidation.

## Phase 2 — project authorization

Target branch: `feature/project-rbac`

Requirements:
- Apply membership scope consistently to project list, counts, detail, tasks, annotations, imports, exports, storage operations, bulk operations, and relevant file/media paths.
- Use the same authoritative project membership source across endpoints.
- Default deny when project scope is unknown.
- UI visibility may mirror permissions but must not replace server checks.

Exit criteria:
- A user outside Project B cannot read or mutate Project B by changing identifiers or calling APIs directly.
- List/count/detail endpoints expose a consistent project set.
- Related file/media access obeys the same project boundary.

## Phase 3 — task assignment and annotation ownership

Target branch: `feature/task-assignment`

Recommended platform model:

```text
Assignment
  project_id
  task_id
  assignee_id
  status
  version
```

Requirements:
- Task retrieval and next-task selection honor active assignment.
- Draft save, create/update annotation, skip, submit, reassignment, and revocation all validate assignment.
- Annotators cannot mutate another annotator's result.
- Reassignment invalidates the prior assignee's future writes.
- Use optimistic/version checks to prevent stale pages from silently overwriting newer work.

Exit criteria:
- User A cannot access or modify a task assigned only to User B.
- User A cannot modify User B's annotation.
- Writes from stale or revoked assignments are rejected.

## Phase 4 — review and immutable submissions

Target branch: `feature/review-workflow`

Requirements:
- A formal submission creates an immutable revision/snapshot identity.
- Review permissions are separate from annotation-edit permissions.
- Approve/reject binds reviewer, time, reason, and exact revision.
- Rejection requires a reason.
- Editing after approval creates a new revision that is not automatically approved.
- Generic annotation APIs cannot set authoritative review fields.
- Fork release/export delivery gates use approved immutable revisions only. The authorized internal ordinary-read path for the controlled Core-only-review Pilot is separate and follows the integration mode defined above.

Exit criteria:
- Annotators cannot forge approval or reviewer identity.
- Approval always refers to an exact revision.
- Changed content cannot inherit approval from an older revision.

## Negative authorization matrix

The labeling toolbar uses the hidden Data Manager `next_task` action. That read
must accept effective Annotators as well as Managers, and intersect prepared
filters/selected IDs with the authenticated user's existing task-assignment
queryset before sampling. Reviewer, revoked membership, cancelled assignment,
unassigned and cross-project task IDs remain denied. Submitted actor fields do
not select the queue owner. Task locks do not replace this assignment check.
All other Data Manager actions retain the Manager mutation gate. Queue request
errors are displayed as recoverable errors in the chosen language; only an
explicit `404` with `display_context.reason=QUEUE_EXHAUSTED` is presented as
"no tasks currently available". A project/view 404 after revocation is an error,
and unavailable work or locks must not be reported as all tasks completed.

PostgreSQL candidate validation must include the existing submission/review,
assignment and composed collaboration tests as well as session tests. #76
reproduced PostgreSQL rejecting `FOR UPDATE` over nullable annotation and
submitted-by joins in the submission services. Those unused joins are excluded
from the lock queries; assignment/project serialization, actor checks, immutable
revisions and reviewer authorization remain covered by the same tests. SQLite
alone cannot detect this database compatibility failure.

| Scenario | Expected |
|---|---|
| User changes project ID to unauthorized project | Denied |
| User changes task ID to another assignee's task | Denied |
| User changes annotation ID to another user's annotation | Denied |
| User supplies another user's `completed_by` | Ignored/rejected; actor remains authenticated user |
| Removed project member submits from an already-open page | Denied after required revalidation |
| Task is reassigned while old page remains open | Old assignee write denied |
| Annotator submits review fields through generic annotation API | Ignored/rejected |
| Reviewer approves own submission when policy forbids self-review | Denied |
| User directly requests unauthorized uploaded media | Denied |
| User attempts unauthorized export/bulk action | Denied |
| Stale revision attempts to overwrite newer annotation | Conflict/rejected |

## Upstream maintenance

- `release/1.23-base` remains an unchanged reference for the 1.23.0 baseline.
- Product work happens from `main` through focused feature branches.
- For a future stable upstream release, create a dedicated upgrade branch, reconcile upstream changes, and rerun the full negative authorization matrix before merging to `main`.
- Avoid copying the archived third-party RBAC fork wholesale. Reuse ideas or narrow code patterns only after verifying them against the current upstream implementation.

## Deferred work

Do not block the first secure collaboration loop on:
- real-time co-editing of the same annotation;
- advanced blind-label consensus;
- complex performance scoring;
- full parity with Label Studio Enterprise;
- support for multiple annotation engines.

The first milestone is a secure flow for two annotators and one reviewer: assign → annotate → submit → review/reject → revise → approve → release.

### Organization membership candidate contract

Project member administration depends on `GET /api/organizations/{org_id}/memberships?active=true` returning only non-deleted organization memberships. In this fork, `active=true` is therefore treated as a stable authorization-adjacent contract and filters `deleted_at IS NULL` regardless of the upstream feature-flag rollout state. This prevents soft-deleted organization users from being offered as project-member candidates. Preserve this behavior across upstream rebases unless the project-member UI is migrated to another server-filtered candidate source.

Project member administration uses a dedicated lightweight capability probe at `GET /api/projects/{project_id}/members/capability/`, enforced by the same server-side `authorization.can_manage_project(...)` check as member mutations. Settings navigation must use this endpoint rather than loading the roster. The actual member list at `GET /api/projects/{project_id}/members/` is paginated (limit/offset) to avoid unbounded serialization for large projects.

The add-member selector uses `GET /api/projects/{project_id}/members/candidates/`, which is manager-authorized and server-filters active organization memberships to exclude every existing project member plus the effective creator before pagination. Client-side filtering is only defensive and must not be the source of uniqueness across roster pages.

### Task assignment eligible-assignee contract

Project task assignment administration depends on a fork-specific, manager-authorized helper endpoint:

- `GET /api/task-assignments/eligible-assignees/?project={project_id}`
- authorization: the authenticated principal must satisfy `authorization.can_manage_project(...)` for the requested project; project visibility alone is insufficient;
- eligibility is derived server-side and requires all of: `User.is_active=True`, a non-deleted `OrganizationMember` in the project organization, and either the effective project creator or an enabled project member with labeling access (`annotator` or `manager`);
- direct `POST /api/task-assignments/` validation must enforce the same active-user and active-organization-membership invariants and must never rely on the selector as a security boundary;
- the eligible-assignee response is paginated (50 by default, capped at 100) and the Data Manager assignment selector is a client of that paginated contract;
- preserve this endpoint, its authorization boundary, mutation-side invariant checks, and pagination across upstream rebases unless the assignment candidate source is intentionally redesigned.

### Reviewer queue and immutable review contract

Reviewer UI access depends on fork-specific, server-authoritative review endpoints:

- `GET /api/projects/{project_id}/review-capability/` is the lightweight capability probe used by Data Manager; it succeeds only when the authenticated principal has the effective `reviewer` role for that project.
- `GET /api/submissions/?project={project_id}&reviewable=true` is the authoritative pending review queue. It requires Reviewer role, returns only `pending` submissions, excludes submissions created by the authenticated reviewer, and is paginated (50 by default, capped at 100).
- `GET /api/submissions/?project={project_id}&history=true` returns non-pending submission history with the same bounded pagination contract.
- `POST /api/submissions/{submission_id}/review/` remains the authoritative mutation endpoint and revalidates Reviewer role, pending status, self-review prohibition, and required rejection reason at decision time.
- The Reviewer workspace must display `Submission.result_snapshot`, `revision`, and `result_hash` as the review subject. It must not substitute the current mutable Annotation state.
- If Reviewer membership is revoked while the workspace is open, subsequent queue/review requests must be denied and the client must clear stale review controls rather than treating UI state as authorization.
- Preserve the capability probe, reviewer-only queue semantics, self-review filtering, pagination, immutable snapshot contract, and stale-session enforcement across upstream rebases unless the reviewer workflow is intentionally redesigned.

### Approved immutable release contract

Manager release UI depends on the existing fork-specific release endpoint:

- `GET /api/submissions/{submission_id}/release/` revalidates effective Manager role for the Submission's project at request time;
- only `approved` Submission revisions are releasable; pending, rejected, and superseded revisions must fail;
- the release payload is derived exclusively from the selected immutable Submission and returns `submission_id`, `revision`, `result_hash`, and `result_snapshot`;
- the client must validate that the release response matches the selected immutable revision and must never substitute the current mutable Annotation;
- object-level authorization must reject cross-project Submission ID substitution even within the same organization;
- preserve this authorization and immutable-payload boundary across upstream rebases unless release orchestration is intentionally redesigned.

### Storage create pre-validation authorization

Storage create endpoints must authorize the requested project before serializer validation can perform provider-specific connection checks:

- import and export storage create requests read the requested `project` identity first;
- the project must be visible to the authenticated actor and the effective project role must be Manager;
- Annotator/Reviewer requests are denied before S3/GCS/Azure/Redis/etc. credential or connectivity validation;
- external provider errors must never replace the authorization denial for an unauthorized project actor;
- keep the existing `perform_create` Manager check as defense in depth.

This ordering is part of the fork authorization boundary and must be preserved across upstream rebases.

## Storage validation authorization ordering

Fork-specific storage validation and file-listing endpoints must authorize the submitted project before serializer validation or provider/filesystem probing.

Enforcement points:

- `/api/storages/*/validate`
- `/api/storages/*/files`
- shared `validate_storage_instance()`

For new configurations, validate the raw `project` identifier, resolve it within the request user's visible projects, and require project-manager authority before serializer validation. For existing storage IDs, authorize both the storage's current project and any submitted replacement project before serializer validation. This ordering prevents unauthorized users from triggering network, credential, or local-filesystem side effects before access is denied.

## Immutable review release boundary

Once a project has any formal `Submission`, mutable annotation export/storage delivery is fail-closed outside the approved immutable submission release path. This does not prohibit the authenticated ordinary Submission reads explicitly used for the controlled Core-only-review Pilot's internal acquisition. That read permission is not approval or release authority and does not reopen any delivery endpoint listed below. These boundaries must survive upstream rebases.

Required enforcement points:

- deprecated synchronous project export (`/api/projects/{id}/export`);
- export snapshot list/create/detail/download/convert (`/api/projects/{id}/exports/*`);
- legacy export-file listing and nginx auth check (`/api/projects/{id}/export/files`, `/api/auth/export/`);
- export-storage manual sync and automatic annotation delivery for S3, GCS, Azure Blob, Redis, and Local Files.

Mutable export-storage delivery and formal submission creation serialize on the project row. Automatic storage callbacks run after transaction commit, so the first formal submission is visible before delivery is considered. Approved immutable revisions are delivered through the submission release API rather than mutable project/export-storage paths.


### Immutable release and mutable export boundary

Once a project has any formal `Submission`, mutable Annotation state is no longer an authorized delivery artifact:

- `GET /api/projects/{project_id}/export` must fail closed;
- snapshot export list/create/detail/download/convert under `/api/projects/{project_id}/exports/` must fail closed;
- legacy `/api/projects/{project_id}/export/files` and nginx `/api/auth/export/` delivery must fail closed;
- configured export-storage synchronization and automatic annotation delivery must fail closed;
- export-storage annotation writes and formal submission creation serialize on the same locked Project row, so an in-flight mutable delivery cannot cross the first-submission boundary;
- S3, GCS, Azure Blob, Redis, and Local Files automatic export callbacks run only after the annotation transaction commits and must re-check the formal-submission boundary;
- post-commit storage failures must not make an already-committed annotation request appear unsuccessful;
- approved delivery must use `GET /api/submissions/{submission_id}/release/`, whose payload comes only from the immutable approved Submission snapshot.

Each immutable Submission snapshot must preserve the exact `label_config` plus its hash and must derive task/project identity from the locked Assignment, not mutable Annotation relations.

Preserve these enforcement points across upstream rebases unless the release/export architecture is intentionally redesigned.

## Required main merge gate

The fork's authorization and enterprise browser workflows are required checks for
every PR targeting `main`. Preserve their unconditional PR triggers, unique check
names, and default PR merge-ref checkout across upstream upgrades. Repository
rules must require both GitHub Actions checks, an up-to-date base, and a PR, with
force pushes and branch deletion blocked. See [main merge gate](main-merge-gate.md)
for the desired ruleset, application procedure, and acceptance boundaries.

## Server-authoritative browser session revocation

Preserve the database-only browser session backend, the independent per-user
security counter, its independent recovery-boundary record, and inclusion in
Django's auth-session hash during upstream
upgrades. Current-session logout deletes the authoritative DB session; user-wide
revocation advances the counter without scanning sessions. Account disablement
must advance the counter transactionally even for QuerySet/bulk writes, and a
stale User save must never reduce it. Missing security state must fail closed.
Full profile saves also preserve the authoritative disabled flag, including stale
administrator forms. Keep reactivation as an explicit administrator operation with
fresh server-side authority checks; it must preserve the existing revocation
boundary.
Ordinary explicit active-field saves, QuerySet updates and bulk updates must reject
inactive-to-active transitions outside that service, even with an administrator
actor. Preserve direct bypass, mixed-batch rollback and fresh-login denial tests.
Backup recovery must preserve/reconcile current account active flags, both security
tables and the complete audit ledger before traffic resumes.
Retain regressions for stale authorized profile saves, unauthorized reactivation,
pre-disable cookie replay after reactivation, and fresh-login denial after restoring
a backup that predates account disablement. Version verification and cookie clearing
alone do not establish account-state recovery.
Provision both security records atomically with new users. Bounded backfill copies
existing primary versions into new boundaries without resetting them; drain old
writers and verify zero missing/inconsistent pairs before enforcement. Preserve
negative provisioning-failure, nonzero-version migration, missing-boundary and
mismatched-pair regressions. Authentication never repairs either record, and
online recovery requires a surviving authoritative recovery boundary.

Account mutation batches must preserve Django write routing and backend parameter
budgets, including target discovery and later ID predicates. Keep global PK lock
order and capture the entire target scope before any profile/state write. All
chunks share one transaction and fresh authority checked after locking; retain
budget, captured-scope, whole-batch rollback, self-disable and overlapping-batch
concurrency regressions across upstream QuerySet changes.
When the original scope exhausts its parameter budget, do not force a one-ID
predicate: retain the executable scope, lock in global order and intersect the
supplied object set before writes. Document the broader lock scope of this fallback.

Disable entry points must receive the trusted authenticated `session_actor`, reload
its active staff/`users.change_user` authority, lock authoritative targets and
apply side effects only to real active-to-inactive transitions. Preserve the
independent monotonic recovery boundary and require matching state on authentication.
Online recovery is administrator-only; loss of that boundary requires the documented
maintenance reauthentication barrier, never a guessed default version.

Revoke-all and real account-disable operations commit a durable
`SessionRevocationEvent` in the same transaction. The local DB audit ledger is the
supported receiver, with stable UUID and unique target/version identity, no TTL and
read-only administrative access. Log callbacks are optional projections. External
SIEM delivery is a separately configured topology and must prove receipt/dedup,
retry/redrive and restore semantics before use. Rollback/restore must preserve the
ledger and reject restored browser credentials before traffic resumes.

Account mutation batches must preserve Django write routing and backend parameter
budgets, including target discovery and later ID predicates. Keep global PK lock
order and capture the entire target scope before any profile/state write. All
chunks share one transaction and fresh authority checked after locking; retain
budget, captured-scope, whole-batch rollback, self-disable and overlapping-batch
concurrency regressions across upstream QuerySet changes.
When the original scope exhausts its parameter budget, do not force a one-ID
predicate: retain the executable scope, lock in global order and intersect the
supplied object set before writes. Document the broader lock scope of this fallback.

Retain Django password-change and secret-key fallback behavior, existing session
expiry policy, and project/member/assignment authorization as distinct controls.
The user admin revocation action derives its actor from the authenticated server
request. API/JWT tokens are unaffected. Require negative copied-cookie and
cross-process regression tests, and keep expired DB-session cleanup scheduled.
Keep authorization rejection tests for account disablement at every supported
write entry point: model `save()`, `QuerySet.update()`, and `bulk_update()`.
Missing-state recovery requires a freshly authorized active human staff
administrator with `users.change_user`, derived from trusted server authentication;
ordinary self-service and system/background identity provide no recovery grant.
Preserve direct-service rejection, stale-privilege/disable, and API/admin
actor-substitution regressions for recovery across upstream upgrades.
The normative security state machine, monotonicity, transition, atomicity,
migration/recovery, authorization, and audit-durability guarantees are defined in
[session security invariants](session-security-invariants.md). Preserve that file
as the source of truth across upstream rebases.

#47 owns security-state and authorization acceptance; #48 owns audit acceptance.
The selected local database topology deploys both as one shared foundation, with
all security/audit tables and integrated writers present before revocation or
disable paths are enabled. Durable audit intent is required in the same
transaction as the transition; audit insertion failure rolls back the security
state and enclosing account/batch writes. The selected local database ledger accepts
events atomically, retains stable event/target-version identity without an audit
TTL, and preserves or reconciles the complete accepted ledger across rollback/
restore. Optional logs are projections. If external delivery is configured, it
additionally requires at-least-once dispatch with crash-durable receiver dedup,
retention for the full replay horizon, retry/dead-letter redrive, and preservation
or reconciliation of pending intents and receiver accepted-ID state across restore.
Do not regress either topology to best-effort post-commit-only logging or volatile/
short-lived acceptance state.

See [session revocation](session-revocation.md) for configuration, cutover,
rollback boundaries, and deployment acceptance.

The [#48 local candidate handoff](issue48-release-candidate.md) records the fixed
source/image identities and synthetic operations evidence. Preserve native Linux
entrypoint symlinks and Git object bytes when building on Windows. A restore must
reconcile independently retained, authoritative latest account flags, paired
versions and the complete accepted ledger before clearing restored sessions and
reopening traffic; pair consistency or a checkpoint checksum alone cannot prove
freshness. The handoff does not establish production or trusted-browser acceptance.

## Display locale remains outside authorization

The main app's language controls wait for AuthProvider to confirm the actor;
an SSR avatar is not authenticated authority. Background refresh of an already
confirmed actor does not disable the language controls. Pending initial Whoami cannot trigger a
locale write or a misleading save failure. Imperative collaboration modals
retain their existing provider boundary and open only after actor readiness.

The request-locale middleware runs after Django session authentication and
wraps JWT middleware and DRF views. A successful JWT or legacy API-token
authentication may update only the active display locale for that request.
It must not establish, replace, or revive an authenticated actor. Keep the
preference in its separate `UserLocalePreference` table, never in session
security state. Preserve the existing token enablement and revocation checks
before applying a token user's locale, and re-run the negative authorization,
CSRF, and copied-cookie tests after upstream middleware/authenticator upgrades.

## Credentials in diagnostic logs

During upstream upgrades, preserve the organization invite-reset diagnostic as
an organization ID only; logging the newly issued invite token exposes a usable
credential at DEBUG level. Keep the API's authorized response intact. The
organization logging regression exercises the real reset endpoint under DEBUG
capture and checks that the returned token is absent from diagnostics.

Preserve the shared Cypress failure hook's message/stack redaction for credential
headers, session/CSRF cookie fragments, Bearer/Token values, and keyed or URL
`token` values and the existing JWT `access`/`refresh` credential fields.
It rethrows the original error, retaining failing request status
and route. Re-run the actual HTTP, Token and invite negative probes, inspect
their logs/screenshots/videos, and retain a passing known-credential detector
control before publishing evidence. See the
[failure artifact rehearsal](../i18n/failure-artifact-rehearsal.md) for guarded
synthetic-data commands, receipts and inspection limits.

Retain complete opaque-token matching, including RFC 6750 `+`, `/` and `~`
characters. The artifact auditor must reject missing/empty directories,
media-only text inputs and empty logs; an empty scan is not accepted evidence.

## Candidate UI lifecycle checks

On upstream upgrades, preserve the optional login-tips timeout's bound
`AbortController.abort` receiver. A slow advisory request must not raise an
exception or interrupt authentication. The bilingual timeout regression checks
the actual aborted signal, retained input and successful password login.

Data Manager initialization must ignore dependency/backend completions after
its React page effect is disposed. Check cancellation after asynchronous work
and reject an absent/already-initialized DOM root before creating the workspace.
The bilingual delayed-backend navigation regression exercises this on the real
page. These guards control UI lifetime only; they do not authorize access or
replace server-side membership, assignment and revocation checks.

The data-list debounce must resolve without calling a destroyed model. Guard
collection fetches before accessing their workspace SDK and after asynchronous
responses, using the existing MST `isAlive` checks. Re-run the five collection
fetch lifecycle cases and real import/navigation journeys on upgrades; these
checks retain normal live loading and do not relax server authorization.

Tab selection, reload, creation and save continuations must also stop after
workspace disposal. A cancelled data-list promise resolving is not permission
to send a tab-change event or update an old view. Keep the tab lifecycle
regressions alongside the collection cases and run the whole Data Manager
unit suite before carrying these patches through an upstream upgrade.
The enclosing `fetchTabs` initializer must recheck workspace lifetime after
selection and save as well: a cancelled child flow may resolve normally while
its parent still owns a disposed model. Do not yield actions from that model
or start labeling after disposal.

The E2E submission fixture must lock its assignment without joining the nullable
annotation relation. Lazy annotation access inside the transaction keeps the
fixture compatible with PostgreSQL; the actual submission service retains its
own authoritative locks and checks.

### Issue #76 annotation usability, 2026-10-14

Interactive task responses expose `can_label` from the existing server-side
assignment policy. An unassigned Manager retains task/annotation read access;
Data Manager makes this editor read-only and explains how to request assignment.
This projection does not replace the annotation/draft endpoint guards or grant
Managers authority to edit another actor's assigned annotations. Revocation
still denies API writes and stale task access on the server.

Export-format discovery now applies the same immutable-release guard as raw
exports after checking Manager scope. `IMMUTABLE_RELEASE_REQUIRED` distinguishes
this expected rejection from format-load failure. Both display locales explain
the existing Release route; pending formal submissions remain unreleasable.
No Core review authority, approval rule or export-storage boundary is changed.

The Release workspace downloads the original response blob from that same
authenticated release endpoint after verifying the selected submission ID,
revision, hash and snapshot. Do not re-serialize its parsed JSON: snapshot
integers can exceed JavaScript's exact Number range. Failed or stale responses
(including a changed selection or closed workspace) must not download a file
or display success. Preserve the bilingual retry feedback and actual file
checks when rebasing; mutable project export remains closed.

The optimized finished-task aggregate counts distinct task IDs so project
membership joins cannot inflate completion. Draft navigation feedback uses the
stable locale runtime; changing locale does not re-register or release a pending
navigation guard. Selected-region action labels retain the original behavior.

The shared ToastProvider maps its documented `duration=-1` contract to Radix's
supported `Infinity` duration. Passing -1 through previously closed permanent
guidance immediately. Mounted-provider tests retain the normal timed-close
control and verify that dismissing another toast ID cannot remove this guidance.

DraftGuard also depends on ToastProvider's stable `show` callback rather than
the context value object, which changes when feedback is rendered. Controlled
context-replacement regressions keep failed and pending saves blocked, including
a locale change during the request; successful saves release navigation once.

Readonly annotation results also disable submit/update/skip/unskip controls,
including projects that permit empty annotations. AppStore rejects these editing
actions before dispatch when called through keyboard/action paths. Review
accept/reject keeps its separate existing controls and authorization boundary;
readonly results do not themselves remove a Reviewer's decision authority.

Data Manager refreshes persistent task-access guidance when its existing locale
runtime changes. This refresh only replaces or dismisses its own toast ID; it
does not reapply annotation access, recreate stores, save drafts or submit work.
Destroyed editors cannot show stale guidance. Browser regressions switch both
languages in the same readonly task while retaining disabled editing controls.

Both existing next-task entry points now prioritize the authenticated actor's
active TaskAssignments within the prepared filter/selection. The existing
upstream assigned-mode sampler handles those tasks, so another assignee's
completion or task lock cannot exhaust the actor's queue. Unassigned Managers
retain the original automatic read-only exploration fallback; no client field
selects assigned mode or expands the permitted task scope.

Assigned completion, skipped annotations and postponed drafts are tied to the
current active assignment. Historical cancelled assignments do not complete or
postpone a new assignment to the same actor. Assigned skipped/postponed queues
remain available after another actor completes the task. The existing unassigned
completion/overlap filters and server-side write/revocation guards remain
authoritative. Preserve these two-entry-point regressions on upstream upgrades.

Interactive task and queue payloads restore only the current actor's active
assignment drafts. Cancelled/unbound historical drafts remain stored but cannot
be carried into a new assignment's save or submission. A requeued skipped task
returns only its current assignment's owned skipped annotation and selects it
with the editor's existing `default_selected_annotation`/Unskip mechanism. Keep
the resume, draft-submit and historical-record retention controls as regressions.

### Issue #76 persisted draft confirmation, 2026-10-15

Refresh/close protection and Data Manager navigation use the annotation's
server-confirmed snapshot rather than Undo history or timestamps. Initial server
results, restored drafts, lazy server hydration and successful saves explicitly
confirm that snapshot. An older draft response confirms only its sent snapshot;
later edits, undo/redo and failures remain protected until their own save succeeds.
Image/layout history resets must neither confirm user edits nor cancel a pending
dirty autosave. Keep the delayed-image and completed-queue browser regressions in
both display languages when upgrading upstream.

After submitting the last assigned task, the selected annotation may remain alive
on the queue-completion screen. Confirming that successful submission prevents
Projects/navigation from sending a redundant draft with an obsolete assignment
version. The server must still reject stale assignment tokens; no authorization
policy, Core review authority or IAM operations are changed by this UI fix.

### Issue #76 in-page task navigation, 2026-10-16

Data Manager task-row selection previously fired a draft save without awaiting
its result. Replacing the outgoing editor while that request failed discarded
the only copy of its draft. Task selection, closing the task panel and entering
the labeling queue now share an awaited draft gate in AppStore. Keep the editor,
task selection and URL intact while saving, preserve failed edits for retry,
and ignore competing task choices until the first transition finishes. Browser
history failures restore the last accepted editor URL. The annotation flush must
still confirm edits made during an older request before releasing navigation.

Retain both-language browser regressions for failed saves, delayed responses,
newer edits, repeated task choices, separate A/B draft ownership, reload recovery
and rejected browser Back navigation. This changes client navigation only;
assignment IDs/versions and server-side draft authorization remain authoritative.

### Issue #76 keyboard focus and IME input, 2026-10-16

Choice controls must keep a stable native input across observable updates and
retain keyboard focus after Space. Pointer selection still releases focus for
editor shortcuts. The keymaster filter establishes the originating input scope
before forwarding numpad digits; text inputs and contenteditable elements must
not forward typing as label shortcuts. Checkbox/radio focus remains in the
editor shortcut scope.

Composition keys (`isComposing`, React's native event, or legacy process key 229)
must not submit, cancel, blur or run editor shortcuts. Single-line annotation
forms also suppress implicit Enter submission during composition. Preserve the
normal Enter/Shift+Enter/Ctrl+Enter behavior after composition finishes, including
existing text result edits and per-region inputs. Keep bilingual browser and
negative-path component regressions on upstream upgrades. Chromium composition
events do not establish acceptance of a physical Windows IME candidate window.

### Issue #76 restored entity draft state, 2026-10-16

Plain-text entity global offsets are derived solely from the persisted numeric
start/end offsets. Initialize this cache with the MST region snapshot, before
saved-state confirmation and history capture. Lazy first rendering must neither
report a saved draft as dirty nor enqueue an unnecessary autosave. Preserve
already-calculated Undo/Redo snapshots and the existing DOM-dependent HTML range
calculation. Do not relax the generic snapshot comparison or clear dirty state
after rendering: actual text/boundary edits, failed writes, and edits made during
an older request must remain protected until their own save succeeds. Keep the
English/Chinese entity draft restore, failed-save and retry browser regressions
when upgrading upstream. Server ownership and assignment checks remain unchanged.

### Issue #76 concurrent draft pages, 2026-10-16

Assignment versions protect authorization lifecycles, not draft contents. Each
draft now exposes a monotonically increasing content `revision`. Updates,
deletes and annotation writes consuming a draft require `draft_revision` from
the page's last acknowledged snapshot. Missing or stale tokens return 409 with
`display_context.reason=DRAFT_CONFLICT`; no remote revision is adopted on error.
The transaction locks the active assignment, project summary and draft in that
order, preserving upstream summary-before-draft ordering. The model increments
the revision for all instance saves. First creation is serialized by assignment
and rejects an existing draft in the same actor/assignment/annotation scope.
Draft owner, task and assignment references are server controlled; the annotation
reference permits only the existing, revision-checked unskip detach operation.
Unversioned old clients must reload the new editor before writing existing drafts.

On conflict, preserve local results and unsaved navigation protection, cancel
pending automatic saves and refuse blind retries. The bilingual conflict panel
offers a local JSON backup, then an explicit reload of the server's latest task.
Edits made after downloading invalidate that backup acknowledgement. Failed
reload retains the local editor. Recovery must not merge obsolete annotations
into the fresh server snapshot. Submission/deletion must not bypass this guard.
Submit/update must first await this page's own in-flight draft acknowledgement
before capturing results and its content token, so an ordinary continuous edit
does not produce a spurious conflict with its own background save.
The recovery copy must observe the real MST content snapshot: serialization is
an action and does not by itself subscribe a React observer to later edits. Read
the current copy again at download/reload action time. Render the recovery panel
through a body portal so the editor's clipped panes cannot hide its instructions.
Keep PostgreSQL simultaneous-writer tests and bilingual two-page offline/recovery
acceptance on upgrades. This is draft concurrency control; immutable review
decisions, IAM operations and Core business integration remain outside this work.

An existing draft can also disappear when another page submits or deletes it.
Its update/delete endpoint retains the ordinary 404 response and authorization
checks. Only a 404 from an existing-draft operation enters the editor's missing
draft recovery state; a create endpoint's 404, permission denial or transient
network error must not be misclassified. Keep the stale ID, pause autosave and
block submission/retries while retaining local edits and navigation protection.
Do not fall back from PATCH to POST or automatically recreate a removed draft.
After a current local backup, explicitly load the latest task (which may contain
a final annotation or no draft). A failed load preserves the local editor.
When no draft remains, explicitly select the latest returned annotation owned
by the authenticated editor user, rather than upstream's default blank new
annotation. Existing drafts retain their normal hydration selection. This is
display selection only; all read/write authorization stays server controlled.
Keep bilingual submit/delete recovery tests, including server readback proving
no draft recreation and no changes to submitted annotation/submission records.

### Unconfirmed text before navigation (#76)

TextArea input buffers and local edits to an existing text result are not yet
confirmed annotation results. Track them separately from the server-saved draft
baseline. They must trigger the browser's native unload warning, and prevent
in-page navigation or recovery reload until the annotator confirms or clears
the input. Explain the action in both display languages. Unchanged prefilled
text and read-only views must not be blocked. Retain upstream's explicit text
confirmation/submission behavior; no buffered text is silently added to draft
serialization, immutable submissions, or recovery-copy downloads.
Physical Windows IME and native Cancel acceptance remain separate from synthetic
composition events and beforeunload-hook regression assertions.

TextArea action copy uses the existing per-editor locale runtime. Adding a text
entry or confirming an existing entry is distinct from saving a draft or
submitting an annotation; do not label text-entry counts as annotation submissions.
Shortcut hints must follow the control's actual single-line Enter or multiline
Shift+Enter behavior. Changing display language must preserve the mounted text
editor, active input buffer, focus and pending-input registration. Keep custom
project text/labels unchanged and retain upstream confirmation, blur and save
semantics; these copy changes do not alter server authorization or submission.

Recheck pending text after an awaited outgoing draft save, immediately before
releasing either task selection or router navigation. The saved result snapshot
does not include raw input entered while the request was pending.
Conflict recovery must also retain the backed-up editor identity and content
through the HTTP response and the subsequent paint yield. Check again immediately
before resetting the annotation store; reject an obsolete recovery if confirmed
results, pending input, annotation selection or editor identity changed. Fetch
without changing task selection, keep all local content, and require a current
copy before retrying. Explain this local-edit outcome separately from a failed
network load in English and Chinese. No authorization or server revision rules
change; retain delayed-response text and rectangle browser regressions.

Retain upstream's text span resize handles and symbol granularity. Chromium hides
the `<area>` elements used for these handles by default; explicitly display them
as inline within rich-text highlights so the existing native drag selection can
run with each handle anchored to its text boundary (block breaks static positioning).
Preserve the same region ID, label and per-region results while adjusting offsets.
Cover both handles with native browser mouse input, including Chinese characters
after a supplementary Unicode character, outer and internal English spaces, and
intentional Chinese/English punctuation. Draft save, reload and submission must
retain the selected code-point range and its exact text; never silently trim data.
Exact-space templates must opt into the existing Text whitespace contract with
`<Style>.lsf-richtext__container { white-space: pre-wrap; }</Style>`. The default collapsed
display remains unchanged. Keep consecutive internal spaces and source offsets
in the regression fixture; use cypress-real-events for AUT iframe coordinates
when dispatching native mouse input rather than assuming frameElement is present.

Submission history is also a read-only feedback entry for annotators. Reuse the
existing submission list/object scope and immutable review metadata: an annotator
can read their own submissions and rejection reasons without gaining reviewer or
release authority. The shared history component defaults to read-only; only the
manager entry opts into release actions. Server-side list, object, review and
release checks remain authoritative, including after role revocation. Keep the
English/Chinese rejection-to-revision browser flow covered without granting extra
roles merely to display feedback.

When restoring a draft attached to a submitted annotation, select it with an
explicit hydration marker. Object/control context must be selected before
deserialization, but that intermediate selection must not flush the temporarily
cleared result with a pre-hydration draft token. Normal user selection still
awaits outgoing edits; draft revision checks and real conflict recovery remain
unchanged. Cover reload of a saved correction before submitting revision 2.
Without an explicit annotation selection, keep the restored correction's existing
annotation selected rather than opening a new blank annotation; explicit user
selection continues to take precedence.

### Project write fencing and personal work (#83, first slice)

The second #83 slice is documented in [Fixed task packages](fixed-task-packages.md).
It adds an opt-in fixed membership list and read-only progress, declares the
ORGANIZE_WORK operation for the common #86 policy boundary, and reuses the same
assignment state expression. It does not enable dispatch or a lifecycle engine.

The package navigation slice is documented in [Package-scoped continuous annotation and review](package-scoped-work.md).
Keep live visibility in every aggregate and recheck the original role and page
package scope before serializing a read response; use a fresh Project for the
final READ_WORK policy without adding a project write lock to progress GETs.
After materializing a scoped Editor/next response, compare its assignment ID
and version with the current actor's active assignment, including replacement
assignments to the same actor. Revalidate annotation/draft ownership and narrow
can_label using the fresh EDIT policy. Scoped history may clear an inaccessible
annotation reference in its response but must preserve the global history.
Review pagination recovery uses SUBMISSION_PAGE_OUT_OF_RANGE rather than
translated error text; revoked or unavailable scope never triggers fallback.

`access_control.project_operations` is the common operation-policy boundary for
single-assignment creation/cancellation, interactive Annotation/Draft writes,
immutable submission and review decisions. Write transactions acquire Project
first, then OrganizationMember rows, User rows and ProjectMember rows (each set
in primary-key order), then Task, Assignment and the existing summary/draft or
submission locks. Use single-table row-lock queries and reuse the locked
objects, avoiding joined users/tasks, nullable joins and backend-specific OF
support.
Never enter this fence after acquiring an assignment lock unless this same
transaction already owns the project/authority fence. PostgreSQL tests exercise
actual Editor and member APIs; a submission-helper-only test cannot detect the
former Assignment -> Project / Project -> Assignment deadlock.

Re-read account activity, current organization membership, project role and
target eligibility under the fence. Single-task assignment POST reuses the
same target validation as its preflight serializer and records the trusted
`resolve_actor` mapping as `assigned_by`. Cancellation increases the existing
assignment version; a later assignment is a new row without copied answers.
Security cancellation/member revocation remains available while ordinary work
is blocked. Revocation wins -> stale Editor write is denied; Editor wins -> its
completed immutable submission remains historical evidence and revocation
invalidates subsequent writes. The pre-write task token and draft CAS checks
remain required. Project-first fencing also serializes concurrent review with
submission supersession and role removal; manager does not become reviewer.

`ANNOTATION_PROJECT_OPERATION_PROVIDER` explicitly defaults to the versioned
`LegacyProjectOperationProvider` for compatibility with existing fork-local
single-task workflows. Its state is **legacy**, never inferred active from
`Project.is_draft`/`is_published`. It grants no batch-dispatch operation. A missing
provider or unknown policy state denies work. #86 owns an authoritative project
lifecycle/provider; a test paused/draft policy proves these covered write paths
obey its denied operations. This patch does not implement #86 transitions or
claim coverage of import, storage, project configuration, deletion or release
policy. Host-controlled/batch writes remain unavailable pending that contract.

`GET /api/projects/{project}/my-work/` projects current personal facts without
mutations, new task states or migrations. Supported filters are `kind`
(annotation/review), `state` (annotation only), `page`, and `page_size` (50 default,
100 maximum). Counts cover the same authorized base queue, before the optional
state filter; pagination count covers the filtered queue. Unknown identity/user
filters are rejected. Scope and audit identity come from `resolve_actor`, with
an explicit current organization membership check. Replies, including failures,
are private/no-store. No Task data, annotation result or snapshot is returned.

Annotation rows start from the actor's active assignments. Latest means highest
revision per assignment. Rejected -> rework with its reason; different valid
saved draft -> in progress while retaining the previous submission badge;
unchanged draft -> preserve pending/approved status. Empty first draft is work,
not completion. Result comparison ignores JSON object insertion order and
lead-time but preserves array order and region IDs, using native PostgreSQL
jsonb or SQLite json_tree facilities. Duplicate/mismatched draft sources or a
latest superseded-only revision yield blocked conflict rows without repairs.
Cancelled assignments disappear from annotation work, without deleting history.
Queries compute states/counts in the database and fetch metadata for one page;
query count must not grow with page length.

Review rows mirror the existing pending, non-self review queue and retain
snapshots whose source assignment was cancelled. They never grant Task/Editor
access. My work links reuse the existing Editor and review workspace; these
recheck permissions when opened or written. The bilingual read-only list clears
old rows on refresh failure and ignores disposed requests. It does not claim to
display unconfirmed browser input or grant authority from cached action flags.

An out-of-range work page returns `display_context.reason=WORK_PAGE_OUT_OF_RANGE`
from the paginator only. The client retries page one once using that code,
independent of DRF's translated detail. Other 403/404 responses clear the list
without retrying, so revoked project access cannot masquerade as pagination.
Editor anchors use AsyncPage's existing `data-external` native-navigation opt-out:
its query-only partial navigation otherwise retains the old DataManager/modal
instead of initializing the requested task. Native navigation retains the
existing browser unsaved-work guard. Work-list classes use the common `cn`
helper to match the build's CSS prefix.

This slice does not enable TaskBatch, bulk assignment, self-claim, deadlines,
load balancing, host/Core integration or #79 delivery. Keep PostgreSQL real
transaction interleavings, SQLite projection tests, provider-mapping/negative
authorization cases, and bilingual component regressions in the fork gates.

Concurrent API tests must initialize each independent client middleware stack
before starting request threads, as a production worker does. The existing
`CommonMiddlewareAppendSlashWithoutRedirect.__init__` temporarily mutates global
MIDDLEWARE while constructing its replay handler. Cold concurrent test-client
construction can therefore omit slash handling and return 404 before a write
ever reaches the service. Warming the stacks preserves the real simultaneous
write/CAS assertions; it does not resolve that separate baseline initialization
architecture risk. This slice leaves middleware behavior unchanged and makes no
claim about a cold concurrent handler-startup acceptance.

The My Work entry and its Editor/review destinations resolve the same trusted
local actor. Task detail, project bootstrap, DataManager views/columns/task
collections, ML bootstrap metadata, submission lists/details and uploaded-image reads must not borrow
the session user's project/manager role. The shared visible-project query repeats
live organization membership in SQL; cancellation still preserves authorized
submission history while removing Editor access. View creation records the
mapped actor, updates retain the creator, and source/target projects are checked.

Object permissions use DRF's existing `HasObjectPermission` with an explicit
`HasActorObjectPermission` opt-in only on the migrated entry APIs. Keep the
global default unchanged: legacy APIs may still scope collections by session
identity, so globally replacing their object-check actor can grant unrelated
access (including orphan user profiles). Regressions exercise the real provider
seam across work-to-task, saved drafts/annotations, review/history, view startup,
uploaded PNG bytes, project/organization/account revocation and reassignment,
plus refusal to borrow a session manager's role or expand legacy profile access.
Annotation/draft object reads recheck current project/organization authority;
first-annotation creation and collection reads use the same mapped actor.
ML metadata GET does not grant backend configuration: its POST requires the
existing project-manager policy before serializer health/setup can contact a URL.
Uploaded media retains its existing project-member policy, not assignment-only
visibility. External-storage proxy/presign, local-storage roots and avatar
identity migration are outside this slice and have no new runtime acceptance.

Callers that wrap `create_submission` in an outer transaction must acquire the
same Project/eligibility -> Task -> Assignment fence before any lower-level
lock or Annotation save; model signals can write Task rows too. The existing
`create_enterprise_e2e_submission` command now follows that order for existing
shared tasks. Real PostgreSQL command/API interleavings cover command-first
submission followed by revocation and revocation/cancellation/reassignment-first
refusal without modifying the old Annotation. Seed commands only submit resources
created in their own uncommitted transaction; API callers already use the fence.

Fixed task-package detail reads revalidate each returned assignment ID/version
or pending submission ID/revision through the existing actor/project/live-task
querysets after the final project/package policy check. Partial cancellation,
reassignment or review departure rejects the materialized page with
PACKAGE_ACCESS_CHANGED even if another row keeps the package visible. This is
a bounded scope check without Project write locks; ordinary progress values
remain a live projection. PostgreSQL regressions interleave real cancellation,
reassignment and review APIs with a paused detail read. Existing Editor/submit
and review write fences remain authoritative independently of these links.

Scoped package navigation resolves its initial Project through the trusted
actor before legacy transport-user object checks. The toolbar next-task,
legacy project next-task and label-stream history entrances use the same
PackageScope authorization as the later task query and response checks.
Transport membership is neither required for an authorized mapped actor nor
sufficient for a revoked actor. This migration is limited to explicit package
context; shared legacy project helpers, global permissions and mutation actions
retain their existing behavior. API regressions cover both directions and the
unchanged unscoped denials.

Review sampling (#84) scopes the existing review command with READ_WORK plus
REVIEW and retains its Project-first write transaction through serialization.
Before returning a batch-context decision, recheck current actor/mode/operation
authority and the emitted frozen source/decision facts. Invalid evidence or
lost authority rolls the decision back. Queue responses reuse this representation
check; previews recheck ORGANIZE_REVIEW after planning. Keep this boundary when
upgrading the shared review API; ordinary unscoped review/source-read behavior
and Core-only-review internal acquisition are unchanged. PostgreSQL NOWAIT,
revocation, serialization corruption, original-key recovery and conflicting-key
regressions protect the boundary.

Rework comparison (#84) is a read-only batch-context projection over the frozen
rejected source and paginated later revisions of the same assignment/task/actor.
Reuse current READ_WORK, role, mode and source/representation validation; do not
replace it with unscoped Submission reads or infer REVIEW/release authority.
Revalidate after serialization and clear stale UI evidence on failed reads.
Reload the batch too: a deleted batch or changed frozen plan must not authorize
an already materialized comparison, even if a replacement plan hash is valid.
Core-only-review denies this new route. See review-sampling-v1.md for the bounded
JSON comparison and unchanged #81 conflict/arbitration ownership.

Classification disagreement (#81) is a separately gated read-only projection.
Reuse live READ_WORK and trusted actor/project scoping; exclude Reviewer self
sources even for signed receipts. Revalidate exact pinned source/hash/configuration
tuples and current authority after materialization. Signed receipts never grant
access or replace unavailable inputs with latest revisions. Preserve independent
feature/mode gates and Core-only denial on upgrades; no review, assignment,
arbitration or release authority is introduced. See classification-disagreement-v1.md.
Repeated checks must bind the same trusted principal, local actor and role, not
only a same-name role. The metric supports inline Text without resolvers and
static Choices only; editor remote resolvers and dynamic label sources cannot
stand in for immutable displayed-content evidence.

Durable classification batches (#81) add a separately gated immutable analysis
record and ORGANIZE_CLASSIFICATION; do not substitute REVIEW/ORGANIZE_REVIEW.
Creation retains the project-first transaction through response rendering and
rechecks the signed preview plan, identity and operation. Original-key recovery
is principal/actor-bound and never selects new sources. Reviewer filtering omits
whole self-participant tasks before SQL count/pagination; no complete manifest
hash or hidden counts may escape through a subset view. Live source loss masks
answers/scores without changing the frozen batch. PostgreSQL/SQLite triggers
protect the manifest; retain the projects.0028 → core.0001 schema dependency on
upgrades. See classification-batches-v1.md. Core-only-review denies all new batch
routes; no arbitration, sampling, review, export or Host authority is introduced.
Migration 0003 also freezes the batch primary key, preserving object identity
and original-key recovery for databases that already applied 0002. Bound source
loading before retaining each decoded snapshot; never load unfrozen Task.data,
Task.meta or review comments for this calculation. An over-budget changed source
masks answers rather than returning partial comparisons.
The UI may invalidate a first attempt only for recognized preview rejection
codes with matching HTTP statuses. Once confirmation is unknown, subsequent
errors and late responses must preserve the original request/key; reads never
resubmit it or silently create a replacement.

### Frozen text classification arbitration (#81)

Arbitration adds a separate immutable local ledger and independent disabled-by-default
flag/mode map. Only current Managers dispatch/cancel; only the assigned independent
Reviewer can adopt an existing frozen revision. All sources remain pinned and verified;
source loss masks answers, while authorized original-key recovery retains receipts.
Project-first fencing, CAS and PostgreSQL/SQLite constraints protect concurrent terminal
transitions. Cancellation remains a SECURITY_CANCEL operation without READ_WORK.
Recheck source graph projection, mapped principals, frozen triggers and response-time
authority on upgrades. See [classification-arbitration-v1.md](classification-arbitration-v1.md).
Submission, review state, sampling, Host and Core authority remain unchanged.

Concurrent arbitration capability/data reads also bind an ephemeral viewer HMAC
to the current principal, actor, project/organization and role. Denied reads or
mixed contexts clear the UI; current capability and object permission must both
permit an action. This pin grants no authority and does not alter persisted
receipts, including recovery across secret rotation. Standard DRF validators reject
invalid Unicode/NUL reasons with 400 before request hashing.

Package discovery (#83) reuses existing package membership and assignment/review
facts for batched role-scoped list summaries, pre-pagination state filters and
explicitly selected-package My Work. Preserve sample versus assignment units and
independent counts for overlapping packages. Package context only narrows existing
READ_WORK/EDIT/REVIEW authority; scoped My Work rechecks materialized work identity
and current policy before emitting actions. Failed reads clear old rows and never
fall back to project work. Keep unscoped behavior and existing Editor/review
navigation guards; do not turn this projection into dispatch, #86 lifecycle or
#80 content/release identity. See package-discovery.md for scope and regressions.
Keep live project-role predicates in the membership SQL and personal/reviewer
predicates on the counted facts. Intersect that scope once per aggregate;
re-nesting the full visibility query can stall thousand-item progress reads.
Package visibility still guards metadata before and after materialization.
