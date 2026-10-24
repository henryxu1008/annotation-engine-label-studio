# Frozen text classification arbitration (#81, first slice)

This local Fork ledger lets a Manager dispatch one persisted comparable disagreement
to an independent current Reviewer. The Reviewer selects one complete existing
frozen Submission revision and records a reason. Only ArbitrationTask and
ArbitrationDecision change. Submission, ReviewDecision, annotation approval,
sampling, release and Host DatasetVersion do not change. #81 and #84 remain open.
NER, boxes, field composition, reassignment, bulk decisions, automatic rework and
cross-system arbitration are outside this slice.

## Authority

All three independent feature flags (classification disagreements, durable batches,
arbitration) default off. Both classification-disagreement and arbitration mode maps
must contain the exact `organization_id:project_id` with value `fork_review`.
Core-only-review, unknown modes, unsupported databases and inactive memberships
fail closed. Actor identity comes from resolve_actor, including trusted principal
mapping; request actor fields are rejected.

Current Manager membership, READ_WORK and ORGANIZE_ARBITRATION are required to
dispatch. Only the assigned current independent Reviewer with READ_WORK and
ARBITRATE may adopt. Frozen participants include assignment owners and submission
authors. Manager/global organization privilege does not grant Reviewer adoption
authority. Reviewer scope excludes unassigned/self-participant records before SQL
counts and pagination. Eligible assignees are current project Reviewers in the same
active organization, excluding the project creator and all frozen participants.

Cancellation uses SECURITY_CANCEL, current Manager authority and both mode checks.
The direct API remains available after READ_WORK is disabled or source loss, with
a metadata-only receipt. The UI exposes readable queues only with READ_WORK.

## Frozen identity and integrity

Dispatch requires a persisted ClassificationBatch ID, task ID and exact full batch
manifest hash. Copy the entire cohort, pinned revision IDs/hashes, original
selection/metric rule, calculation time and batch provenance into an immutable
origin. Do not use current task payloads, mutable annotations, newest revisions,
remote URLs or storage resolvers.

The semantic input hash includes project/task, rule, revision-selection contract and
the entire assignment/source cohort; it excludes batch name/time/ID. One pending
or resolved arbitration per semantic input set is enforced in PostgreSQL and SQLite.
Explicit cancellation permits explicit redispatch. New revisions require a newly
frozen analysis; they cannot replace the existing winner.

Each evidence read and new adoption revalidates the original batch/copied origin,
snapshot/config hashes, assignment/task/project/submitter graph and source access.
Source loss or corruption masks the whole comparison and question while preserving
decision metadata and original receipts. Reviewers get a task-scoped input hash,
never the full batch hash or other tasks' manifests. Question strings render as
escaped text from referenced frozen inline Text fields only.

## API

Base: `/api/projects/{pk}/classification-arbitrations/`.

| Route | Authority | Contract |
| --- | --- | --- |
| GET capability/ | Manager/Reviewer | capabilities and limits; no answers |
| GET assignees/ | dispatch-capable Manager | exact batch_id/task_id/expected_manifest_hash; paginated eligible Reviewers |
| GET collection | Manager or scoped Reviewer | state=pending/resolved/cancelled/all, page, page_size <=100 |
| POST collection | Manager | batch_id, task_id, expected_manifest_hash, assignee_id |
| GET {id}/ | Manager or assigned independent Reviewer | verified frozen evidence or unavailable; decision metadata |
| POST {id}/decision/ | assigned independent Reviewer | expected_version, expected_input_set_hash, winner_submission_id, reason |
| POST {id}/cancel/ | Manager | expected_version, expected_input_set_hash, reason |

POSTs require a UUID Idempotency-Key. Unknown fields, malformed/unsafe IDs and empty
or over-2000-character trimmed reasons fail. DRF's standard text validators reject
NUL and unpaired Unicode surrogates with 400 before hashing or persistence. Pending version 1 may become resolved
or cancelled version 2 once. The winner must match one complete frozen source pin;
clients cannot submit a replacement answer.

## Transactions, recovery and limits

Lock order: shared project-operation fence, related identities, arbitration row,
source task/assignment/submission rows. Source locks use separate base-table
queries, not nullable joined FOR UPDATE. Keep writes and the project fence through
response rendering and final authority, source, assignee and receipt verification.
Concurrent dispatch is serialized; concurrent decision/cancellation has one winner.

Receipts bind project/resource/action, principal, resolved actor, UUID and canonical
body. Under current authority, recover an original receipt before CAS, source
availability or new assignee eligibility. Same-key recovery returns the committed
record after terminal transition/source loss; altered body/actor/principal cannot
reuse it. Revoked actors cannot recover receipts or answers. Browser uncertain
acknowledgements retain body/key, permit read-only refresh and require explicit
recovery. No automatic POST or rekey occurs.

Read responses carry an ephemeral `viewer_hash` scoped to principal/source, actor,
project/organization and role. It is a salted HMAC for correlating concurrent
capability and data responses, not an authorization credential. The browser drops
mixed viewer contexts and explicitly denied reads; write controls require both
current capability and object-level permission. The pin is never accepted in POST
bodies or persisted in origins/receipts. Secret rotation changes this read pin but
does not invalidate original-key recovery. Server authorization stays authoritative.

Limits are inclusive: 20 participants, 8 MiB total canonical source snapshots,
64 KiB total UTF-8 bytes across referenced frozen inline Text fields; reason is
1..2000 trimmed characters. Over-budget new dispatch/adoption returns 422 without
a task/decision. Metadata, cancellation and original-key recovery stay available.
Pre-existing classification batch budgets still apply.

Migrations 0004/0005 add unique/partial/check constraints and PostgreSQL/SQLite
immutable-update triggers. PKs, identity pins, input sets, receipts and terminal
states cannot mutate. Nullable actor FKs may clear for user deletion; frozen IDs
remain. Pending-to-resolved requires a decision row. Project deletion follows the
existing local project-cascade contract.

## Verification and upgrade surface

Tests cover direct permission bypass, revocation/remapping, source/foreign graph
corruption, revision pinning, budget edges, unchanged submissions/review decisions,
SQL immutability, CAS and replay after source loss. PostgreSQL separate-connection
tests cover dispatch deduplication, same/different decision keys, both decision/cancel
orderings, lock waits, revocation and render-time rollback. English/Chinese synthetic
browser journeys cover dispatch, cancel, redispatch, adoption, lost acknowledgements,
original-key recovery, unassigned/annotator denial, revocation and source loss.

Fork PR Gate covers SQLite and full PostgreSQL regressions. The separate
Classification Arbitration E2E job uses a fresh database and guarded synthetic seed,
preserving the existing enterprise browser job's journeys and time budget. Recheck
source projection, project fencing, mapped identity, triggers, capabilities/DTOs,
modal lifecycle and catalogs on upstream upgrades. No Host, Core, token, deployment
or release authority is introduced.
