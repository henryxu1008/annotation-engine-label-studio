# Frozen text classification partial matching (#81)

Default-off, read-only supplement to exact-set reports. Do not change
classification-exact-sets/v1, stored manifests/calculation hashes, queue outcomes,
arbitration identity/eligibility, conflict-priority sampling, Submission,
ReviewDecision, release or Host data. No table/migration. #81/#84 remain open.

## Formula and input boundary

Schema classification-partial-match/v1 uses rule
classification-jaccard-nonempty/v1. For each control and unordered pair of
independent submitters, J(A,B) = |A intersection B| / |A union B|. Average pairs
equally per control, then controls equally. No weighting, threshold, accuracy or
statistical reliability claim. For {A,B}, {B,C}, {B}, scores 1/3, 1/2, 1/2 give
mean 4/9; exact agreement stays 0. Single-choice scores are naturally 0/1.

Reuse the existing exact-source validator/normalized sets. Either empty set,
including two empty sets, is unavailable, never 100%. Missing controls/values,
null, cancelled submissions, duplicate submitters, invalid labels, unsupported
configurations and frozen task/configuration mismatches retain old boundaries.
Never drop bad inputs/controls to reduce the denominator. No case folding,
whitespace/alias normalization or special treatment of a configured None label.

Return shared/left-only/right-only labels, source IDs and integer intersection/
union counts. Differences do not identify a correct answer. Source pairs follow
Submission ID order and labels the existing Unicode order. Standard-library
Fraction produces reduced numerator/denominator decimal strings; no floating
point values enter supplement hashes. Only displayed percentages round to two
places. Reuse Django signing, defusedxml and current validators; no metrics
framework or third-party dependency is added. The upstream Annotation distribution
API reads mutable Annotation results, so it cannot replace frozen evidence.

## API and frozen replay

Under /api/projects/{P}/classification-disagreements/:

| GET | First read | Replay |
| --- | --- | --- |
| tasks/{T}/partial-match/ | source_frozen=existing manual receipt | frozen=supplement receipt |
| batches/{B}/items/{T}/partial-match/ | no query | frozen=supplement receipt |

Reject unknown/duplicate parameters, actor/answer/threshold fields and mixed
receipt types. No write API. Old calculation results are unchanged; source/item
metadata adds can_explain_partial. The joint response supplies freshly verified
exact comparison and supplement together. A viewer HMAC binds current trusted
principal/source, actor, organization/project and role, without granting access.

An independently salted signed receipt freezes schema/rule, parent origin,
exact source pins, first explanation time and supplement result hash. observed_at
is a fresh check time. Replay retains the receipt, source selection and first
time; new revisions never replace inputs. Source loss masks scores/answers;
changed pins or inconsistent comparable replay rejects. Never fetch URLs or
silently switch rule versions. Reviewer receipts contain only the visible task,
not full batch manifests/hashes, hidden tasks or totals.

Old batches gain a supplement only on explicit read, never by backfill. Keep old
manifest, metric time, calculation hash and original-key recovery unchanged.
Supplement time does not claim prior existence. This is transient signed evidence,
not permanent audit/metric retention or a delivery Manifest; key rotation can
invalidate it. Future incompatible rules need a new version. Never extend the
old service.calculate() report, whose full shape is already hashed by consumers.

## Authority, size and response integrity

Require ANNOTATION_CLASSIFICATION_PARTIAL_MATCH_ENABLED=false by default, existing
classification flags and exact org/project fork_review mode; batches additionally
require their existing flag and supported backend. Unknown/Core-only modes deny.
Current trusted Manager/Reviewer and READ_WORK are required for every new read and
replay. Manual self-source exclusion remains; batches exclude the whole task when
the viewer belongs to its cohort. Do not recompute a smaller visible cohort.
Signatures/IDs are not authority and imply no review/arbitration/release operation.

Reuse targeted project_item_sources: verify complete frozen cohort, source graph,
snapshot/config/task-data hashes and old calculation hash without loading other
tasks' answers. Repeat principal/actor/role, mode, policy and source checks after
materialization and actual JSON rendering. Drift discards answers. These reads
create/change/lock no rows. Responses use private/no-store and no-referrer.

Inclusive bounds: 20 participants, 20 controls, 100 labels/control, existing 64 KiB
XML and 8 MiB canonical snapshot bounds. New business data (origin/base/partial/
viewer) is capped at 1 MiB canonical UTF-8 JSON, excluding the fixed signature/
timestamp envelope; the uncompressed supplement token is capped at 16 KiB. Check
per-pair output during construction and stream final byte counting. Reject excess
with 422, without truncation, sampling, jobs or changing old endpoints. Invalid
queries/signatures: 400; scope denial: existing 403/404; changed evidence: 409;
valid but incomparable sources: unavailable with null score and no answer locations.

## UI and acceptance

Reuse manual comparison and batch detail/queue navigation and ClassificationResult.
ClassificationPartial adapts the two GET transports and presents formula, version,
first time and pair differences separately from exact agreement. Return retains
page/filter/focus. Scope changes, blur and failures clear evidence; generations
discard late responses. Explicit frozen refresh is GET, never a creation/recovery
POST. No separate workflow, decision or approval authority is introduced.

The batch's arbitration entry is rendered inside the same verified joint source
projection as the answers. A pending/failed supplement read removes that entry;
an unavailable result cannot retain a dispatch action from the older parent
projection. A fresh comparable read can restore the existing independently
authorized entry. Server-side arbitration eligibility and write semantics remain
unchanged.

Regression requirements: exact rational means, single/multiple/multi-control
cases, empty/missing/unsupported cases, frozen replay/source tampering/loss, old
batch/calculation/arbitration identity, modes/permission withdrawal, UTF-8 bounds,
and render-time revocation. Independent PostgreSQL connections cover revocation,
source deletion/corruption and new revisions after materialization. Dedicated
synthetic PostgreSQL CI also runs old classification/review/priority/arbitration/
handoff tests, frontend regression, and real en-US/zh-CN browser journeys including
existing batches, budget rejection, scoped access and source loss.

## Retained environment evidence

Local Docker/#76 remain read-only. Historical CHANGED,
CHANGED_DATABASE_CONTAINER_REPLACED, cause UNKNOWN and full database comparison
NOT_RUN_AFTER_DOCKER_INTERRUPTION remain. This implementation baseline could
read all six recorded containers, but the prior #76 worktree path was absent:
worktree comparison is unavailable, not PASS. No restore/cleanup, Docker lifecycle
operation, deployment or Core integration follows from implementation/test success.
