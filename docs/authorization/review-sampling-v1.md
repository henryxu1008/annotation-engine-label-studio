# Local review sampling and rework (#84, first slice)

This slice organizes existing Fork reviews. It is not a new decision ledger,
platform review policy, release rule, dataset version or scheduler. #84 remains
open for remaining confidence strategies and second review. Optional frozen
classification conflict priority is documented in [review-priority-v1.md](review-priority-v1.md);
the implemented exact review/arbitration handoff is documented in
[review-arbitration-handoff-v1.md](review-arbitration-handoff-v1.md).

## Product workflow

A current project Manager previews a named batch over the latest pending
Submission of each assignment. Optional fixed-package and submitter filters
intersect that project scope. Overlapping packages contribute a Submission
once. A missing/cross-project package rejects the selection; unavailable fixed
members contribute no pending Submission. Cancelled source assignments retain
their existing authorized pending-review semantics.

The percentage is an integer from 1 to 100. SHA-256 ranks canonical
algorithm/project/seed/source-identity tuples with Submission ID as a tie-break.
Select ceil(N * percentage / 100), either globally or separately per submitter.
The UI displays requested and actual rates; per-person rounding can exceed the
requested global rate. The version is sha256-ranked-v1, not Python random state.
Both the full candidate list and selected flags are frozen, preserving an exact
denominator independent of later status, deletion, pagination or new submissions.
An empty preview rejects; more than 1,000 candidates rejects without truncation.

Creation requires the preview plan hash and a project-scoped UUID
Idempotency-Key. Under the existing project-first write fence, recalculate the
plan and reject changed candidates with REVIEW_BATCH_PREVIEW_CHANGED. Same-key,
same-request, same-principal/actor replay returns the original batch even after
source decisions; changed request or actor returns 409. No automatic job or
network service is involved.

Current Reviewers enter a selected-only queue in the existing ReviewerWorkspace.
Empty queues, lost authority and invalid source evidence stop the scoped flow;
there is no project-history or unrelated-work fallback. Pagination recovery
retains the batch context. The existing review command revalidates batch
membership/source integrity inside Project -> Task -> Assignment -> Submission
locks. It still permits one decision per pending exact revision, rejects self
review, and derives the reviewer from the trusted actor.

Batch-context decisions require both READ_WORK and REVIEW. The API retains
the project-first transaction through response serialization, then repeats
current actor/mode/operation checks and compares the emitted snapshot, source
identity, status and decision facts with the frozen source. A revoked scope or
invalid representation rolls the decision back instead of committing a write
behind a failed response. Queue serialization uses the same evidence check.
Preview also repeats ORGANIZE_REVIEW after materializing its plan. These checks
apply to explicit batch context; ordinary source reads/reviews keep their
existing contracts, including Core-only-review internal reads.

Reject with a reason, then use existing My Work and Editor to return work to the
original active assignment. Cancellation/reassignment does not revive that
assignment or grant a new one. Editor Update creates a new immutable Submission;
the old rejected result/hash/decision stays unchanged. The batch shows the newest
successor on the same assignment and provides read-only source inspection.
Review that new revision separately through the existing project review queue;
it is never silently appended to the frozen sample or automatically approved.
Progress includes its read time and explicit refresh.

### Progress and rework follow-up

The detail UI derives counts and filters only from the authorized `items`
projection: selected/unselected totals, then selected pending, approved,
rejected, superseded and unavailable counts. Only approved/rejected items are
in the reviewed filter. These are display projections, not a second persisted
batch-completion state or decision ledger. Reviewer counts and sample rates
explicitly describe their visible subset, excluding their own submissions.
Filters never recalculate the frozen denominator, selection or server authority.

After a successful review command, the existing queue and batch detail/capability
reads refresh independently. The detail refresh keeps the queue mounted and
retains its current selection and filter. A failed detail/capability refresh
clears authorization-derived details/actions and offers a read-only retry for
the same batch; the saved-review notice prevents repeating the write merely to
refresh progress. Context guards ignore an old batch/project's late responses.
Native fetch-failure Responses and nonconforming detail projections also take
this failure path, including initial selection and post-decision refresh. Only
a matching decision acknowledgement confirms a save; a failed progress read
does not confirm an unknown write or authorize replaying it.
The existing Zod dependency validates the narrow list/detail/capability view
contracts without coercion, including all rendered scalar and item fields.
Nullable/empty rejection reasons and unknown successor states keep their existing
display semantics; invalid truthy permission or selection strings are rejected.
A pending row with no current review action differs from an empty filtered view
and from a sample with no currently reviewable items. None implies full approval.

