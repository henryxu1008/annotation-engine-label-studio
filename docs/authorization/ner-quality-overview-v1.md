# Single frozen NER batch quality overview (#81)

The independent, default-off `ANNOTATION_NER_QUALITY_OVERVIEW_ENABLED` read path
adds `GET /api/projects/{P}/ner-disagreements/batches/{B}/quality-overview/`.
It also requires the existing NER/batch flags, exact org/project `fork_review`
mode, supported DB and current Manager/Reviewer plus READ_WORK. No ORGANIZE_NER,
review, arbitration or release permission is inferred or required for this GET.
The [confirmed design](ner-quality-overview-v1-design.md) defines the full scope.

## Metric and denominators

Use `ner-quality-overview/v1`, `ner-batch-quality-task-macro/v1` and the existing
strict/explanation/engine/selection versions. Strict pair F1 remains
`2 * exact-matching-entities / (left-entities + right-entities)`. Both-empty is
undefined (`no_entities`), one-empty is zero, and missing/unusable inputs make
the whole task incomparable under the original NER rules.

Average defined unordered pairs within each task, then average scorable tasks
with equal weights. Each annotator averages its own defined peer pairs at a
task, then its scorable tasks. Never average annotator rows into a project score
or pool all entities/pairs across tasks. Ratios use exact reduced integer strings;
display rounding never changes an old agreement/disagreement outcome.

Return frozen task and pair denominators, scorable counts, undefined counts and
their distinct reason counts, coverage and nullable strict F1. The complete
visible batch is the scope, irrespective of queue filters or personnel pagination.
There is no gold answer, accuracy score, ranking or cross-batch comparison.

## Sources, visibility and replay

Read only the original manifest cohort/revision pins via the existing NER batch
loader; retain missing inputs. Reviewer self-cohorts are excluded as whole tasks
before source SQL, counts, reasons, rows and pagination. Reviewer references only
its projection hash, never the full manifest hash or hidden totals/identities.
Annotators, cross-project/org, revoked identity/operations and Core/unknown modes
deny even with a valid signature or stale browser state.

Any unavailable visible pinned source masks the entire overview's summary,
annotators and pagination, keeping the original frozen scope counts. Preserve
NER's source_unavailable contract for missing/rebound/corrupt sources; manifest
corruption and calculation mismatch still reject. Do not shrink the denominator
or select a later revision. New submissions, review or arbitration do not replace
intact historical evidence.

An independently salted, uncompressed signing receipt binds rules, viewer,
org/project/batch, visible source-set/reference hashes, first summarized_at and
the complete logical result hash. It adds no persistent ledger or DB migration.
Replay revalidates current authority and all original sources. Pagination keeps
the same receipt, first time and full result hash. Source failure may retain an
old receipt but masks scores and returns receipt_verified=false/result_hash=null;
only intact original restoration verifies the old result again. Existing v1
batches need no backfill and their manifests, hashes and original keys stay intact.

## Budget and atomic read

Only frozen/page/page_size are accepted, once each. Initial page must be 1;
later pages require frozen. Page size defaults to 50, maximum 100; actor, weights,
thresholds, revision selection and queue state are rejected. No write methods.

Apply existing whole-visible-cohort limits: 100 tasks, 20 participants/task,
2,000 entries, SQL stored snapshots 64MiB and canonical snapshots 8MiB, original
XML/text/label/entity constraints, sum W <= 5,000,000 per pass and <=380 library
calls across both passes. Scorable pair count is checked before source loading.
Successful complete-source reads call nervaluate exactly 2Q times, not once per
person or page. Both-empty/incomparable pairs make zero calls; failures report
their actual partial cost. No HTTP fan-out, hidden loads or per-person N+1.

Logical output includes all rows before slicing (1MiB), actual response including
receipt is independently bounded (1MiB), and receipt is at most 16KiB. The existing
32KiB Nginx request-line support remains. Oversize logical/actual output and
receipts reject before the first scorer call; revalidation checks its budgets
again. Do not truncate, sample, split or silently approximate fractions.

Reuse NER's two-pass read and actual JSON render/representation guard. Materialize
and bound all evidence before scoring; after rendering recheck identity, scope,
flags/mode, READ_WORK and counter agreement on the already bounded pinned input.
Replay all sources and compare the complete result **after the last counter**,
then recheck authority. This also applies to manual NER comparison and frozen
batch GETs; scorer runtime must not hide a committed source change. Two source
materializations and exactly 2Q counter calls remain; a concurrent failure can
consume both bounded counter passes before final replay rejects it. Changes
discard the response. Use private/no-store and no-referrer. GET writes no business
data. Existing create/original-key recovery still precedes source/processing-budget
reevaluation and is independent of overview reads.

## Page flow and verification boundary

The existing frozen-batch workspace now has an explicit optional renderOverview
adapter. NER uses its own API/schema/renderer; an adapter without that slot cannot
fall back to ClassificationOverview. Reuse the batch list/detail/queue/frozen-pair
flow, layout, locale and controls; retain pagination/filter/focus/receipt on return.
Blur, scope/viewer change and failure clear evidence and fence late responses.

Tests cover exact task/peer weights, mixed double-empty pairs, missing versus
empty, frozen replay/restoration, permission/source failures, original-key
recovery, actual 380-call limits, budgets and write-free reads. Independent
PostgreSQL connections interleave revocation, operation withdrawal, source
deletion/corruption and assignment rebinding after actual rendering and during
the last counter, including a legitimate new revision, budget growth and exact
call cost across the three NER read paths. Task and pair reason counts have
distinct localized unit labels. Bilingual
browser journeys use fresh synthetic databases, real Fork/Nginx, 52-person
pagination, immutable replay, real source-growth budget denial, restoration,
self exclusion, revocation and late responses. Test definitions are not results;
record each actual validation separately.

No existing metric/review/arbitration/sampling/release policy is changed. #81/#84
stay open. No Host change, merge, deployment or Core integration. All PostgreSQL
and browser services run on synthetic ephemeral CI runners, never retained local
Docker/#76. Preserve old/new CHANGED, database-container replacement, cause UNKNOWN
and full database comparison NOT_RUN_AFTER_DOCKER_INTERRUPTION. Narrow fingerprint
checks do not prove database equality.
