# Project lifecycle: #86 implementation contract

Status: design and acceptance baseline for the default-off implementation, not
evidence that all surfaces have passed review or that deployment is approved.
Baseline: `main` at `c341c4788`. The lifecycle must remain disabled until the
write-surface inventory, concurrency tests and deployment prerequisites below
are complete. The host owns product policy; this fork must enforce any lifecycle
it exposes server-side. Real-cloud isolation and request drain are tracked in
#144; automatic version-safe deletion is tracked in #145.

## Facts and boundary

`access_control.project_operations` provides a project-first transaction fence
for several existing collaboration operations. Its default provider reports
`legacy`, not an authoritative lifecycle. The authorization plan explicitly
does not claim coverage of import, storage, configuration, deletion or release.
`Project.is_draft` means creation in progress, `is_published` concerns publication
to annotators, and `deleted_at` hides soft-deleted projects. None is a reliable
proxy for completed or archived. Task FSM, assignment and immutable Submission
state remain independent.

## Proposed lifecycle and transition protocol

Persist a separate constrained state and monotonically increasing revision on
Project, plus a transition event with old/new state, revision, timestamp,
authenticated actor and non-empty reason. Preserve actor identity if the user
is later removed. Event and state change commit together. Event access requires
current project authorization; do not put sensitive free text into general logs.

Proposed transitions: draft → active; active → paused or completed; paused →
active or completed; completed → active or archived; archived → active. An
explicit Manager command and reason are required for each, including recovery;
an archive is never implicitly undone by editing another field. Treat this graph
as a policy proposal pending product review, not an existing guarantee. Reject
unknown states, transitions and stale expected revisions. A request must resolve
the trusted principal and project independently of submitted actor/project IDs,
lock Project first, revalidate current membership and Manager role under the
existing authority fence, then validate revision and transition. Duplicate retries
need an idempotency contract or a clear conflict response; never silently create
two events. A read-only state/capability response must not confer authority.

## Proposed operation matrix

All entries remain subject to existing RBAC, assignment, immutable release and
feature-flag checks. “Allow” means eligible for those checks, not a new grant.
Unknown operations or states deny. Security revocation/cancellation remains
available in every state and must not grant or alter annotation/review evidence.

| Operation | draft | active | paused | completed | archived |
| --- | --- | --- | --- | --- | --- |
| Authorized project, work and immutable history reads | allow | allow | allow | allow | allow |
| Task import/create/delete, reimport, predictions and source sync | allow for setup | allow | deny | deny | deny |
| Assignment, claim, annotation/draft/submission writes | deny | allow | deny | deny | deny |
| Review, arbitration and quality mutations | deny | allow | deny | deny | deny |
| Project configuration and storage configuration mutations | allow | allow | deny | deny | deny |
| Approved immutable release/download and audit reads | existing policy | existing policy | existing policy | existing policy | existing policy |
| Generate exports or initiate external export sync | deny pending separate contract | existing policy | deny | deny pending separate contract | deny |
| Explicit lifecycle transition; security revocation | authorized command | authorized command | authorized command | authorized command | authorized command |

Whether completing a project may initiate a new immutable delivery or audit
capture is unresolved; default deny mutations until the delivery owner and
authorization are specified. Existing authorized immutable downloads are reads,
not permission to regenerate or mutate the source. This matrix does not change
the existing prohibition on delivering mutable annotations after a Submission.

## Required coverage before enabling transitions

The following are starting points, not an exhaustive route list. Inventory every
URL, bulk action, management command, scheduled job, signal and provider-specific
worker that can affect project data; map each to an operation or explicitly
disable it. A UI-only check or a check before an unlocked transaction is not a
write fence.

