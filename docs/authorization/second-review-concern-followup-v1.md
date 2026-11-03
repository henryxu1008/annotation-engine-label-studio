# #84 second-review concern controlled follow-up V1

This optional Fork extension records one independent Manager disposition for an
existing completed `SecondReviewTask` with a `concern` opinion. It supports only
the existing static inline Text/Choices contract. Its effect is always
`supervisory_only`: it changes no initial ReviewDecision, batch selection,
submission status, assignment, release eligibility, #79 export or #80 snapshot.
The approved design and its original 56-case acceptance requirements remain in
[the design](second-review-concern-followup-v1-design.md) and
[the design matrix](second-review-concern-followup-v1-acceptance.md).

## Authority and activation

`ANNOTATION_SECOND_REVIEW_FOLLOWUP_ENABLED` defaults to false. Activation also
requires the existing second-review switch, a stable instance UUID, an explicit
`fork_review` project mode, PostgreSQL or SQLite, a current Manager with
`READ_WORK`, and the explicit `RECORD_SECOND_REVIEW_FOLLOWUP` operation. Legacy
providers do not automatically grant the new operation. Human identity comes
from the trusted server mapper; client actor/effect/time fields are rejected.

The handler cannot be the original submitter, assignment owner, first Reviewer,
second Reviewer, any frozen classification-cohort participant, or the selected
target's first Reviewer. These exclusions are checked before returning answers.
Collection, detail, preview, command, replay and final output all enforce current
scope. Metadata readers need not be the original handler, but a replay does.

## Frozen evidence and commands

`GET /api/projects/{pk}/second-review-followups/` lists only concern metadata,
including recorded evidence whose original graph has since disappeared. The
signed cursor binds current scope, owner and ordering. Neither count nor paging
hydrates snapshots or the complete review batch.

`GET .../context/{second_review_task_uuid}/` validates the exact original frozen
dispatch, complete concern opinion, initial review and nine submission pins.
Optional `related_submission_id` is selected explicitly from narrow successor
metadata. The old source is verified historically, without re-scoring confidence
or requiring a retired producer grant. A successor must be a higher, latest
revision on the same active assignment, task, owner, submitter, frozen text and
label config. Pending has no initial review; approved/rejected has a complete
matching independent initial proof. Both answer projections are atomic.

`POST .../` accepts the strict eight-field command plus a UUID Idempotency-Key.
`no_change` requires a nonblank reason and null target. `revision_linked` requires
an eligible existing target. Reasons preserve Unicode and exact bytes, with the
existing 2,000-code-point / 8,192-byte limits. Expected concern/context/viewer
hashes and version zero bind the preview. The receipt records protocol schema,
rule, effect, trusted handler, time, exact source/target hashes and recorded target
status. There is no cancel, reopen, reassignment or second disposition command.

A Project fence is acquired and the key is rechecked **before** any live origin
hints. A same-owner, same-body recovery reads only its own bounded frozen record,
even after source deletion or viewer rotation. It returns the original receipt
with `replayed=true` and `source_integrity=not_checked`. Other bodies/owners
conflict. A new key cannot replace the existing disposition. Fresh writes lock
the complete related human/source set, revalidate before insert and after render,
and roll back if authority, source, typed receipt or rendered bytes change.

## Storage and budgets

Migration 0009 creates `SecondReviewConcernFollowup`; 0010 adds PostgreSQL/SQLite
guards. Business fields are immutable; convenience FKs may only become null.
Insertion binds the actual completed second task and concern opinion, scope,
instance, full source/opinion documents and their receipts. SQLite additionally
rejects duplicate logical JSON keys, including Unicode-escaped duplicates, so
SQL's first-key semantics cannot disagree with Python's last-key decoder.
Object key order is not evidence identity; numeric storage changes such as
`1` to `1.0` are protected. Direct child deletion is refused while its Project
exists. Project deletion performs database-owned cleanup after the parent is
gone; Django uses `DO_NOTHING` for that relation to avoid premature child DELETE.

The existing `Budget` supplies SQL byte preflight and CASE gates before decoding
selected payloads and scalars. A narrow adapter reads only the original and
selected target. Shared 8 MiB canonical / 32 MiB stored request budgets include
repeated final verification, with the existing 256 KiB snapshot, 512 KiB snapshot
union, 2 MiB stored snapshot, 4/16 MiB plan, 32/128 KiB metadata and 4 MiB output
limits also enforced. Malformed SQLite converters return typed corruption rather
than a 500. No deferred task data/user profile hydration is used.

The authority remains the legacy exact Python snapshot hash and frozen source
pins. A separate `presentation_hash` verifies the browser-visible JSON after
ECMAScript binary64 conversion. Python RFC 8785 and the existing open-source
`fast-json-stable-stringify@2.1.0` agree on that representation, including numeric
keys, non-BMP keys, `1.0`, `-0.0`, exponent boundaries and old large integer
metadata. Such metadata may round in the browser; this display digest never
substitutes for the exact authority hash. Values outside the finite binary64
domain fail closed. Cross-language vectors are committed with the tests.

## Browser behavior and upgrade boundaries

The Manager workspace shows original initial review, concern and disposition as
separate evidence, plus target status **at recording time**. It has explicit
successor selection, reason and supervisory-effect confirmation. It does not
claim resolution or approval. Generation, project and owner checks discard stale
responses. UNKNOWN retains a frozen original body/key and offers only exact
recovery; refresh does not resend. Read denial clears answers.
The upstream shared API proxy mutates its caller's body while merging transport
parameters. Each send receives an independent shallow transport copy; the frozen
original command remains the source for hash checks and exact recovery.
Explicit actions
move focus before controls disable/unmount; async completion does not steal it.
The existing second-review parent's refresh/return follows the same lifecycle.

The existing bilingual `second-review` Electron call covers both dispositions,
lost ACK recovery, delayed refresh and trusted native Space/Enter. Fixture opt-in
is explicit; old fixtures force the follow-up flag off. The CI inventory remains
10 shards, 25 calls and 11 producers with unchanged required checks/timeouts.

Keep the following administrative boundaries visible during upgrades:

- SQLite's unordered whole-database `flush` is not guaranteed while append-only
  children remain. Legitimate Project cleanup followed by real flush is tested;
  no FK-off or guard bypass is introduced.
- Existing organization deletion can be blocked by `JWTSettings.DO_NOTHING`.
  Its rollback is retained; authorized dependency cleanup is a separate concern.
- Audit download, arbitration linking, product resolution, Host policy, deployment,
  real business acceptance and Issue closure remain outside V1.

Local acceptance and independent review are recorded separately from any future
remote CI or merge evidence. No local test authorizes activation or publication.
