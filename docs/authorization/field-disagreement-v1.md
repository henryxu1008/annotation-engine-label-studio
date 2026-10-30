# Frozen plain-text field comparison V1 (#81)

This is a single-task, GET-only Fork `fork_review` projection. #81 and #84
remain open. It adds no persistent batch, arbitration, review decision, sampling,
release/export policy, Host integration, or Core-only-review authority.

## Supported frozen inputs

- Explicit exact Submission IDs from 2–20 independent submitters on one task.
  Choosing a revision never means choosing the current/latest annotation.
- Identical frozen label configuration and complete task-data hash. One inline,
  unencoded plain `Text` with a simple `$key` binding; no paths/interpolation.
- 1–20 static whole-document `TextArea` controls, each explicitly
  `maxSubmissions="1"`. The editor's omitted default permits multiple entries,
  so omission is unsupported. Names must be distinct and target the same Text.
- Every configured field must have exactly one string in `value.text`. Missing,
  null, multi-valued, duplicate, regional, dynamic, media/OCR/PDF, mixed-control,
  cancelled or malformed inputs make the entire cohort incomparable. We never
  drop an input or field to manufacture a smaller denominator. Explicit `['']`
  is an empty value; the editor omitting an unfilled field is missing, not empty.

## Frozen metric and explanation

`field-disagreements/v1`, rule `field-lf-ascii-trim/v1`, explanation
`field-value-differences/v1`, engine `python-string-equality/v1`.

Normalize CRLF/CR to LF, then trim outer ASCII whitespace (space, tab, LF, VT,
FF). Do not change internal whitespace, case, Unicode normalization, numeric
values, leading zeroes, dates or business meaning. The original string is retained.

For each unordered pair, let D be the number of configured fields for which at
least one normalized value is nonempty. R counts raw-equal fields in D and N
counts normalized-equal fields in D. Return exact reduced fractions R/D and N/D,
plus the integer counts. Both use the same denominator. D=0 means unavailable,
never 100%. Both-empty fields retain `both_empty` explanations and are excluded
from both rates, even if the two raw strings differ.

Each field is `equal`, `format_only`, `value_difference` or `both_empty`.
Three or more people produce a deterministic pair matrix, not a ranking,
accuracy estimate or aggregate average. Only the selected pair exposes bounded
raw and normalized values. The complete logical result hash is independent of
which pair is displayed. Example: `" 张三 " / "张三"`, CRLF/LF city values, and
`"001" / "1"` yield raw 0/3 and normalized 2/3, with the code still different.

## API and replay

Default off: `ANNOTATION_FIELD_DISAGREEMENTS_ENABLED=false`. Independently map
`ANNOTATION_FIELD_DISAGREEMENT_MODES={"org:project":"fork_review"}`. Any missing,
unknown or Core mode denies access. Classification/NER flags do not enable it.

- `GET /api/projects/{P}/field-disagreements/capability/`
- `GET /api/projects/{P}/field-disagreements/tasks/{T}/sources/` — metadata only,
  no snapshot or live task JSON hydration. No automatic revision choice.
- `GET /api/projects/{P}/field-disagreements/tasks/{T}/?submissions=id,id,...`
- Replay with `?frozen=receipt` and optionally `left=id&right=id` from that same
  frozen cohort. Unknown, duplicated, actor/reviewer, weighting and threshold
  parameters reject. Write methods are unsupported.

The uncompressed signed receipt uses a separate salt, binds project/task,
assignment/assignee/submitter/revision, source/config/task-data hashes,
configured-field scope, rule/engine versions, first calculation time, complete
result hash and current server-derived viewer. No raw values are signed into the
receipt. It is a transient signed replay reference, not a durable batch Manifest.
NER/classification receipts cannot be used here; no older batch is backfilled.

Later revisions do not replace pinned inputs. Missing original sources hide all
scores and values, retaining cohort/pair/configured-field scope and original
receipt. Exact restored originals replay with the same result/time/hash. A
corrupt or rebound surviving source fails closed rather than returning stale
answers. Retired signing keys, changed viewer, rule or inputs reject replay.

