# Frozen field review to arbitration handoff

This default-off Fork slice connects one selected exact field conflict review
source to the existing field arbitration lifecycle. A Manager explicitly dispatches
or links an independent arbitration. Arbitration does not approve, reject, rework
or release the Submission. #81/#84 remain open.

The [approved design](field-review-handoff-v1-design.md) and
[73-case matrix](field-review-handoff-v1-acceptance.md) are the design-stage
snapshots. Executed local evidence is recorded separately; definitions and prior
#128 CI are not acceptance of this slice.

## Typed origins and authority

New writes require a `field-conflict-ranked/v1` ReviewBatch with a selected
`frozen_conflict` candidate, the complete verified original FieldBatch, all nine
source-pin fields, every field rule including the raw-difference queue rule,
and the selected current latest pending Submission without ReviewDecision.
Other cohort inputs remain frozen. A normalized 100% format-only raw difference
still counts as conflict. Missing/null/unknown inputs never become agreement.

`FieldReviewArbitrationHandoff` explicitly refers to FieldArbitrationTask. Its
immutable metadata binds project/organization, parent UUID/body hash,
principal/provider/actor, complete review proof and both original source
provenances. It copies no answers. PostgreSQL/SQLite protect PKs, scope,
identities, frozen references, time and receipts. Nullable convenience FKs may
only become NULL; frozen IDs cannot restore or rebind them. Existing project
cascade semantics apply.

Equal inputs may link across different FieldBatch origins, but both complete
Manifests and the full item must independently validate. The existing field
input hash and complete equality are required; task IDs, pairs or scores alone
are insufficient. Pending v1 and resolved v2 targets can link; cancelled targets
require an explicit new dispatch. Linking never reassigns or restores adoption
authority, and source B cannot be repaired by substituting source A.

Require `ANNOTATION_FIELD_REVIEW_ARBITRATION_HANDOFF_ENABLED=false` to be
explicitly enabled, plus all original review/field priority/batch/analysis/
arbitration flags and exact organization:project Fork-review mode maps.
Core-only/unknown scope defaults deny. New writes and recovery require current
Manager + READ_WORK + ORGANIZE_FIELD_ARBITRATION. Planning, analysis creation,
REVIEW and ARBITRATE_FIELD do not substitute for that operation and are not
additional prerequisites for using existing frozen inputs.

Reviewer history is the intersection of currently visible review candidates,
whole frozen self-cohort exclusion and current independent assigned target scope,
before counts, pagination and JSON loading. Annotators gain no access. Actual
adoption and known-ID security cancellation keep their original field endpoints
and independent permission rules. Files, media, export and bulk access gain no
new capabilities.

## Routes and command recovery

Under `/api/projects/{P}/review-batches/{B}/`:

| Route | Contract |
| --- | --- |
| GET `sources/{S}/field-arbitration-context/` | Manager preflight and verified eligible frozen input |
| GET/POST `sources/{S}/field-arbitration-handoffs/` | Scoped metadata history or explicit dispatch/link |
| GET `field-arbitration-handoffs/{H}/` | Frozen association and authorized current target metadata |
| GET `field-arbitration-handoffs/{H}/provenance/` | Complete A/B historical metadata verification, without pending/latest checks |

Schema `review-field-arbitration-handoff/v1`, kind `field`. Strict POST fields
are mode, expected_plan_hash, expected_input_set_hash, expected_viewer_hash and
either assignee_id or field_arbitration_id/expected_arbitration_version.
Actor/answer/status fields reject. The typed command hash includes route IDs and
the full canonical body. A fresh dispatch uses a separate server-private child
UUID with the original field create hash. Target and association commit together
or both roll back through render/final validation; no nested HTTP calls occur.
After transaction rollback, only the named parent-key, association-binding,
child-create-key and active-field-input unique constraints map to an explicit
`REVIEW_FIELD_HANDOFF_UNIQUE_CONFLICT` 409. PostgreSQL constraint names and
SQLite exact unique-column sets are allowlisted; unrelated database failures
retain their original handling. The client treats this proven rollback as a
definite rejection, while transport uncertainty still retains the original key.

Under current write authority, original parent recovery validates the fixed
local proof, owner/provider/actor, route/body/key before live source, CAS,
fresh-viewer or processing limits. It reads no live plan/Manifest/target JSON or
answer snapshots and performs no calculation. Old cancelled keys never redispatch.
Current account, organization, permission and feature/mode revocation still deny.

