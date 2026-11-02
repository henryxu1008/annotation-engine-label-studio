# Independent supervisory review V1

The accepted effect is **supervisory_only**. This implementation follows the
[frozen design](second-review-v1-design.md) and its
[56-row acceptance matrix](second-review-v1-acceptance.md). Their historical
design-stage headers remain unchanged. Implementation was authorized separately
on 2026-11-02, after CI governance was merged to Fork main `4347019b8`.

## Effect and policy

An assigned independent Reviewer may append one `confirmed` or `concern` opinion
about an exact approved/rejected static inline Text + Choices submission in an
existing selected ReviewBatch. Neither opinion changes the initial decision,
approval, release eligibility, #79 release artifacts or #80 snapshots. There is
no automatic rework, arbitration handoff, assignment or second opinion retry
with a different person. Pending cancellation permanently occupies the subject
slot, as do completed tasks.

The new APIs default off. Enabling requires `ANNOTATION_SECOND_REVIEW_ENABLED`,
an operator-owned stable UUID in `ANNOTATION_SECOND_REVIEW_INSTANCE_ID`, and an
org:project `fork_review` entry in `ANNOTATION_SECOND_REVIEW_MODES`. The explicit
`organize_second_review` and `second_review` operations are never added by the
Legacy operation provider. READ_WORK, organization, active account, current
project membership and role remain authoritative on the server. Only a Manager
can organize; only the live assigned independent Reviewer can record an opinion.
SECURITY_CANCEL permits a current Manager to terminate a pending task after work
operations are revoked, while still enforcing the new feature/mode/instance and
current account/org/project scope. Its capability response and cancellation
return no source answers.

## Frozen origin and lifecycle

The additive `review_batches` carriers and migrations preserve the original
ReviewDecision OneToOne and ReviewBatch plan/hash contracts. Each subject is
unique across every batch/key/assignee and terminal state. Frozen provenance
binds the full plan hash and selected candidate, nine identity/data/config pins,
the complete initial-review reason/time/actor proof, fixed rule/input/effect and
the separate stable instance identity.

The existing initial-review table is not a database-frozen audit ledger. This
slice therefore verifies its full proof twice and retains SET_NULL links to the
original decision and humans. Deletion followed by reusing a numeric ID cannot
restore those links. Role loss alone does not invalidate a legitimate historical
initial review. An old dispatched exact revision remains reviewable after a new
revision appears; fresh dispatch still requires latest. Missing or changed
origin data is unavailable, never replaced with current data.

The targeted loader reads a bounded complete plan and applies its original
ordinary/classification/confidence policy. It loads only the target snapshot and
initial review, reusing the existing snapshot byte gate and static classification
parser. A narrow `supervision` projection omits unused submission timestamps and
User profiles and SQL-gates the needed initial-review timestamp. Confidence
history does not consult a fresh profile registry or current producer grant.
No scorer, metric calculation, model call or other cohort answer is loaded.
The response says `target_source_verified` and `metric_integrity=not_checked`.

Project/member/User fences precede Task → Assignment → Submission → initial
ReviewDecision → ReviewBatch → SecondReviewTask → SecondReviewDecision locks.
Scalar hints gather related humans before those locks and are rechecked after
the Project fence. Rendering and a real second source pass occur inside the
transaction; current identity and operation policy are checked at the end.
PG deferred consistency rejects a terminal task without its matching opinion.
SQLite additionally rejects invalid transitions and insertions, but an externally
created pending+opinion half-state is reported as corruption by reads/writes;
it is not described as deferred enforcement.

## Bounded transport and recovery

The strict parser limits actual request bytes, including short/chunked reads,
rejects duplicate/unknown keys and false integer types, and accepts no actor,
reviewer, effect, status, first-review proof or Host fields. Snapshots, complete
metadata unions, full reasons, actual response wrappers and bounded scalar names
have the frozen byte limits. SQL preflight and CASE/window gates protect actual
JSON/Text/name materialization, including SQLite varchar/date affinities. The
legacy plan projection excludes unrelated timestamp/UUID columns. Assignee
filtering precedes count/page; pages contain only id/display_name. Signed cursors
bind the current scoped viewer, source and ordering.

Dispatch, decision and cancellation own separate immutable typed receipts and
key namespaces. A same-owner, same-body original-key recovery is selected before
any live origin lookup. It reads only bounded local ledger/receipt metadata,
never a snapshot, original first-review reason, batch or producer fact. Metadata
and recovered receipts are explicitly `not_checked` for current source health.
Viewer salt rotation preserves the original UNKNOWN body/key for the same owner;
real owner/scope/instance changes clear it. A read retry never posts a command.
Successful command acknowledgement and subsequent history failure are separate.

The UI reuses the existing immutable snapshot presentation, Manager batch row
entry and Reviewer work area conventions. DTOs are validated before caching;
source/record/first-review hash bindings and generation/owner checks discard
late or invalid responses. Existing filters and return focus remain intact.
The initial decision and supervisory opinion have separate bilingual labels.

## Acceptance and preserved scope

Local acceptance uses newly created native PostgreSQL/SQLite and synthetic browser
resources only. The synthetic seeder requires a matching bootstrap ownership
proof (actual data directory/system identifier/database/port) and an empty
business scope; a loopback hostname and database name alone are insufficient.
The new browser journey is also runnable independently through
`cypress.second-review.config.ts`; extending the closed remote UI inventory is
not represented as remote execution in this local stage.

The execution evidence and reviewer findings are in the task report directory:
`C:/Users/Henry/.codex/reports/ls84-second-review-impl-20261102`.
Final matrix outcomes, tested local commit/tree and exact reviewer binding are
recorded there after execution. This document does not turn unexecuted external
acceptance into a pass. Deployment, real users, Host integration and Issue closure
remain NOT_RUN; retained runtime/full-database equality remains UNKNOWN. Fork-only,
#81/#84 OPEN, local Docker/#76 and every preexisting worktree remain preserved.