The rework column displays both the immutable original decision/reason and the
latest successor revision's current state. A rejected original stays rejected
even when that successor is approved. Unknown successor states remain explicitly
unavailable; no new review command, automatic approval, reservation, rework task
ledger, policy or release authority is introduced.

Unselected, pending and unavailable are not approval. Even if a selected subset
passes, that supplies no approval for the remainder. Existing ordinary project
reviews remain available to authorized Reviewers; a batch is not an exclusive
review reservation. Explicitly reviewing an unselected item through the ordinary
workflow remains legitimate. Neither sample membership nor batch completion
authorizes /release/, #79 approved export, #80 frozen Manifest or delivery.

## Routes and authority

All routes below are under /api/projects/{project}/review-batches/:

| Route | Method | Authority |
| --- | --- | --- |
| capability/ | GET | Manager or Reviewer; current READ_WORK |
| preview/ | POST | Manager; ORGANIZE_REVIEW and READ_WORK |
| collection | GET | Manager or Reviewer; paginated metadata |
| collection | POST | Manager; ORGANIZE_REVIEW and READ_WORK |
| {batch}/ | GET | Manager or Reviewer; frozen identities and live progress |
| {batch}/queue/ | GET | Reviewer; selected, pending, non-self snapshots and REVIEW |
| {batch}/rework/{source}/ | GET | Manager or Reviewer; READ_WORK, visible frozen rejected source; paginated later revisions |

The existing POST /api/submissions/{id}/review/ accepts an optional positive
review_batch query context. Invalid, foreign or unselected contexts reject.
Removing that context uses the ordinary pre-existing authorized review command;
it does not grant a different role or release authority.

No actor/reviewer/result/status fields are accepted by batch creation/preview.
Manager batch planning does not imply Reviewer permission. Annotators have no
batch APIs; they retain their own existing work, reason and Editor access.
Reviewer projections exclude their own sources. All endpoints repeat current
account/organization/project scope, include private/no-store responses, and
recheck materialized read authority. Queue responses also verify the actual
serialized snapshot against frozen source hashes before returning it.

Frozen identity includes Submission, assignment, task, revision, original
submitter and content hash. Validate the live source graph, snapshot hash and
matching ReviewDecision for approved/rejected states. Missing sources remain
unavailable without shrinking the denominator; changed/corrupt sources reject.
Snapshots/answers are not copied into the batch table.

The additive ReviewBatch table has a unique project/key constraint.
PostgreSQL and SQLite update triggers protect the plan, hash, name, request,
creator identity and project binding, including bulk ORM updates. Nullable
creator-FK cleanup does not rewrite the frozen creator. Project cascade deletion
remains existing behavior; these local work plans are not a permanent audit log.

## Explicit activation and deferred ownership

Default: ANNOTATION_REVIEW_BATCHES_ENABLED=false. Additionally require the
operator-controlled ANNOTATION_REVIEW_BATCH_MODES JSON mapping, for example
{"7:12":"fork_review"} for organization 7/project 12. It is independent of
APPROVED_EXPORT_POLICY and export credentials. Missing/unknown/core_only_internal
modes deny every new batch route and batch-context decision. No retained
environment is activated by this implementation or its tests.
This first slice enables only PostgreSQL and SQLite, which have the frozen
plan triggers and regression coverage. Other database backends reject activation.

Core-only-review ordinary internal Submission reads keep their existing
contract. This patch does not connect Core, translate Core decisions, enable
delivery, or globally retrofit a different policy onto legacy Fork review APIs.
The new mode mapping is a narrow local workflow opt-in, not Host IAM or a
platform policy engine.

The legacy project-operation provider offers ORGANIZE_REVIEW only when this
feature is enabled. A #86 provider may independently deny planning, REVIEW or
READ_WORK; read-only progress must remove review actions. No lifecycle state or
transition is inferred/implemented here. #83 package membership, personal work,
assignment and navigation mechanisms are reused. No Host files are modified.

## Validation