| Surface | Known starting points | Required work |
| --- | --- | --- |
| Editor, assignments, review | `tasks/api.py`, `tasks/assignments.py`, `tasks/submissions.py`, review and quality APIs | Preserve Project-first locking and check each mutation, including bulk paths, against fresh state. Verify all feature-flagged operations and security cancellation. |
| Import and data | `data_import/api.py` ImportAPI, ReImportAPI, ImportPredictionsAPI, TasksBulkCreateAPI; `projects/api.py` ProjectTaskListAPI; `tasks/api.py` TaskAPI and PredictionAPI; data-manager actions | Cover direct, bulk and reimport writes, uploaded-file deletion and asynchronous entry points. Validate scope from the locked project, including ID substitution. |
| Storage | `io_storages/api.py` configuration, sync APIs and `_run_export_storage_auto_sync`; provider sync workers | Cover auto-sync as well as explicit sync, provider callbacks and already queued jobs. A request-time check cannot stop a later worker. |
| Project and deletion | `projects/api.py` ProjectAPI, summary reset and project task deletion; project/model and organization deletion | Prevent update/delete, bulk/cascade and soft-delete from bypassing archive; define retention and privileged legal deletion separately. Existing ORM deletion coordinators must be respected. |
| Export and audit | `data_export/api.py`, approved delivery and review audit worker/download paths | Distinguish authorized reads from creation, conversion, deletion and external delivery. Preserve existing immutable release boundary and recheck publication. |
| Side effects | webhooks, ML backends, storage signals and background jobs | Enumerate effects and admission/revocation points; no external action may be assumed rolled back with a DB transaction. |

### Concurrency and external side effects

For database mutations, take the same Project row lock before dependent rows and
hold it through commit. A transition acquiring that lock waits for earlier
admitted writes; writes acquiring it after transition observe the new state and
fail. State checks without the lock, stale model instances and enqueue-only
checks cannot establish this ordering. Preserve existing lock order and test on
PostgreSQL with separate connections, including role revocation and project
deletion, rather than inferring it from SQLite.

External storage/network effects cannot be rolled back by a database lock. Before
enabling pause/archive, define a per-project admission and drain protocol for
each worker and provider: pause blocks new work, waits for admitted work to reach
a documented safe completion point, and only then reports the state transition
as effective. If a provider cannot fence or reliably drain an in-flight effect,
disable that path or reject the transition; a state flag alone is insufficient.
Queued and retried work must revalidate current state and authority at execution.
Capture job identity and terminal outcome without exposing credentials. Do not
hold a database transaction open across arbitrary network operations as a
substitute for this protocol.

## Migration, rollout and rollback

First deploy additive schema and compatibility reads with lifecycle enforcement
disabled. Existing projects require an explicit, reviewed mapping: legacy flags
may inform a migration report but cannot establish completion or archive. In
particular, an unpublished non-draft project must not be guessed archived. Keep
unresolved rows in an explicit legacy/unclassified state and disallow enabling
enforcement for them until an authorized operator resolves them. New projects
need an explicit creation rule. Preserve flag values and existing task FSM data;
do not dual-write legacy flags without a separately specified compatibility
contract. Migrate and verify counts, history and permissions on a production-like
copy before activation.

Use an expand/contract deployment: deploy schema and compatible code to all web
and worker versions, drain incompatible jobs/processes, validate coverage and
then enable the provider. Rolling back to code that ignores a paused or archived
state would reopen writes: disable traffic and workers or retain a compatible
enforcement layer before rollback. Preserve state and events; never claim a
schema rollback alone restores enforcement. Test upgrade, interrupted rollout,
rollback and re-enable with in-flight jobs and retained historical evidence.

## Acceptance gates

* Table-driven state/operation and transition tests, unknown-state default deny,
  Manager/non-Manager and cross-project IDs, forged actor/reason, stale revision,
  duplicate retries and event/state atomicity.
* PostgreSQL races in both orders for transition versus Editor draft/submit,
  assignment, review, import, bulk deletion, role revocation, project deletion
  and every covered background commit. Verify old tabs and queued/retried work.
* Provider-specific drain/failure tests for in-flight external side effects;
  verified immutable history and authorized downloads remain readable while
  source mutation and unauthorized cross-project reads fail.
* Upgrade and rollback with representative old flag combinations and active
  tasks; bilingual UI E2E for status, permitted controls, stale writes and
  actionable error reasons. Run existing authorization regressions on final head.

No gate is satisfied by this document. #86 remains open until the implementation,
independent review and actual-environment acceptance establish the claimed scope.
