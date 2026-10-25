# Exact review-to-arbitration handoff (#84)

This slice links selected frozen conflict-priority review sources to the existing
text-classification arbitration workflow. It is not a second review/arbitration
decision ledger, an automatic approval policy or Host integration. #81/#84 remain
open for their other modalities, confidence rules, aggregation and second review.

## Scope and provenance

New handoffs require a selected frozen-conflict candidate in a priority review
batch, its current latest pending Submission, and verified complete classification
inputs. Plain old batches, unknown/mismatched revisions and unavailable inputs
cannot create a handoff. No analysis or replacement revision is generated.

ReviewArbitrationHandoff freezes the review batch/plan hash and exact candidate,
classification origin/manifest/rules/calculation hash, target arbitration identity,
origin/input hash, version/status at linking, creator principal/actor/time and
original UUID/request hash. It copies no answer snapshots. PostgreSQL/SQLite
guards freeze all fields and the PK, permitting only nullable FK cleanup. Source
deletion leaves frozen IDs and receipts; project cascade deletion is unchanged.

The existing semantic arbitration uniqueness excludes batch names, timestamps and
IDs. A Manager therefore explicitly chooses new dispatch or linking an existing
pending/resolved task. Equal inputs from different classification batches retain
both origins. Cancellation keeps previous associations; an explicit new dispatch
may create a new target. An old key never means redispatch.

## Routes

Under /api/projects/{P}/review-batches/{B}/:

| Route | Contract |
| --- | --- |
| GET sources/{S}/arbitration-context/ | Manager eligibility, frozen reference, existing target and current write capability |
| GET sources/{S}/arbitration-handoffs/ | visible paginated history, default 50/max 100 |
| POST sources/{S}/arbitration-handoffs/ | explicit dispatch or link_existing, UUID Idempotency-Key |
| GET arbitration-handoffs/{H}/ | frozen provenance plus authorized current status/result summary |

POST always includes mode, expected_plan_hash and expected_input_set_hash.
Dispatch adds assignee_id; link_existing adds arbitration_id and
expected_arbitration_version. No actor, answer, reviewer or approval fields are
accepted. Client IDs/hashes are expectations, never authority.

## Authority and atomicity

Default ANNOTATION_REVIEW_ARBITRATION_HANDOFF_ENABLED=false. Existing review,
classification/batch and arbitration flags and exact fork_review mode mappings
must independently permit the scope. Unknown/Core-only modes and unsupported DBs
deny all new routes. No retained runtime is activated by migration or tests.

Writes require a current Manager with READ_WORK and ORGANIZE_ARBITRATION.
Read history intersects current review candidate visibility and existing scoped
arbitration visibility before counts/pagination. Other reviewers and annotators
gain no answer access. Existing arbitration decision/cancellation APIs retain
their original independent authority, CAS and receipts.

The old arbitration create endpoint and handoffs share one transaction-local
creation service. Keep the project-first fence and existing lock ordering through
rendering and current identity, source, target and representation checks. New
dispatch writes both records or neither. A changed target/version returns 409.

Same key/body/principal/actor returns the original handoff receipt, before live
source checks. Source/target loss, later revisions or terminal transitions never
create a substitute. Revocation still denies recovery; corrupt receipts reject.
Different bodies/actors and different keys for an existing association conflict.
Existing standalone create key/hash semantics are preserved.

Targeted verification loads the candidate's metadata and the complete frozen
task cohort, not every review candidate's answers. Existing 20-participant,
8 MiB canonical snapshot and 64 KiB frozen-text limits remain inclusive. Overflow
returns 422 before a new record is committed; metadata receipt recovery does not
reload over-budget sources.

## UI and validation

Reuse the existing arbitration dispatch/assignee/detail/decision/cancel views.
The adapter uses the handoff write transport; old entries retain their transport.
Returning restores review batch/page/filter/focus with fresh reads. Keep only
navigation context while leaving a view, not old permissions or answer evidence.
Uncertain commands retain their original body/key within the open workspace and
require explicit recovery; read refresh never POSTs or switches to another key.

Arbitration and source-review states are shown independently. No adoption approves
a Submission, changes sampling or grants release authority.

Backend regressions cover SQL immutability, source identity/loss, authority and
render-time rollback, legacy keys, exact budgets and targeted read cost.
PostgreSQL separate connections cover duplicate dispatch/link, decision/cancel
orderings, revocation and new-revision races. A dedicated synthetic bilingual
browser workflow exercises the full handoff and return path without enlarging
the existing Enterprise E2E job. Local Docker/#76 remain read-only.

### Retained runtime observation during implementation

The 2026-10-25 read-only preservation check found the baseline #76 database
container ID missing, with a newly created container using the same name
(created 12:13:23 UTC, observed running/healthy). The other five protected
container fingerprints and the protected worktree matched the implementation
baseline. This window is recorded as CHANGED_DATABASE_CONTAINER_REPLACED; the
cause and the missing container's current fingerprint are UNKNOWN. This task
issued no Docker start/stop/restart/remove commands and does not infer database
content integrity from the replacement container's health.

The prior runtime CHANGED incident, initial exit cause UNKNOWN and
NOT_RUN_AFTER_DOCKER_INTERRUPTION full-database comparison remain independent
historical evidence. No current read-only observation erases those records.
