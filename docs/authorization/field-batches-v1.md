# Durable frozen plain-text field queues V1 (#81)

This is an independently enabled Fork analysis feature. It creates only an
immutable `FieldBatch`; reading a queue never writes annotations, Submissions,
review decisions, arbitration or sampling state. #81 and #84 remain open. There
is no Host integration, release/export authority or Core-only-review fallback.

The static Text/TextArea support, raw and normalized fractions, empty/missing
boundaries and explanation rules are unchanged from
[field-disagreement-v1.md](field-disagreement-v1.md). There is no new scorer or
third-party dependency. Existing classification/NER batches and manual field
receipts are separate contracts and are not migrated or reinterpreted.

## Scope, selection and freezing

A manager supplies a name and either explicit task IDs or an explicit project
scope. Project scope enumerates once at preview; it never means the current
Data Manager page or selection. Tasks sort by ID. Queue filtering defaults to
`disagreement`, with `all`, `agreement` and `unavailable` also supported. Queue
pagination is 50 by default and at most 100; the batch list is at most 8.

`active-assignment-latest/v1` freezes every active assignment for each selected
task, in assignment-ID order, and its highest `(revision, id)` Submission. It
does not compare revision numbers across assignments or fall back to an older
approved revision. A missing Submission freezes `source=null`. Fewer than two
participants, a missing participant or an unusable latest revision makes the
entire task unavailable. No participant is dropped to improve a score.

`field-batches/v1` stores scope, creator principal/source/actor, enumeration and
calculation times, ordered cohorts, source pins, result summaries, logical-result
hashes and costs. Each source pin binds task, assignment, assignee, submitter,
Submission, revision and result/configuration/task-data hashes. Raw field values,
documents and snapshots are never stored in the Manifest.

The Manifest freezes `field-lf-ascii-trim/v1`,
`field-value-differences/v1`, `python-string-equality/v1` and
`field-raw-difference-queue/v1`. A comparable pair with any raw unequal field
makes a task a disagreement, including format-only differences. Both-empty
fields stay outside the denominator. D=0 stays unavailable, not 100%.

`FieldBatch` has a `(project, idempotency_key)` uniqueness constraint and an
immutable-row PostgreSQL/SQLite trigger, including its primary key. Only nullable
creator-FK cleanup is permitted; frozen creator identity remains unchanged.
Reads validate the complete V1 shape, pin graph, selection reasons, counts,
costs and canonical hash. A bad Manifest rejects the whole response.

Later assignments, completions and revisions cannot upgrade an existing batch.
Later valid review status changes, superseding or assignment cancellation alone
do not replace pinned answers. Missing, rebound or corrupt original sources mask
the whole affected task: values, evidence and calculation hash are hidden;
observed comparable pairs are null. Frozen participant/pair counts and original
outcome remain explicitly historical. Exact restored originals reproduce the
same calculation and evidence. They are never replaced with current answers.

## API and authority

Base: `/api/projects/{P}/field-disagreements/batches/`.

| Method and suffix | Contract |
| --- | --- |
| GET `capability/` | Current viewer, read/create authority, versions and limits |
| POST `preview/` | `{name, scope}`; signed bounded 15-minute preview, no write |
| GET collection | Visible metadata only, no source hydration |
| POST collection | `{preview_token, expected_plan_hash}` and UUID `Idempotency-Key` |
| GET `{B}/` | Current visible display counts plus frozen metadata |
| GET `{B}/items/` | Whole-visible-batch verification before filtering/pagination |
| GET `{B}/items/{T}/` | Frozen matrix, selected pair and item evidence; optional `left/right` |

Default off: `ANNOTATION_FIELD_BATCHES_ENABLED=false`. The existing field flag
must also be enabled and `ANNOTATION_FIELD_DISAGREEMENT_MODES` must map the exact
organization/project to `fork_review`. Missing, unknown and Core modes deny.
No other analysis flag enables these routes.

All reads require a server-resolved authenticated actor, current organization
and project manager/reviewer membership and `READ_WORK`. Create and preview
add current manager role and the independent `ORGANIZE_FIELD` operation.
Read authority does not imply create, annotation-edit, review or publish authority.
Annotators cannot call the API. Caller-supplied actor/reviewer fields reject.

A reviewer's own frozen assignee/submitter cohort excludes the entire task
before collection counts, SQL pagination or snapshot loading. All-hidden batches
are absent from the list and return 404 on direct access. Reviewer responses
contain a visible projection hash and visible costs/counts, never the full
Manifest hash or hidden task count. Direct project/batch/task and selected-pair
scope checks remain authoritative even if a browser keeps stale controls.

Every verified read materializes the visible batch twice, with authority checks
before and after rendering. Identity, role, policy, mode, source, Manifest,
result or rendered-JSON changes discard the response. Responses are
`private, no-store` with `no-referrer`. These checks provide a consistent bounded
read; they do not claim retained runtime acceptance or lock every subsequent
read-side mutation.

## Creation, concurrency and original-key recovery

Creation holds the existing Project → identity/member/User → Task → Assignment
→ Submission lock order through actual rendering and final verification. New
creation reselects inputs under the fence and checks the signed preview's plan
hash. Changes reject with no new row or consumed key. Failed final source,
authority, Manifest or response checks roll the transaction back.

