# Single frozen text-field batch quality overview (#81)

This independent default-off GET consumes one existing FieldBatch. It adds no
batch, database table/migration, annotation, Submission, review decision, arbitration,
sampling rule, release authority or Host/Core integration. #81/#84 remain open.
The confirmed first-slice scope is one frozen batch and a read-only derived view.

## Metrics and denominators

Schema field-quality-overview/v1; aggregation field-batch-quality-task-macro/v1.
Keep field-lf-ascii-trim/v1, field-value-differences/v1,
python-string-equality/v1, active-assignment-latest/v1 and
field-raw-difference-queue/v1 unchanged.

Within each frozen pair, D counts configured fields with at least one nonempty
normalized value; raw/normalized matches share D. D=0 is undefined/no_values.
Average defined pairs at each task, then scorable tasks equally. Each annotator
averages its own defined peers at a task, then its scorable tasks. Do not pool
all pairs/fields or average annotator rows into the project score.
Fractions are exact reduced decimal integer strings, never rounded percentages.
Missing/null/duplicate/multivalued fields or non-equivalent configurations keep
the entire original cohort incomparable. Explicit empty strings differ from
missing fields. Both-empty comparisons still incur actual field calculation work.
Normalized 100% may coexist with raw format-only disagreement; old queue outcomes
and ordering retain their original semantics.

Scope and undefined-reason counts distinguish tasks from pairs. Coverage includes
all visible frozen tasks; scores include only defined tasks/peer pairs. Zero
scorable tasks yields null scores, not 100%. The result describes this batch,
not full-project accuracy, gold quality, ranking or cross-batch trends.

## API, authority and replay

GET /api/projects/{P}/field-disagreements/batches/{B}/quality-overview/

Only frozen/page/page_size once each; page is initially 1, later pages require
frozen, page_size defaults to 50 with a maximum of 100. No actor, weighting, threshold, revision
selection or queue-state parameters and no write methods.

ANNOTATION_FIELD_QUALITY_OVERVIEW_ENABLED defaults to false and also requires existing
field/batch flags, supported DB and exact org/project fork_review mode. Current
Manager/Reviewer and READ_WORK are required, independently of ORGANIZE_FIELD,
REVIEW, arbitration and release. Annotators, cross-scope IDs, revoked identity,
Core-only-review and unknown modes deny access. Reviewer self-cohorts are excluded
as whole tasks before source loading/counts/reasons/personnel/pagination.
All-hidden batches remain 404. No new arbitrary user/name query is introduced.

An independently salted, uncompressed Django signing receipt binds rules,
org/project/batch, current server-derived viewer, complete visible source-set
and reference hashes, first summarized_at and complete logical result_hash.
Reviewer references expose only their visible projection. No raw values or
documents are returned or signed. All rows are hashed before personnel slicing;
a browser cannot reconstruct unreturned rows from one page. It validates the
page's types, fractions, costs, scope/viewer/receipt and generation. Server
authorization/signature/actual representation checks remain authoritative.

Replay retains the original receipt/result/first time across pages and refresh.
New submissions/assignments, normal review or arbitration do not replace pinned
inputs. Existing v1 batches and original-key creation recovery remain unchanged.
Current visible source loss/rebind/corruption masks the whole new summary,
personnel, pagination and result_hash without shrinking frozen denominators.
A prior token can remain as history with receipt_verified=false. Exact original
restoration is required to replay it. Manifest corruption, strict-ID violations
or calculation mismatch still reject. Complete-source/no-score results may have
a valid receipt and null scores; availability and source_status are distinct.
Retired signing keys are not a permanent aggregation storage contract.

## Two passes, output limits and truthful work

The shared field reader now exposes inspection and pure scored projection as
small internal steps; its original public DTO/hash/behavior remains unchanged.
Overview first hydrates/inspects bounded pinned inputs and calculates/reduces.
After actual JSON rendering it recalculates the same bounded inputs, then
rehydrates and validates current source/Manifest/viewer evidence AFTER the last
normalization/equality. Authority is checked throughout and again at completion.
A final viewer check also binds the organization after source hydration: retaining
the same principal, actor and manager role while moving the project/active
organization cannot release a response sealed for the old organization. Supported
signing-key fallbacks still accept the original unchanged viewer on replay.
A changed representation, source or permission discards the response.
There are exactly two hydration/calculation passes, no third scoring pass or
per-annotator/HTTP fan-out. Responses are private, no-store and no-referrer.

Whole-visible-batch limits remain 100 tasks, 20 participants/task, 2,000 cohort entries,
20 fields, 4 KiB/raw value, 64 KiB/revision raw values/text/XML,
64 MiB SQL JSON and 8 MiB canonical snapshots per hydration.
The SQL preflight plus hydrating SELECT aggregate CASE gate precede JSON decode.
Aggregate input/CPU rejects before first calculation: 400 normalizations and 7,600
string equalities/pass; success totals 800/15,200 with zero third-party scorers.
Actual aggregation_pair_visits <= 3,800 and annotator_pair_updates <= 7,600/pass;
undefined pairs count as visits but do not receive fake scores/peer updates.

Logical output containing ALL personnel rows and actual rendered response
including token each have a 1 MiB exact byte limit; the uncompressed token is 16 KiB.
These calculation-dependent output checks occur after calculation; failures
return no scores and do not claim zero performed work. A second-hydration
failure may occur after both counter passes. GET has no business row to commit.
Scope/personnel pagination cannot reduce work or bypass limits.

## Workflow and validation

Reuse the existing frozen field batch list/detail/queue/pair/return flow through
its explicit renderOverview adapter. No classification/NER formula or fallback
is borrowed. Retain receipt/personnel page/queue filter/focus on return; blur,
viewer/project/batch change or failure clears evidence and fences late responses.
Refresh is a same-receipt GET; explicitly regenerate to create a new derived time.
Old uncertain creation UUID/body and recovery remain independent.

Tests cover mixed two/three-person task/peer weighting, empty/missing boundaries,
frozen source/revision restoration, role/self/revocation, exact input/output/work
limits, rendered DTO tampering, metadata-only old-key recovery and no business
writes. Independent PostgreSQL writers interleave revocation, actor remapping,
deletion/corruption/rebind/growth after render, during the final equality and
between SQL preflight/hydration.

The existing Field Batches workflow also seeds an independent synthetic overview
organization/domain/schema. The new en-US/zh-CN journey checks 56-person pagination,
frozen replay/return, exact 65/72 raw and 35/36 normalized task averages, source
loss/restoration, 65 MiB stored-source overflow 422, Reviewer self exclusion/revocation,
Annotator denial and delayed replies. Test definitions are not executed evidence:
actual results and any local startup policy block must be reported separately.
No new long journey is appended to the existing Enterprise job.

No Host, deployment, real Core or retained Docker/#76 operation is authorized.
Keep all prior/new CHANGED, database-container replacement, cause UNKNOWN,
protected-worktree-missing history and NOT_RUN_AFTER_DOCKER_INTERRUPTION /
unfinished full-database comparison records. Existing shared Biome baseline
findings stay separate; source/unit/CI success never proves retained DB equality.