## Authorization and response races

Reuse authenticated principal resolution, current organization/project membership,
manager/reviewer scope and READ_WORK enforcement. Annotators cannot read these
APIs. Reviewers' own submissions are excluded before collection counts or payload
loading; explicitly selecting a forbidden source rejects the request. Collection
and object scope are checked independently. Frozen receipts grant no authority.

After rendering, repeat authority/source hydration and full result validation;
discard the whole rendered response if membership, identity, source or actual
JSON representation changed. Headers are `private, no-store` and `no-referrer`.
UI scope/viewer/generation checks discard late or malformed reads after blur,
project/task changes and revocation. Values are React text, never HTML.

The client recomputes the manifest digest using sorted-key JSON through all
input-array elements, rather than trusting a repeated digest field. Initial
comparison also binds each manifest pin to the selected source metadata
(assignment, assignee/submitter, task, revision and snapshot hash). Status is not
an input pin: a later review decision alone does not change a frozen answer.
Digest or selected-source mismatches clear the projection; a retry keeps the
original receipt and never silently selects another revision. This verifies
receipt/body consistency and does not replace server-side signature or authority
checks. Existing unmodified V1 receipts continue to replay.

## Whole-request budgets and cost

No truncation, field/input dropping, splitting or hidden fallback:

| Boundary | Inclusive maximum |
| --- | ---: |
| Source collection metadata | 100 revisions |
| Exact inputs / configured fields | 20 / 20 |
| Each raw field / raw values per revision | 4 KiB / 64 KiB UTF-8 |
| Inline source document / XML | 64 KiB / 64 KiB |
| SQL stored JSON per hydration / canonical snapshots per pass | 64 MiB / 8 MiB |
| Actual rendered response / uncompressed receipt | 1 MiB / 16 KiB |

The shared snapshot loader preserves the existing NER wrapper/contract. Both SQL
preflight and the hydrating SELECT's aggregate CASE gate must pass before JSON
decode, including growth committed between the two queries. No live task payload,
media/network requests or per-pair database queries are used.

Each successful available report has two materialization passes. Per pass,
normalize I×F values once and perform 2×C(I,2)×F string equality operations.
Selected-pair rendering reuses stored equality flags and does not recalculate.
Maximum request cost is 800 normalizations and 15,200 equalities, with zero
third-party scorer calls. `cost_per_pass` reports the actual bounded input work.
Unavailable malformed cohorts do not claim a successful scoring cost.
Client ASCII trimming uses the existing es-toolkit implementation with explicit
ASCII characters, avoiding repeated regex scans of internal whitespace while
retaining the same versioned normalization rule. Client hashing reuses the
existing Noble SHA-256 dependency and works without secure-context WebCrypto.

## Acceptance and remaining scope

Offline formula/empty/missing/normalization/budget tests; PostgreSQL negative
authorization, independent-connection revocation/hash/delete/rebind/growth tests;
exact frozen replay/new revision/source restoration and no business-write tests;
UI malformed evidence/denominator/cost/viewer and late-response tests; bilingual
browser journeys through manager comparison, reviewer exclusion/revocation and
annotator denial. CI retains full production build and browser artifacts.

Future independent design is required for image box identity/IoU, richer document
formats, persistent field batches/overviews/arbitration and low-confidence review.
Second-review scope still needs explicit agreement: ReviewDecision is one-to-one
with a Submission and cannot be overwritten to impersonate a second review.
Do not close #81/#84 from this slice or claim retained runtime/deployment acceptance.

Docker/#76 stay read-only and are not required to run these tests. Historical
OLD_AND_NEW_CHANGED, CHANGED_DATABASE_CONTAINER_REPLACED, cause UNKNOWN and
NOT_RUN_AFTER_DOCKER_INTERRUPTION / unfinished full-database comparison remain
independent of all new source, CI and isolated synthetic evidence.
