# Frozen NER review to arbitration handoff

This optional Fork slice links a selected, exact frozen NER conflict review source
to the existing NER arbitration lifecycle. A Manager explicitly dispatches a new
task or links a pending/resolved task with the same complete semantic input.
Arbitration never approves, rejects, reworks or releases a Submission.

## Source and storage

New commands require a NER priority ReviewBatch, selected frozen conflict evidence,
the exact original candidate and NER source pins, and the current latest pending
Submission with no ReviewDecision. Plain/old/classification batches, unavailable
sources, unselected or mismatched revisions cannot be upgraded by calling the API.
Other cohort revisions remain frozen; no latest-source substitution occurs.

NerReviewArbitrationHandoff explicitly references NerArbitrationTask. Classification
models, namespaces and key contracts are unchanged. The new record freezes review
plan, source Manifest/rules/calculation/input hashes, both source and target
provenances, target status/version at linking, authenticated principal/provider
source/actor and the original command receipt. The six-field legacy review
candidate is enriched only in this new proof with assignee_id from its verified
NER source pin. Historical ReviewBatch plans are not rewritten.

No text or entity answers are copied into the new ledger. Django uniqueness and
PostgreSQL/SQLite immutable guards protect scope, primary key, frozen references
and receipts. Nullable convenience links allow SET_NULL but never non-null
rebinding; frozen IDs cannot restore deleted links. Project cascade is unchanged.

Equal semantic inputs from different NerBatch origins can link, but each original
full Manifest and the complete task item must independently validate. A matching
task ID, pair or F1 is insufficient. Loss of target origin B cannot be repaired by
substituting review origin A. Cancelled targets require an explicit new dispatch.

## Authority and routes

ANNOTATION_NER_REVIEW_ARBITRATION_HANDOFF_ENABLED defaults to false. Existing
review, NER priority, NER batch/disagreement and NER arbitration gates and exact
Fork review maps must independently admit the current organization/project.
Core-only-review and unknown scopes default deny. No Host/Core integration,
Token type or policy store is added.

Under /api/projects/{P}/review-batches/{B}/:

| Route | Contract |
| --- | --- |
| GET sources/{S}/ner-arbitration-context/ | Manager eligibility, verified frozen context, existing target and current write capability |
| GET/POST sources/{S}/ner-arbitration-handoffs/ | Scoped history or explicit dispatch/link |
| GET ner-arbitration-handoffs/{H}/ | Frozen association and authorized current target metadata |

POST requires UUID Idempotency-Key, mode, expected_plan_hash,
expected_input_set_hash and expected_viewer_hash. Dispatch adds assignee_id;
link_existing adds ner_arbitration_id and expected_arbitration_version.
Unknown/actor/answer/status fields reject. The typed command hash includes route
project/batch/source IDs and canonical body. Schema is
review-ner-arbitration-handoff/v1; classification routes retain their old schema.

Writes and recovery require current Manager + READ_WORK +
ORGANIZE_NER_ARBITRATION. Classification operations grant no NER authority.
Reviewer history intersects current visible review candidates, whole frozen
self-cohort exclusion and the current independent assigned NER target scope before
counts, pagination and source loading. Annotators cannot use these routes.
Existing NER adoption and independent security cancellation remain separate,
including the existing known-ID security-cancel route after READ_WORK withdrawal.

## Atomicity and recovery

Reuse the project-first fence, target-before-source lock order, sorted source
batches, full source replay and render-time authority/representation checks.
New NER task and association commit together or both roll back. The shared
transaction-local creation helper binds preverified evidence to its full origin;
it never invokes another HTTP view or repeats snapshot/scorer work.

The parent handoff UUID has a separate server-private child create UUID. Neither
transport borrows the other's receipt. Same owner/principal/provider/body/route
recovers the original parent receipt before current source availability and
processing limits; current write authority and the fixed v1 receipt envelope
still apply. Different intent or owner conflicts. Old keys after cancellation
return the old association, never redispatch. Linking uses the target's explicit
CAS version and does not reassign or restore withdrawn adoption rights.

## Budgets and presentation