Before preview expiry/signature validation or any source/engine/budget work,
the locked request checks an existing project/UUID. Identical canonical body,
principal, identity source and frozen actor recover the original metadata,
plan hash and key with `200, replayed=true`; a new commit returns 201. A different
body or identity returns 409. Recovery still requires current create authority
and a valid V1 Manifest, but not available source rows, current engine support
or the original signing key. Frozen envelope validation uses V1 limits and
versions, independently of later processing ceilings. Recovery performs zero
source materializations, normalizations, equalities or scorer calls.

The browser preserves an uncertain request's exact UUID/body in the open
workspace. An invalid acknowledgement stays unknown until original-key recovery;
refresh never issues a new POST. Fresh capability must match the intent viewer
before create/recovery, and the acknowledgement must match that same viewer as
well as the exact UUID/plan. This is not durable offline command storage.

## Budgets and truthful cost

| Boundary | Inclusive maximum |
| --- | ---: |
| Tasks / participants per task / total cohort entries | 100 / 20 / 2,000 |
| Configured fields / raw value | 20 / 4 KiB UTF-8 |
| Raw values per revision / inline document / XML | 64 KiB each |
| SQL stored source JSON / canonical snapshots per pass | 64 MiB / 8 MiB |
| Logical Manifest / stored Manifest guard | 1 MiB / 4 MiB |
| Final response / uncompressed preview token | 1 MiB / 16 KiB |
| Normalizations / equality operations per pass | 400 / 7,600 |
| Materialization passes / external scorer calls | 2 / 0 |

The shared snapshot loader checks SQL byte totals both at preflight and in the
hydrating SELECT's aggregate CASE gate, before JSON decoding. Every task is
inspected without normalization/comparison; aggregate count/input/CPU budgets
must pass before the first scoring operation. Then each task normalizes I×F
values and performs 2×C(I,2)×F equality operations. Counters record actual loops
and must match the plan. Selected-pair rendering reuses those results.

A 20-person, 20-field task costs 400/7,600 per pass (800/15,200 per request).
100 two-person, one-field tasks cost 200/200 per pass. A reachable normalization
overflow is 20×19 + 3×7 = 401; equality counts are always even. No task/field is
dropped and no automatic splitting or hidden retries lower the apparent cost.
Unavailable malformed cohorts have no scoring work; valid all-empty cohorts
still incur their actual normalization/equality work and report unavailable.
Detail/queue/item filtering and pagination do not reduce whole-visible-batch
validation. List/capability/recovery have zero calculation cost.

**Confirmed final-output clarification:** final Manifest and actual rendered
response bytes depend on calculation. Their exact byte checks therefore happen
after calculation/serialization. An over-limit response returns no data and a
new creation rolls back, but performed work is not claimed as zero. Input/count/
CPU overflows still reject before normalization or equality. No conservative
output estimate rejects an otherwise valid boundary request.

The list decodes and validates at most eight guarded Manifests, using a bounded
iterator that is explicitly closed on both success and rejection. Per-pass
stored/canonical page bounds are therefore 32 MiB / 8 MiB; no source snapshots
are loaded by the collection route.

## Client contract and acceptance

The existing complete batch workspace is reused through an explicit field-only
transport/schema/renderer adapter. It does not fall back to classification,
NER, quality overview, arbitration or manual signed receipts. Item evidence is
`field-batch-item-evidence/v1`, bound to project/batch/task/viewer, frozen
reference, cohort, calculation time and result. The browser recomputes its
canonical Noble SHA-256 digest and checks pins, dimensions, pair arithmetic,
explanations and actual costs. It also rejects a later rehashed revision change
inside the same retained batch. Evidence cohorts must retain their unique,
ascending assignment order, and the frozen input/pair counts must match the
displayed comparison. A valid digest alone cannot make contradictory payloads
acceptable. Server authorization remains authoritative.

Manifest task identifiers are strict positive integers at both the outer scope
and each task entry; Python boolean/numeric equality is not a scope check.
The shared frozen-source validator also requires integer snapshot project/task
identities before accepting their numeric equality with current ORM scope.
Rehashed snapshots carrying booleans or floating-point IDs are invalid sources.

Project/viewer/batch changes, blur, revocation and late replies clear sensitive
display. Retained-evidence state updates only after the active request generation
passes. Pair values render as React text; the immutable calculation hash is
independent of the selected pair.

Acceptance covers formula/empty/missing vectors; deterministic revision choice;
frozen replay and restoration; PostgreSQL permissions and independent-connection
create/revoke/revision/render/SQL-growth races; exact key recovery; reachable
budget edges, actual loop counts and output rollback; existing classification,
NER, arbitration, review and sampling regressions; bilingual browser journeys.
The `Frozen Field Batches` workflow provides an ephemeral synthetic PostgreSQL
and browser environment. A workflow definition is not an executed acceptance.

Retained Docker/#76 remains read-only and is not used by these tests. Keep
historical old/new CHANGED, database-container-replacement CHANGED, cause UNKNOWN,
missing protected-worktree history and `NOT_RUN_AFTER_DOCKER_INTERRUPTION` /
unfinished full-database comparison records. Source, synthetic or CI success
does not resolve them. Existing broad Biome baseline findings remain separate.
