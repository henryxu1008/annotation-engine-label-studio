# Read-only text classification disagreement (#81, first slice)

This is an opt-in Fork projection, not arbitration, a decision ledger, review
approval, assignment, risk-priority sampling or a Host/Core integration. #84
sampling and all existing Submission/ReviewDecision semantics are unchanged.
Neither My Work's structural `conflict` state nor Task.precomputed_agreement is
treated as quality evidence.

## Workflow and frozen input contract

Reuse the existing project Data Manager and modal. A selected task prepopulates
the task field; otherwise choose its ID. Load only currently authorized revision
identities, then explicitly select up to 20 revisions, one per distinct trusted
submitter. Sources are bounded at 100; larger sets reject instead of truncating.
There is no automatic latest-revision selection. Reviewers cannot see or select
their own submissions; the comparison denominator uses their visible inputs.

All endpoints are GET under `/api/projects/{project}/classification-disagreements/`:

- `capability/`: current availability, no answer payload.
- `tasks/{task}/sources/`: visible IDs, exact revisions, source/config hashes and status.
- `tasks/{task}/?submissions=1,2`: compute on those explicit revisions.
- `tasks/{task}/?frozen=...`: revalidate and recompute precisely the signed input receipt.

The signed receipt freezes project/task, rule version, calculation time and the
Submission/assignment/task/revision/submitter/content/configuration hash tuples.
The manifest hash includes the frozen calculation time. Rechecking retains that
manifest, hash and token; `observed_at` separately records fresh access/source
checks. A newer revision never replaces a pinned input. Reordering input IDs
does not change the canonical source order. Source changes invalidate the
receipt; deletion returns unavailable without substituting another revision.

The first slice writes no database rows and creates no analysis or arbitration
ledger. Receipts contain provenance, not answer snapshots, and grant no access.
They are transient read evidence, not permanent audit retention or an external
delivery Manifest. Secret-key changes can invalidate them; failure never silently
creates a replacement analysis. Later durable metric history needs its own design.

## Metric and unavailable boundaries

Rule `classification-exact-sets/v1` supports static, global Choices targeting
Text with explicit configured labels, including multiple-choice sets. It uses
the existing defusedxml library, not an XML parser implementation. Per-region,
conditional, dynamic/aliased labels, NER, bbox and other controls are unavailable.
Text must use inline task data (the normal default or explicit `valueType="text"` /
`valuetype="text"`). URL-backed Text is unavailable even if every frozen URL is
identical: Submission snapshots do not preserve the fetched document body, so
neither matching addresses nor a signed receipt proves identical displayed text.
The projection never fetches a URL to manufacture missing immutable evidence.
Text resolvers (including CSV with explicit inline `valueType="text"`) are also
unavailable: the upstream editor resolves them before applying valueType. Choices
with a dynamic `value` remain outside the static-label first slice, even when
static child Choices are also present. Attribute names follow upstream lowercase
semantics; contradictory case-duplicate attributes are rejected.
Exact frozen XML must match, and frozen task data must match. This intentionally
does not infer equivalence between different configurations or question contents.

For n distinct submitters, enumerate all n(n-1)/2 unordered pairs. A pair matches
only if its selected-label sets match for every classification control. Overall
agreement = matching pairs / all pairs. Also return each control's pair counts,
ratio, `from_name -> to_name` location and explicit choices keyed by source ID.
No rounding occurs in the API; the UI displays two decimal percentage places.
This is exact-set agreement, not a statistical reliability or correction-quality
score. Choice order and result region IDs do not affect the metric; all source
snapshots/hashes still remain unchanged and pinned.

Fewer than two submitters, duplicate submitters, missing trusted identity,
cancelled/missing/malformed values, invalid source evidence, unknown labels,
duplicate control outputs, configuration mismatch, task-data mismatch and deleted
frozen inputs produce explicit unavailable reasons with null scores/counts and
no answer locations. They never become 100%. Unsupported configurations cannot
partially contribute a misleading denominator.

## Authority and failure behavior

Require authenticated server-resolved actors, live account/organization/project
membership, Manager/Reviewer role and READ_WORK. REVIEW is not required for this
read. Both collection and exact-source reads repeat object scope; ID swapping,
mutable actor fields, self-sources and cross-project receipts do not expand it.
Refetch exact inputs and live authority after constructing results. Concurrent
revocation, deletion or changed evidence discards the materialized answer payload.
Every repeated check must resolve the same trusted principal, mapped local actor
and role. A same-role remapping cannot retain the original actor's self-source
filter or borrow the session user's wider permissions. Drift discards source
identities and report values, including drift during final serialization.
Responses are private/no-store; referrer policy is no-referrer. UI generations
discard stale scope/blur responses and clear evidence before reads or after
failure. Frozen retries use GET only. Returning through the existing modal keeps
the Data Manager mounted and restores focus to the trigger.

Default `ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=false`. Separately require
`ANNOTATION_CLASSIFICATION_DISAGREEMENT_MODES={"org:project":"fork_review"}`.
Missing/unknown/Core-only modes deny the new routes. This mapping is independent
of #84 and export credentials; it is not Host IAM or a platform policy engine.
No retained environment is enabled by implementation or synthetic tests.

## Validation and remaining scope

PostgreSQL/SQLite regression tests cover exact, multiple-choice and multi-control
metrics; 2/3-person inputs; signed replay; malformed/cancelled/missing/configuration
boundaries; self/annotator/foreign/revoked/policy negatives. Independent PostgreSQL
connections cover source changes/deletion, organization revocation and a newly
inserted revision while a response is materialized. Bilingual component and real
browser journeys use isolated synthetic users/projects only. CI seeds that
separate fixture and enables only its exact project IDs.

The opt-in durable project batch is documented in classification-batches-v1.md;
it does not change the transient manual comparison contract above.
NER, bbox, field comparison, confidence rules, metric aggregation and second review
remain deferred. Durable batches and frozen-adoption arbitration are implemented
in classification-batches-v1.md and classification-arbitration-v1.md. Optional
conflict-priority sampling reuses frozen batches; see review-priority-v1.md.
#81 and #84 remain open. Merge, deployment and Core integration are separate approvals.