Full fixed v1 review plans and NER Manifests are bounded before target narrowing.
Distinct input metadata is limited to 4 MiB canonical / 16 MiB SQL JSON. A target
cohort has at most 20 sources; each of its initial and final hydrations has the
existing 64 MiB SQL / 8 MiB canonical snapshot and work-per-pass ceilings.
SQL CASE byte gates precede JSON decoding, including page totals.
History reserves the complete page's proofs before reading target metadata;
target origin, participant IDs and cancellation JSON share one predecode SQL
gate and all contribute to the same aggregate budget. Validating a resolved
target also loads its decision winner and reason: both are reserved against the
remaining cumulative budget before decoding. The existing full decision validator
consumes that bounded proof without a second unaccounted read. This applies to
context, new links and history, even though history emits neither winner nor reason.
Current source status
is suppressed if the task/assignment scope or complete source pin no longer matches.

Verified context, fresh dispatch and fresh link each make exactly two official
counter passes over one prepared cohort: 2Q <= 380, excluding double-empty pairs.
Context plus POST may therefore cost 760 calls; opening the existing detail is
another explicit request. Final source replay follows the final counter.
No complete review-candidate answer scan and no nested 4Q detail calls occur.
Commands are at most 16 KiB, proof 64 KiB canonical / 256 KiB SQL, response 1 MiB.
History pages default to 50/max 100 with aggregate metadata/output ceilings.

History and receipt recovery load no answer snapshots and perform no scoring.
History source_integrity is not_checked; available means retained links/current
target metadata can be read, not that source evidence was verified. Opening the
existing NER detail performs its own checks. New handoff views mask full winner,
reason and evidence on unavailable source verification.

The existing complete handoff history/create/return and NER arbitration
workspaces are reused through typed transports and ACK validators. New ACKs bind
schema, actor/viewer, project/source/input, parent key, canonical request hash and
dispatch assignee. Missing or inconsistent ACKs remain unknown; refresh never
POSTs. Explicit recovery retains the exact parent body/key, identity changes scrub
old state, and a same-identity read failure preserves an acknowledged save.
Navigation preserves batch/page/filter/focus with fresh reads.

Command SHA-256 uses the existing locked @noble/hashes implementation, declared
as a direct dependency. It also works on supported HTTP origins where Web Crypto
SubtleCrypto is unavailable; canonical request hashing and ACK validation remain
mandatory. No server TLS setting or authentication boundary is changed.

## Validation and retained environment boundary

A dedicated ner-review-handoff workflow runs PostgreSQL safety/concurrency and
bilingual browser journeys. Coverage includes independent dispatch races,
decision/cancel CAS orderings, original owner/provider/key recovery, source loss,
20-person/380-counter limits, empty revision adoption, render-time rollback,
wrong ACKs and current revocation. Existing classification, NER and review
regressions remain required.

Local tests use a fresh report-owned native PostgreSQL cluster and disposable
synthetic databases/server. Docker/#76 are not started, stopped or restarted.
Host and Core are not operated; there is no merge or deployment authorization.
#81/#84 remain open. OLD_AND_NEW_CHANGED,
CHANGED_DATABASE_CONTAINER_REPLACED, protected-worktree missing history,
cause UNKNOWN and NOT_RUN_AFTER_DOCKER_INTERRUPTION remain independent records;
CI and local test success do not establish retained runtime equality.

### Local acceptance on 2026 10 29

The full classification/NER/review PostgreSQL regression run passed 471 cases.
The final handoff run passed 38 cases, including 14 independent-connection races
and one new moved-task metadata-scope negative added after the full run (37
cases overlap). Frontend shared-flow suites passed 222 cases; real isolated
PostgreSQL en-US/zh-CN browser journeys both passed. The production build,
migration check, scoped Ruff/Blue and NER Biome checks passed. The 25 existing
Biome errors in five shared legacy files match the inspected main baseline by
category/count; this slice does not reformat those complete workflows.

Only this task's synthetic native PostgreSQL and loopback server were started
and then stopped. Raw logs, failure history, screenshots and resource receipts
are retained in the local ls84-ner-handoff-impl-20261029 report. The branch is
local; GitHub CI is NOT_RUN pending separate publication authorization. No
merge, deployment, runtime activation or Issue closure is implied.
