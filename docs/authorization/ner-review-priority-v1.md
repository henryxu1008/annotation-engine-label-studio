# Optional frozen NER conflict-priority review sampling

This is an optional strategy in the existing ReviewBatches workflow, using an
existing persistent NerBatch. It does not change NER metrics, submission states,
review authority, arbitration adoption, release policy or default sampling. It
does not connect to Host or Core. #81 and #84 remain open.

## Authority and API

`ANNOTATION_NER_REVIEW_PRIORITY_ENABLED` is disabled by default. Creation requires
the current Manager, READ_WORK and ORGANIZE_REVIEW, ordinary review gates and
exact fork_review mode, plus both NER analysis/batch gates and the project's
exact NER fork_review mode. It consumes existing analysis; ORGANIZE_NER, REVIEW
and ARBITRATE_NER are not requirements for organizing a sample. A Manager still
cannot make review decisions. Server session/token resolution supplies the
principal, provider source and local actor. Request actor fields are rejected.

Existing `POST /api/projects/{id}/review-batches/preview/` and collection POST
accept `priority: {kind: "ner_conflict", batch_id, manifest_hash}`. Creation uses
the existing expected_plan_hash and Idempotency-Key. Capabilities add independent
can_prioritize_ner and can_read_ner_priority fields. List, detail, queue and rework
reuse the existing endpoints and complete UI workflow. No parallel workbench or
new database table is introduced.

Current Manager/Reviewer reads require READ_WORK and both scopes. A Reviewer's
whole frozen self-cohorts, and own candidates outside the analysis, are excluded
before source loading, list counts and pagination. Reviewers receive neither the
full analysis hash nor participant sets. Annotators cannot read the batch.
Revocation and identity changes invalidate reads and unknown-command recovery.
Ordinary review POSTs independently require REVIEW, a selected source and all
existing review constraints. NER priority batches cannot enter the classification
review/arbitration handoff. Standalone NER arbitration is unchanged.

## Frozen rule and deterministic selection

The immutable plan uses `ner-conflict-ranked/v1` and
`review-ner-priority/v1`. It records the complete latest-pending candidate
denominator, full assignment/task/submitter/revision/result hash pins, selected
bits, full NerBatch hash, creator identity, frozen cohort participants, per-source
state/reason/calculation hash and the existing NER metric, selection, explanation
and engine versions. It copies no text, answer snapshots or engine inputs.

Candidate enumeration, package/submitter filters and global/per-submitter ceil
quota remain the existing rules. Within each quota group, verified frozen
conflicts sort first, then canonical seeded SHA256, then Submission ID. There is
no new severity metric. Conflict means the existing task outcome is disagreement:
at least one defined strict rational F1 is below one. Both-empty pairs remain
undefined; one-empty/nonempty is defined zero. Missing/unusable cohorts remain
unavailable. Uncovered candidates, nonmatching complete revision pins and
originally unavailable metrics are unknown, not agreement or conflict. An old
task conflict never transfers to a later revision by task ID alone.

Old plans, algorithms, hashes, receipts, default flags and classification sampling
remain compatible. Later source loss masks current availability without rewriting
the frozen historical selection or promoting unknown evidence to approval.

## Bounded work and recovery

Before snapshot decoding, enumerate at most 1,000 candidates and validate the full
NerBatch envelope: at most 100 tasks, 20 participants per task and 2,000 cohort
entries. The single guarded source union includes every review candidate and
every frozen NER source, deduplicated by Submission ID. SQL stored JSON must fit
64 MiB before any decoder; canonical snapshots must fit 8 MiB before preparation
or counters. Stored sizes are rechecked at hydration to catch concurrent growth.
Prepared work is bounded by W <= 5,000,000 per pass and official scorer calls by
2Q <= 380 for the whole batch. Logical plans and emitted responses fit 1 MiB.

Both official counter passes run on bounded pinned prepared inputs. Final source
replay and current authority checks follow the last counter; no nested two-pass
HTTP endpoint and no third scorer pass is used. Preview and fresh creation each
cost exactly 2Q official calls, with two guarded union reads for source replay.
An invalid manifest, envelope, SQL/canonical limit or logical plan rejects before
the first official call. No totals are multiplied by task-specific allowances.

Collection metadata, capability and original-key receipt recovery load zero
source snapshots and run zero NER scorers. Detail, queue and rework run zero NER
scorers, but deliberately validate ordinary review-source snapshots (including a
post-render replay); that I/O is not claimed to be free.

Creation locks Project first, then ordered tasks, assignments and submissions.
The frozen plan is checked locally and same-key receipt recovery precedes source
availability and changed processing budgets. Receipts bind body, principal,
provider source and frozen actor; changed scope/body/identity conflicts. Current
authority and stored-plan integrity remain mandatory on recovery. New revisions,
revocation, source mutation or response-time changes roll back a fresh creation
and leave its key reusable. Acknowledgement loss retains the original body/key;
UI reads never issue a recovery POST automatically. Viewer changes scrub old
intent/evidence/confirmation while same-viewer read failure retains a confirmed
save fact.

## Acceptance and remaining scope

Regression coverage includes deterministic global/per-person quotas, empty and
missing inputs, complete pin mismatch, legacy plans, disabled scopes, real Token
denial and revocation, source-bound receipts, 1,000/1,001 candidates, full-union
SQL/canonical limits, 380 calls and the next realizable 382-call rejection, and
post-counter/render mutation. Independent PostgreSQL connections cover same-key
and changed-body races, revocation/revision-first ordering, Project fencing and
snapshot growth after preflight. Frontend tests cover viewer changes, late reads,
invalid typed provenance, uncertainty and exact original-command recovery.
Bilingual synthetic E2E exercises Manager creation, lost acknowledgement,
Reviewer self-cohort isolation, ordinary rejection, original-annotator revision,
rework comparison, independent later review and revocation.

NER overview and standalone frozen-revision arbitration already exist. Remaining
broader #81 scope includes bbox/field comparison contracts and product acceptance;
#84 still needs low-confidence sampling contracts, independent second review,
and an explicit NER review/arbitration handoff design. Those require separate
designs. This slice keeps one ReviewDecision per Submission and does not claim
either issue is ready to close.

Local acceptance uses owned native PostgreSQL and loopback-only synthetic
fixtures. Docker/#76 remains read-only. Preserve historical OLD_AND_NEW_CHANGED,
CHANGED_DATABASE_CONTAINER_REPLACED, missing protected-worktree history, cause
UNKNOWN and full comparison NOT_RUN_AFTER_DOCKER_INTERRUPTION. Passing source
tests or CI cannot establish retained runtime or full database equality.
