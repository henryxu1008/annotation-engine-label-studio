# Optional frozen field conflict priority review sampling

This default-off Fork strategy consumes one existing FieldBatch in the complete
ReviewBatches workflow. #81 and #84 remain open. Field review to arbitration
handoff, bbox, confidence and second review remain separate. Submission, field
metrics, ReviewDecision, standalone arbitration, release and Host are unchanged.

Use `priority: {kind: "field_conflict", batch_id, manifest_hash}` in the existing
preview/create endpoints. The Manager explicitly selects one frozen batch; no
automatic analysis, newest-batch selection or overview/manual-receipt fallback.
Without priority, legacy algorithms, plan shapes/hashes and receipts retain
their existing semantics. No table or migration is added.

Keep the latest-pending-per-assignment candidate pool, package/submitter
intersection, overlap deduplication and global/per-submitter ceil quotas.
`field-conflict-ranked/v1` ranks verified conflict first, then canonical SHA256
of algorithm/project/seed/candidate identity, then Submission ID. Agreement and
unknown share the remaining seeded tier. Conflict never raises the quota.

The existing raw-difference queue rule is authoritative. A defined pair with
raw_matches below denominator produces conflict, including format-only raw
differences with normalized agreement 100%. Both normalized-empty fields are
excluded; no_values, missing/malformed inputs and unavailable cohorts are not
agreement. A task-wide conflict describes the cohort, not which actor was wrong.

Validate the entire guarded field-batches/v1 Manifest, all source graphs and
frozen calculation hashes, including tasks outside review filters. A matching
candidate must retain Submission/assignment/assignee/task/revision/submitter and
result/config/task-data hashes. Task ID alone cannot transfer an old conflict.
Uncovered, revision_mismatch and metric_unavailable are explicit unknown states.
Originally null inputs stay null even after later completion. Any lost/corrupt/
rebound non-null origin blocks fresh planning, rather than becoming unknown.

`review-field-priority/v1` freezes the complete candidate denominator/selection,
organization, origin ID/full Manifest hash, field rule/selection/explanation/
engine/queue versions, creator principal/provider/actor, whole frozen participant
sets and per-candidate reason/hash/exact matched source_pin. It copies no answers
or snapshots. Local validation checks strict IDs, structure, pin relationships,
state/reason consistency, seeded selection and canonical hash.

Require ANNOTATION_FIELD_REVIEW_PRIORITY_ENABLED, existing review/field/batch
flags and both exact org:project fork_review modes. Defaults remain false.
Manager planning/create/recovery requires READ_WORK and ORGANIZE_REVIEW plus
field read scope, independently of ORGANIZE_FIELD, REVIEW or arbitration.
Current Manager/Reviewer reads require READ_WORK and both scopes. A Reviewer's
whole frozen self-cohorts and own other candidates are excluded before counts,
pagination and source loading, including direct batch-context review/rework.
Direct review identity/lock lookups defer immutable snapshots; package scope
lookups load IDs only. Both decision validation and response serialization use
the shared bounded loader, followed by a final source/representation check in
the same write fence. A generic ORM refresh must not bypass that loader.
All-hidden batches are absent/404. Reviewers receive only visible generic
reasons, without full origin hash/cohorts or peers' answers. Current REVIEW and
all ordinary selected/pending/exact-source/no-self-review constraints remain
independent. Existing classification/NER handoff routes reject this algorithm.

Source loss or later decisions/revisions never recalculate historical selection
or shrink its denominator. Detail/queue/rework validate ordinary review sources
without computing field scores or loading other field answers. Matched review
sources must still retain their exact stored pins. Field answer inspection uses
the original FieldBatch endpoint and its current authorization/source checks.
Exact restoration can restore that evidence; newer revisions cannot replace it.

Before any snapshot decoder, enumerate at most 1,000 candidates and validate
the 1 MiB canonical/4 MiB stored Manifest. Deduplicate all candidate and complete
field-source IDs into one guarded union, at most 3,000 rows. SQL preflight and
the hydrating SELECT aggregate CASE both enforce 64 MiB; canonical snapshots
fit 8 MiB before inspection/calculation. Field scope remains 100 tasks, 20
participants/task, 2,000 cohorts, 20 fields, 4 KiB/raw value and 64 KiB each for
revision raw values, inline text and XML. Whole-batch work is at most 400
normalizations and 7,600 equalities/pass. Filters/pages/rate=100 do not reduce it.

Inspect all inputs, preflight CPU and the complete 1 MiB plan before calculation.
Calculate once, render the actual response, calculate again on the same bounded
prepared inputs, then rehydrate/reinspect current sources after the last equality.
No third calculation or nested two-pass HTTP endpoint. Return actual processing
counts: two hydrations/calculations, at most 800 normalizations/15,200 equalities
per request, external scorer_calls=0. All-empty inputs still cost real loops;
incomparable inputs may cost zero. Output-dependent response checks occur after
calculation; failures do not pretend performed work was zero.

New field plans fit fixed v1 1 MiB canonical/4 MiB stored envelopes. Collection
retains existing 50/100 pagination but gates all field plans in the page at
32 MiB stored before decoding and 8 MiB canonical cumulatively. Actual responses
fit 1 MiB. Legacy plan envelopes are not reinterpreted.

Creation holds Project -> identity/member/User -> FieldBatch -> ordered Task ->
Assignment -> Submission locks through rendering, final replay and current
principal/actor/organization/role/policy/mode checks. Failure rolls back the new
row and leaves the key reusable. Original project/UUID/body/expected hash and
principal/provider/frozen actor recover the committed receipt before source,
engine or changed processing budgets. Recovery still requires current create
scope, fixed local v1 plan integrity and bounded response; it hydrates/scores
nothing. New organization scope cannot borrow an old receipt.

The existing selector, preview, save, queue, progress and rework navigation are
reused through explicit typed adapters. Viewer salts for FieldBatch and review
capabilities stay separate. Current generation/viewer checks bind previews and
ACKs. UNKNOWN writes retain their exact body/key for explicit recovery; GET never
POSTs. Changed identity clears old commands/evidence; same-viewer read failures
preserve confirmed save facts. Malformed key/hash/viewer/cost ACKs stay unknown.

Acceptance covers full frozen rules and pins, actual boundary work, union/page
byte guards before decode, original-key recovery, real Tokens, whole-cohort
isolation, immutable database state, independent PostgreSQL concurrency and
bilingual browser review/rework/recovery. Executed evidence belongs in the local
acceptance report; workflow definitions alone are NOT_RUN, not hosted CI proof.

Docker/#76 and retained worktrees/services/data are not test resources. Preserve
OLD_AND_NEW_CHANGED, CHANGED_DATABASE_CONTAINER_REPLACED, cause UNKNOWN,
NOT_RUN_AFTER_DOCKER_INTERRUPTION, unfinished database comparison, missing
protected-worktree history and existing shared Biome baselines. Isolated success
cannot establish retained runtime/database equality. No push/PR/merge/deployment
or real Core integration is implied by local acceptance.