review_batches/tests covers deterministic/global/per-person rounding, exact
1,000/1,001 limits, package union, current latest revisions, stale previews,
original-key replay/conflicts, frozen DB guards, source corruption and deletion,
trusted mapped actors, revocation, invalid contexts and release/download negatives.
Independent PostgreSQL connections exercise same-key creation, two reviewers,
revocation-first creation/decisions and supersede-first decisions.
They also reject concurrent different requests for one key and prove with a
separate NOWAIT connection that the project write fence survives through
decision-response serialization. Negative cases cover response-time revocation,
tampered serialized evidence with full rollback and original-key recovery,
independent read/review/planning policy withdrawal, other actor/principal key
replay, every frozen update field, and all new batch routes/retries in Core mode.

ReviewBatches component tests and en-US/zh-CN browser journeys cover preview,
creation, sample-only review, rejection, Editor rework/new revision, separate
approval and unchanged old evidence. seed_review_batches_e2e creates a separate
synthetic organization/users/projects once, requires TEST_ENVIRONMENT, refuses
existing fixture identities/output, and never deletes existing data. The CI
workflow explicitly opts in only those generated synthetic project IDs.

ReviewBatchProgress component integration tests exercise the real ReviewerWorkspace
with synthetic API responses: independent queue/detail failures after a saved
decision, read-only retry without repeating the command, revoked review actions,
late batch/project responses, overlapping successor/detail reads, and bilingual
counts/filters that keep original rejection separate from successor approval.
The browser journey also asserts automatic progress updates and successor states
before and after the separate new-revision review.

Review queues reset their entire local state when project, package or batch scope
changes. Late decisions cannot refresh another scope or retain its processing
state. A thrown review transport error has an unknown outcome: refresh both queue
and batch progress without replaying the write or claiming a confirmed save.
Bilingual component regressions include a committed decision with a lost response.
Only a response matching the submitted ID and decision confirms a save; native
fetch-failure Responses and mismatched acknowledgements require read recovery.

Feature activation, deployment, merge, real provider acceptance and Core
integration are separate actions. This first slice does not close #84.

## Read-only rework revision comparison

The existing batch follow-up row now opens a comparison with the original
rejected Submission, replacing the latest-snapshot-only inspector. Returning
preserves the batch/filter and refreshes current authority and progress. The
comparison offers every available later revision on that assignment, newest
first with bounded pagination, and displays both snapshots, IDs, revision/hash,
status and decision/reviewer/time/reason. Intermediate rejected and superseded
revisions remain selectable. A missing original never falls back to a successor.

The new GET is anchored to a candidate in the frozen batch, including unselected
candidates that were explicitly rejected in the ordinary review workflow. It
requires the existing feature/mode gates and current READ_WORK. Annotators are
denied, Reviewer self-sources are excluded, and batch/project/source ID swapping
does not expand scope. Later revisions must share the original assignment,
task and submitter and pass snapshot hash, project/task and decision validation.
Both original and page representations are checked against fresh sources after
serialization, followed by a current role/policy check. Source corruption,
missing original, source changes during serialization and revocation fail closed.
The batch is also reloaded after serialization: deletion or a changed frozen
plan (even with a matching replacement hash) discards the assembled comparison.
Independent PostgreSQL connections cover these response-time changes, membership
revocation, original deletion and a newly committed successor decision.
Responses are private/no-store. No REVIEW permission is added merely to read.

The UI clears snapshot evidence before each read, on failed reads and tab blur;
read-only refresh recovers access without replaying a decision. Scope generations
discard late responses. It reuses jsdiff's canonical JSON comparison with a
100 ms / 2,000 edit bound; if that bound is exceeded the snapshots remain visible
with an explicit unavailable difference calculation. Array order is significant;
JSON object key order is not. Answer-change feedback compares annotation results
only, while the full snapshot diff also includes task/configuration/metadata.
Unchanged answers are explicitly labeled and never imply successful correction.

This is a transient read projection, not a new frozen Manifest, audit ledger,
quality metric, approval policy or rework scheduler. Core-only mode denies the
new endpoint; #79/#80 exports and Host contracts are unchanged. #84 remains open.
Frozen classification conflict priority now reuses this workflow. The independent
#81 arbitration ledger can adopt a frozen revision. Classification handoff is
implemented in review-arbitration-handoff-v1.md; second review and explicit NER
handoff remain deferred. Optional NER priority is in ner-review-priority-v1.md.