The existing field capability adds optional `handoff_owner_hash/can_handoff`
only while this feature is enabled. For a current authorized Manager, the owner
fingerprint is a stable SHA-256 of trusted principal/provider/actor/project/
organization/role; it is a comparison aid, never authorization. It lets an
already-attempted same-owner command survive viewer-salt rotation. Recovery
preserves the original body and UUID, and checks ACK against the current
capability viewer. A real owner/actor/provider/organization change clears the
intent. Default-off capability and classification/NER wire/hash semantics remain.

## Bounded work and final source checks

The shared NER/field metadata loader gates actual SQL byte sizes with CASE before
decoding, including remaining row/page allowance. One materialization admits
4 MiB canonical / 16 MiB stored distinct metadata: complete review plan, A/B
Manifests, association proof, target origin/participants/cancellation and resolved
decision winner/reason. Two materializations read at most 8/32 MiB. Cache hits
do not stand in for row locks; final replay has a fresh budget and SELECTs.

Target-only evidence keeps 20 participants, 20 fields, 4 KiB/raw value and
64 KiB per revision values/text/XML. Each of two hydrations gates 64 MiB SQL /
8 MiB canonical snapshots before decode. All CPU limits precede the first
calculation. Eligible successful context/dispatch/link uses exactly two official
field calculations, then a final source hydration/inspection with prepared replay.
There is no third calculation or whole review-candidate answer scan.

For I inputs and F fields, each pass performs I*F normalizations and
2*C(I,2)*F raw/normalized equalities, at most 400/7600; one successful request
therefore performs at most 800/15200, with zero external scorer calls. Double-empty
fields still incur real loops. Metadata-preflight ineligible context returns
200 false with zero snapshot/calculation. Mid-evidence source loss is 409,
budget overflow 422; failed work is not relabeled zero or complete two-pass work.

History, provenance, capability, assignees and original-key recovery perform no
snapshot loading/calculation. Proof is 64 KiB canonical / 256 KiB stored, body
16 KiB and actual response 1 MiB. History defaults 50/max 100; all proofs are
reserved before target/decision metadata, with cumulative limits and explicit
422 rather than silent truncation. Standalone field helper calls keep their
original public wire and default validation; the new internal bounded loader
still checks project/ID/complete Manifest/origin and runs only in the caller fence.

Project-first fencing, identity/member/User locks, existing target before sorted
FieldBatches and ordered Task/Assignment/Submission locks prevent competing
dispatch/link/review/revision commands from changing the frozen intent. Current
authority, organization, source graph, pending/latest, target version and rendered
representation are rechecked before returning or committing.

## Complete workflow and historical verification

Reuse ReviewBatches, the full handoff history/dispatch/link flow and
FieldArbitrations/FrozenArbitrationWorkspace through explicit typed adapters.
Unknown kinds fail closed; no classification fallback. Field commands include
expected_viewer_hash, independent ACK/hash/cost validation and the existing locked
`@noble/hashes` canonical implementation, including HTTP without SubtleCrypto.

Ordinary history is `source_integrity=not_checked`, decision=null; readable
metadata does not prove answers. The original B detail verifies shared frozen
snapshots first, but remains invisible/unretained until a subsequent provenance
GET verifies A/B complete metadata. Both responses bind current viewer, H/route
intent and existing target input/task-evidence/identity/status/version fields.
No new origin_hash is required from the old B DTO. Reviewer projections do not
add complete Manifest/target-origin hashes, cohort or principal/provider data.

Lost A alone masks the handoff even if standalone B still verifies; normal later
review/new revisions do not invalidate intact frozen history. A failed or late
provenance response cannot display answers or poison retention. Blur/identity
changes scrub evidence; generation checks precede visible/cache writes. UNKNOWN
ACKs retain exact parent body/key for explicit recovery, GET never POSTs, and a
same-viewer read failure does not erase an acknowledged save. Return restores
batch/page/filter/focus with fresh authorization.

The independent `field-review-handoff` workflow defines disposable PostgreSQL and
bilingual Chrome acceptance. A workflow definition is NOT_RUN until actually
executed. Local tests use report-owned native PostgreSQL and fresh synthetic
databases/HTTP/browser sessions; Docker/#76, other worktrees, retained data and
Host/Core are not test resources. No push/PR/merge/deployment or Issue closure is
implied. Preserve all retained CHANGED/UNKNOWN/interrupted-comparison history and
shared Biome baselines separately from this slice's engineering evidence.
