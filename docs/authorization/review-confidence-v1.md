# Revision confidence sampling V1

Implementation contract for #84. The approved historical design is
`review-confidence-v1-design.md`; its frozen requirements remain in
`review-confidence-v1-acceptance.md`. Actual execution evidence is recorded separately.

V1 supports frozen **inline static Text + static Choices** submissions. It reuses
ReviewBatch candidates, quotas, ReviewerWorkspace, rework comparison and the existing
one-decision-per-revision workflow. This fork does not infer confidence from Prediction,
agreement or imported scores. No model or external scorer is invoked. Confidence is a
registered producer's revision-specific assertion, not calibrated probability or approval.
Second review, model execution/quality, host integration, activation and deployment are separate stages.

## Source index

- `label_studio/review_batches/confidence_contract.py`: strict operator registry/grants,
  instance, profile digest, complete frozen input and nine pins, scope and limits.
- `label_studio/review_batches/confidence_api.py`: producer transport with pre-parser byte gate.
- `label_studio/review_batches/confidence_observations.py`: trusted producer input, append-only
  fact/receipt, exact event recovery and post-render verification.
- `label_studio/review_batches/confidence_priority.py`: typed source adapter, deterministic
  ranking, frozen evidence validation, redacted projections, batch creation/recovery.
- `label_studio/review_batches/migrations/0006_confidence_observation.py`: additive table,
  event/subject uniqueness and PostgreSQL/SQLite immutable update guards.
- `label_studio/review_batches/handoff_metadata.py`: shared bounded multi-field hydration.
- `label_studio/classification_disagreements/snapshot_loading.py`: shared SQL row/union gates.
- `label_studio/review_batches/service.py`, `priority.py`, `guarded.py`, `ner_priority.py`,
  `api.py`: existing workflow integration; old strategies keep their wire/hash/defaults.
- `label_studio/tasks/api.py`: existing batch-context review response uses the strategy's
  submission projection; ordinary review permissions and decisions retain their contract.
- `web/apps/labelstudio/src/pages/DataManager/confidenceReviewPriority.js`: typed public
  DTO, exact source/version/threshold, quota/selection/summary and ACK binding.
- `web/apps/labelstudio/src/pages/DataManager/ReviewPriority.jsx`, `ReviewBatches.jsx`,
  `reviewBatchResponses.js`: existing complete planning/review flow and explicit recovery.
- `label_studio/review_batches/tests/test_confidence.py`, `test_confidence_boundaries.py`,
  `test_confidence_budgets.py`, `test_confidence_acceptance.py`, `test_confidence_concurrency.py`:
  synthetic protocol, negative, byte/decoder and independent-connection regressions.
- `web/apps/labelstudio/src/pages/DataManager/ConfidenceReviewPriority.test.jsx`:
  bilingual flow, malformed DTOs, late reads, stable owner and original-key recovery.
- `label_studio/core/management/commands/seed_review_confidence_e2e.py`,
  `web/apps/labelstudio-e2e/cypress.review-confidence.config.ts`,
  `web/apps/labelstudio-e2e/src/e2e/review-confidence.cy.ts`: fresh synthetic browser fixture
  and complete bilingual acceptance. The fixture requires TEST_ENVIRONMENT and refuses reuse.

## Authority and producer protocol

Default settings disable the extension. An operator must configure a stable canonical
instance UUID, scoped content-addressed profile entries and trusted source grants in Django
settings. Registry and grant data are operator configuration, never human POST fields.
`ANNOTATION_REVIEW_CONFIDENCE_PROFILES` entries contain organization/project IDs, `descriptor`,
its canonical SHA256 `digest` and strict `fresh` boolean. There are at most 20 selectable
versions per scope. Descriptor identity includes producer namespace, model artifact SHA256,
evaluator version, score definition, calibration version and input/integer contracts.
The same profile label or model name does not imply an interchangeable version.

`ANNOTATION_REVIEW_CONFIDENCE_GRANTS` bind organization/project, authenticated principal ID,
trusted identity source, mapped local actor, producer namespace and profile UUIDs. The
actor must be an active current Manager with READ_WORK and independent
`PROVIDE_REVIEW_CONFIDENCE`. Legacy project policy does **not** auto-grant this operation.
Planning separately requires ORGANIZE_REVIEW. Reviewing still requires the current
Reviewer role and REVIEW; self assignee/submitter rows are excluded before source reads.

- `GET /api/projects/{project}/review-confidence-inputs/{submission}/` requires exact
  `profile_id` and `profile_digest` query parameters. It returns the complete canonical
  frozen input, nine subject pins, input hash and registered profile. It reads only the
  latest pending submission with no decision and assignee equal to submitter, and rejects
  a producer who is either person. Live Task/config never substitute for historical input.
- `POST /api/projects/{project}/review-confidence-observations/` accepts only
  `submission_id`, `profile_id`, `profile_digest`, `evaluated_input_hash`, `score_units`
  and UTC `evaluated_at`, with a canonical event UUID in Idempotency-Key. Score units must
  be integers 0..1,000,000; bool/string/float/NaN, extra identity/model fields and duplicate
  JSON keys are rejected. The raw body is bounded before JSON parsing, including chunked input.
