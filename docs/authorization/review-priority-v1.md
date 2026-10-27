# Optional frozen classification conflict-priority sampling (#81 / #84)

This slice extends new Fork ReviewBatches only. It reuses the existing preview,
creation, selected-only review queue and rework comparison. No Submission,
ReviewDecision, arbitration, release policy or Host record is written by planning
or creation. #81 and #84 remain open. Low-confidence ranking, other modalities,
second review and review-to-arbitration handoff are separate work.

## Selection and compatibility

Without `priority`, requests, request hashes, `sha256-ranked-v1` plans and their
original-key recovery retain their original shape and behavior. Existing plans
are never upgraded or rewritten. No schema migration or new table is needed;
existing PostgreSQL/SQLite plan-update guards cover the added frozen evidence.

An optional strict `priority` object contains `kind=classification_conflict`, a
positive safe `batch_id`, and the exact 64-character lowercase `manifest_hash` of
an existing ClassificationBatch in the same project. Only the current exact-set
classification rule and frozen revision-selection contract are accepted.

`classification-conflict-ranked/v1` retains the original latest-pending-per-
assignment candidate pool and package/submitter filters. For each original group
(global or per submitter), select ceil(N * rate / 100). Sort verified frozen
conflicts first, then SHA-256 of [algorithm, project, seed, candidate identity],
then Submission ID. Remaining places use the same seeded ranking. Conflict count
may exceed the quota: show the uncovered count, never silently raise the rate.

A candidate is a conflict only when its Submission, assignment, task, revision,
submitter and result hash match a frozen input, the complete cohort verifies, and
the frozen comparison has fewer matching than total pairs. Exact comparable
agreement is a separate state. Not covered, revision mismatch and originally
unavailable metrics are explicit unknown states, never low-risk claims. The task
ID alone cannot transfer an old conflict onto a new revision. Current mutable
task data, precomputed agreement, model scores and structural conflict flags do
not supply evidence. No analysis is automatically created or refreshed.

The plan freezes the analysis ID/full manifest hash, rule/selection versions,
per-candidate state/reason/calculation hash, seed, entire candidate denominator
and selected flags. It copies no answer sets or full snapshots. Preview/create
reject changed manifests, source corruption/deletion and excessive inputs. New
revisions invalidate a prior preview if they change the candidate plan. Creating
a fresh preview may explicitly classify a new revision as unknown.

After creation these are historical selection facts, not a current quality score.
Later source loss, revision changes or decisions do not recalculate the sample,
replace its evidence or shrink the denominator. Reading comparison answers still
uses the existing classification APIs and their live source/access checks.
Ordinary reviews continue to require their own current authority and intact exact
Submission. Frozen prioritization grants none of that authority.

## API, permissions and transactions

Reuse `/api/projects/{project}/review-batches/` preview, collection, detail and
queue routes. Capability adds independent booleans `can_prioritize` and
`can_read_priority`. A denied current read cannot publish an older detail. New previews
return a versioned priority plan. New priority create/recovery acknowledgements
also return the original UUID `idempotency_key`; the client verifies it and the
expected plan hash before treating creation as confirmed.

Planning requires current Manager, READ_WORK, ORGANIZE_REVIEW and read access to
classification batches, with both review and classification feature/mode gates.
It does not require permission to create another analysis or to review/arbitrate.
Missing/unknown/Core-only modes deny this strategy. No activation of retained
environments is performed by code or migrations.

Creation uses the existing project-first write fence, then verifies the exact
analysis cohort with ordered Task/Assignment/Submission locks. Retain the fence
through JSON rendering, actual representation validation and final identity,
policy and source rechecks. Failure rolls the whole creation back. The same
principal and resolved actor must survive all checks, including mapped actors.

Same UUID/body/principal/actor recovers the original stored batch before requiring
the source analysis to remain available. Current authority and frozen local plan
integrity are still required. A different body or identity conflicts; withdrawn
authority cannot recover through a key. Recovery does not create another batch.

Reviewer scope excludes whole analysis tasks whose frozen cohort contains that
actor, as assignee or submitter, before detail counts and queue pagination. The
participant IDs are frozen internally so source deletion cannot widen that scope.
Apply the same exclusion to direct batch-context reviews and rework reads. Old
batches and ordinary review APIs retain their existing contracts. Repeat current
principal/actor/role and both mode gates after read materialization, including
same-role identity remapping. Eligible Reviewers see only generic frozen selection
reasons. They never receive the full classification
manifest/hash, cohort or other annotators' answers. Only Managers receive the
analysis provenance reference. Annotators gain no batch access. New responses
are private/no-store; evidence-bearing new responses also prohibit referrers.

An ephemeral salted viewer HMAC correlates capability/detail/queue/rework responses
for the same principal/source, actor, project/organization and role. Mixed responses
clear the view; changing the viewer resets queue state. This pin is not authority,
is never accepted in planning input and is not stored in a plan or receipt. Secret
rotation therefore cannot invalidate original-key recovery.

## Limits and browser behavior

Keep the 1,000-review-candidate limit and all classification limits: 100 tasks,
20 participants per task, 2,000 total participants and 8 MiB cumulative canonical
snapshot JSON bytes, inclusive. Reject excess with 422; do not truncate, silently
sample analysis inputs or lift the limit. The existing one-row loader measures
canonical bytes before retaining the next row. It is not wire-size preflight.
Review planning checks the bounded candidate ID denominator before loading answer
snapshots, then validates sources one row at a time and retains only identity
pins. Enumeration drift fails closed. Stable legacy plans retain identical hashes.

The existing form adds an explicit paginated frozen-analysis selector. Changing
inputs clears the preview. Explain frozen conflict coverage, unknown inputs and
quota overflow in both languages; preserve the original queue/filter/navigation.

For this strategy, uncertain writes retain their original body/key and offer
explicit recovery outside authorization-dependent creation controls. Read refresh
does not POST. Unknown outcomes survive failed reads, blur and later errors;
late acknowledgements cannot confirm another generation. Before uncertainty,
recognized server rejections with matching HTTP statuses permit a new explicit
preview. Confirmed saves remain confirmed even if subsequent reads fail.
Blur invalidates all pending panel reads, including initial detail, analysis
selection and previews before any creation intent exists. It closes any rework
comparison so explicit GET refresh stays reachable. In-flight or uncertain
creation writes retain their original body/key, including legacy strategy writes;
reads never become recovery POSTs. A definitive legacy 4xx rejection does not
create an uncertain-write recovery lock.

## Acceptance and remaining scope

Regressions cover deterministic priority/ties, rounding, mismatch/unknown cases,
frozen origin, old batch compatibility, source loss/corruption, exact byte and
candidate limits, independent role/mode/policy denial, PostgreSQL same-key and
different-body races, revocation/revision-first ordering, response rollback and
original-key recovery. Bilingual synthetic PostgreSQL browser journeys verify
selection coverage, real commit with lost acknowledgement, exact recovery,
unchanged historical Submissions and legacy plans, plus Reviewer/Annotator scope.
The separate review-priority workflow retains existing enterprise journey budgets.

Classification partial matching and single-batch project/annotator summaries are
implemented in classification-partial-match-v1.md and classification-quality-overview-v1-design.md.
Manual NER and independent durable NER queues are documented in ner-disagreement-v1.md
and ner-batches-v1.md. #81 still needs NER aggregation/arbitration and bbox/field
contracts and workflows. #84 still needs low-confidence contracts and second review;
its implemented exact review/arbitration handoff is in review-arbitration-handoff-v1.md.
Arbitration already exists as a separate frozen-adoption ledger; it is not an
approval decision. Neither issue is closed by this slice.
