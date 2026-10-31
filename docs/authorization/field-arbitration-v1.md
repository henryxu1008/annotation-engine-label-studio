# Frozen static-field revision arbitration V1 (#81)

This optional Fork ledger adopts one complete existing frozen Submission from a
persisted FieldBatch disagreement. It writes only FieldArbitrationTask and
FieldArbitrationDecision. Original annotations/Submissions, ReviewDecision,
sampling, quality-overview receipts, release/export and Host/Core remain separate.
#81 and #84 remain open.

## Frozen scope and authority

Require the exact FieldBatch/task/full Manifest hash; manual/overview receipts
are not durable origins. Validate the complete fixed field-batches/v1 envelope
before loading the target cohort. Keep active-assignment-latest/v1,
field-lf-ascii-trim/v1, field-value-differences/v1,
python-string-equality/v1 and field-raw-difference-queue/v1 unchanged.
Format-only raw differences remain conflicts even with normalized agreement 100%.
Whole no-values, missing or malformed cohorts cannot dispatch. A complete explicit
empty-valued revision can win against informative peers. There is no field
composition, automatic winner, replacement answer or normalization writeback.

Default ANNOTATION_FIELD_ARBITRATION_ENABLED=false. Existing field/batch flags
also apply. Both field-analysis and field-arbitration org:project mode maps must
contain exactly fork_review; missing/unknown/Core-only modes deny access. Only
PostgreSQL/SQLite are supported; no retained flag is activated by migration.

A current Manager with READ_WORK and ORGANIZE_FIELD_ARBITRATION dispatches.
Only the assigned current independent Reviewer with READ_WORK and ARBITRATE_FIELD
adopts. Every frozen assignee/submitter is excluded. Manager/global status does
not grant adoption. Filter before counts/pagination/loading. Annotators and
unassigned Reviewers gain no answer access. Identity comes from current trusted
session/token resolution, never request actor/reviewer fields.
SECURITY_CANCEL is a current-Manager metadata action while work policy/source
availability blocks evidence; feature/mode boundaries still apply.

## API and immutable ledger

Base: /api/projects/{P}/field-arbitrations/

| Route | Contract |
| --- | --- |
| GET capability/ | Current viewer, independent permissions and limits |
| GET assignees/ | field_batch_id/task_id/expected_manifest_hash; eligible Reviewers |
| GET collection | Scoped state/page/page_size, at most 100 metadata rows |
| POST collection | field_batch_id/task_id/expected_manifest_hash/assignee_id |
| GET {A}/ | Verified matrix/selected pair or masked unavailable source |
| POST {A}/decision/ | expected_version/expected_input_set_hash/winner_submission_id/reason |
| POST {A}/cancel/ | expected_version/expected_input_set_hash/reason |

POSTs require UUID Idempotency-Key and strict positive non-boolean safe IDs.
Schema field-arbitrations/v1 and rule adopt-exact-field-submission/v1 freeze the
complete target cohort, assignment/source/revision/result/config/task-data pins,
original rules/results/cost/hash, batch reference/time/hash and authenticated
dispatcher identity. Raw answers/documents are not copied into the ledger.
Field cohorts retain assignment-ID order; displayed inputs use Submission-ID
order. Origin IDs remain strict integers even when numeric equality would pass.

The semantic hash excludes cosmetic batch names/IDs/times but includes the full
target/rule/input contract. One pending/resolved row exists per semantic input.
Pending/version 1 becomes resolved or cancelled/version 2 once. Explicit
cancellation permits explicit redispatch, never implicit receipt replay.
PostgreSQL/SQLite freeze PKs, origins, identity, decisions and terminal states.
Nullable user-FK cleanup preserves frozen IDs. Project cascade deletion follows
the existing local-ledger contract.

## Fencing, replay and source loss

Reuse the project-first fence and identity/member/User locks. Commands lock the
existing arbitration before ordered target Task/Assignment/Submission base rows;
fresh dispatch locks ordered source rows before inserting its new row. Avoid
nullable-join FOR UPDATE. Hold the fence through rendering, final authority/
organization/source/assignee and representation checks. Failure rolls back new
rows and leaves the original key reusable.