- Event uniqueness is `(project, instance, trusted namespace, event UUID)`, independent of
  profile/digest. Subject uniqueness is `(project, instance, profile UUID, digest, frozen
  submission ID)`. A new event cannot correct the same version/revision. Correction requires
  an explicit new content digest and event. Facts do not contain a copy of answers.
- Original event recovery verifies current actor/grant/operation and bounded local proof;
  it needs no live snapshot or fresh descriptor. The original request/body and owner must
  match. Resource deletion may null convenience FKs, which can never be rebound.

Both input GET and fresh append build the actual input and verify it again after rendering.
Append takes the common Project-first write fence and rolls back if source, current
authority, descriptor/grant or rendered response changes. External configuration is checked
again; it is not claimed to be locked by a database row lock.

## Planning, freezing and recovery

The existing preview/create APIs accept one optional discriminated priority input:

```json
{"kind":"revision_confidence","profile_id":"canonical UUID","profile_digest":"64 lowercase hex","threshold_units":400000}
```

One batch uses exactly one profile/digest. Low means `score_units < threshold_units`.
Original global/per-submitter quotas remain `ceil(N * rate / 100)`, including unknown
candidates. Low candidates sort by ascending units, then seeded SHA256 and submission ID.
Known non-low and unknown share one seeded fallback bucket. Threshold zero selects no low
evidence; an equal-to-threshold score is non-low. Missing/unsupported/new-revision facts
are explicit unknown, not high confidence. Frozen-pin or fact corruption aborts the read.

Preview and fresh create each materialize and construct the plan twice, render, then
perform a third full bounded actual-source verification without sorting. Current authority
is checked afterward. Producer append and batch create acquire the same Project fence,
including when a fact is absent. If ingest commits first, an old expected plan is rejected;
if batch creation commits first, its unknown selection remains frozen.

Internal immutable proof includes the full fact metadata. Manager public previews redact
producer namespace, event, private owner and receipts. Their opaque plan_hash identifies
the internal plan; hashing the redacted public DTO does not reproduce it. Clients validate
the typed public input/selection/cost and bind ACK schema/kind/algorithm/project/actor,
owner hash, viewer hash, request hash, original key and plan hash. Reviewer projections
contain no raw score or source proof.

Frozen selection/hash/denominator never rerank after new facts, revisions, retirement or
review. Historical source health remains current and may become unavailable. Existing
ReviewDecision, ordinary review and release authority remain independent.

Creation transport failures retain UNKNOWN and the exact body/key. Explicit original-key
recovery needs current planning authority and valid local receipt, independently of the
fresh registry or source size. Capability exposes a separate recovery flag and stable
owner fingerprint. Salt-only viewer rotation clears old reads while retaining an unknown
same-owner command; actor/principal/provider/org changes clear it. These fingerprints are
comparison tokens, not authorization credentials. Confirmed creation and a later read
failure stay distinct; refresh does not repeat POST.

## Budgets and inherited projection fixes

| Resource | Stored SQL bytes | Canonical UTF-8 bytes |
| --- | ---: | ---: |
| One snapshot | 2 MiB | 256 KiB |
| Snapshot union per materialization | 64 MiB | 8 MiB |
| Each fact proof / receipt | 32 KiB | 8 KiB |
| All metadata per materialization | 16 MiB | 4 MiB |
| Frozen plan / confidence-containing page | 16 MiB | 4 MiB |
| Actual response, including wrapper | — | 4 MiB |
| Descriptor / raw producer body / capability | — | 4 KiB / 16 KiB / 128 KiB |

The existing smaller static text/config limits still apply. SQL preflight is followed by
row and cumulative window CASE gates **in the hydrating statement**. Oversized JSON cannot
reach the decoder after concurrent growth. Both metadata JSON columns count together.
Canonical cumulative limits apply after bounded decoding. Confidence-containing mixed
pages retain the old field/NER limits as well; pages without the new marker retain old budgets.
Partial/unknown new kind/schema/algorithm markers never enter ordinary fallback.

Fresh batch cost records three snapshot passes (at most 192 MiB stored / 24 MiB canonical),
three metadata passes (48/12 MiB), two sorting passes and zero scorer calls. Producer input
uses two single-input materializations. Recovery uses zero confidence materializations.
Ordinary authorized answer reads are separate and do not count as model/scorer work.

Independent review found two inherited projection gaps. Unbounded ordinary review reasons
could be loaded before the final response limit, and full UserSerializer could decode
other users' custom_hotkeys/current organization metadata. Confidence-only policies now
SQL-gate the total review reason bytes at 4 MiB and return the existing UserSimpleSerializer
with explicit scalar columns, including original rework source and successor pages. The
old reason write semantics and old strategy wire remain intact. This is a scoped fix,
not a claim that all legacy serializers are globally bounded.

The shared authorization path still loads the complete current Project, including
parsed configuration/control metadata. These inherited authorization-context fields
are outside the new frozen snapshot/plan/fact/receipt byte accounting. A general
minimal Project projection would require a separate compatibility review of operation
providers; this implementation does not claim that broader hardening is complete.

## Acceptance and stage boundaries

Use a fresh isolated PostgreSQL/SQLite database and synthetic producer. The test issuer
asserts scores directly; protocol acceptance does not prove a real evaluator, model quality,
calibration, production registry or host deployment. Remote PR/actual merge/main CI and
real activation are NOT_RUN until separately authorized. #81/#84 stay open; Docker/#76,
retained runtime/data and other worktrees are outside this implementation.