Receipts bind action/resource, exact canonical body, UUID, principal/provider
and frozen actor. Under current authority, validate/recover the committed local
receipt before CAS, current source availability, new assignee eligibility or
changed processing budgets. Changed body/identity conflicts. An old cancelled
create receipt never redispatches. Revocation denies recovery.
The ephemeral viewer pin does not replace authority or rewrite stored commands.

Source loss, corruption or rebinding masks all field values/comparison and
blocks fresh adoption. Authorized decision metadata and original receipts remain.
Only exact original restoration restores evidence; newer revisions never replace
a pin. Final hydration follows the last equality. Final authority binds
organization even when principal/actor/role stay unchanged.

## Bounded work and truthful cost

Reuse the fixed full FieldBatch envelope: 1 MiB canonical / 4 MiB SQL JSON.
Origins fit 64 KiB canonical / 256 KiB stored; participant/cancellation/winner
JSON also have predecode SQL guards. At most 100 origins per metadata page means
6.25 MiB canonical / 25 MiB stored, with 1 MiB final response. Metadata is bounded
per materialization (two validation passes can read 12.5 MiB canonical / 50 MiB
stored) without pretending its JSON work is zero I/O.

Target limits: 20 participants, 20 fields, 4 KiB/raw value, 64 KiB per revision's
raw values/text/XML, 64 MiB SQL snapshots and 8 MiB canonical snapshots per
hydration. Preflight and hydrating-SELECT aggregate CASE gates precede JSON decode,
including concurrent growth.

Verified detail/fresh dispatch/adoption have two hydration/calculation passes.
Each pass performs I*F normalizations and 2*C(I,2)*F equalities, at most 400/7,600;
request maxima are 800/15,200, with zero external scorer calls.
Second calculation uses prepared immutable inputs; final hydration only inspects
and verifies current sources. No third calculation or full-batch/overview HTTP
fan-out occurs. Pair selection/pagination cannot lower the target budget.

processing_cost records validation/hydration/calculation passes, per-pass/total
normalizations/equalities and scorer_calls=0. Unavailable reads can still hydrate.
Capability/list/assignees/cancel/original-key recovery load no snapshots and score
nothing. Commands fit 16 KiB, trimmed reasons 1..2,000 characters and output 1 MiB.
Calculation-dependent output is checked exactly after rendering. Rejection emits
no result and rolls back a new command, without claiming zero performed work.

## Existing workflow and acceptance

Reuse FrozenArbitrationWorkspace through typed field transport/evidence/ACK/cost
validation and FieldResult. The path is frozen batch conflict -> explicit
Manager dispatch -> independent Reviewer whole-revision inspection/adoption ->
return to the original batch/page/filter/focus. No field review handoff or
sampling strategy is added.

Bind retained state to arbitration/input/viewer and immutable task evidence.
Update retention only after the active generation is accepted. Blur, denial and
identity changes clear answers; late responses cannot poison newer state.
Read refresh never POSTs. Unknown commands retain original body/key for explicit
recovery. A same-viewer read failure does not erase a confirmed save.
Verify receipt payload/request hashes, actor/resource/key and winner before ACK.
Render answer values as text.

Coverage includes field/empty/missing/format vectors, Token/role/revocation and
Core-mode negatives, strict origin/source identities, immutable SQL state, CAS/
key/body recovery, exact work/byte limits and no business-state writes.
Independent PostgreSQL connections cover dispatch/decision/cancel races,
render/final-counter mutations, organization switches and preflight/hydration
growth. Real synthetic en-US/zh-CN journeys cover dispatch/cancel/redispatch,
empty revision adoption, lost ACK/recovery, source restoration/new revisions,
narrow layout, Annotator denial and Reviewer revocation.
field-arbitration.yml defines an independent disposable CI environment; a
definition is not executed GitHub acceptance. Actual results are recorded
separately.

No Host change, merge, deployment or Core integration is implied. Docker/#76
are not operated/restarted. Preserve old/new CHANGED, database replacement,
cause UNKNOWN, protected-worktree-missing history,
NOT_RUN_AFTER_DOCKER_INTERRUPTION / unfinished full-database comparison and
the shared Biome baseline. Isolated tests cannot establish retained DB equality.
