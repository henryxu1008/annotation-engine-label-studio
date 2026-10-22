# Fixed task packages and read-only progress (#83, second slice)

This is a project-local grouping adapter over the existing fixed assignment,
submission and review mechanisms. The host platform still owns product-level
allocation policy. There is no second scheduler, annotation state machine,
review ledger or release manifest. #83 remains open.

## Fixed membership

With `ANNOTATION_FIXED_PACKAGES_ENABLED=true`, a project manager can preview and
create a named list of 1–1000 explicit, unique Task IDs. Instructions are shared
with authorized workers; source notes and membership digests are manager-only.
Data Manager's explicitly checked IDs prefill the form. A dynamic filter or
"select all" is never silently resolved into a fixed list.

Creation validates the entire selection under the existing Project-first fence:
Project → OrganizationMember → User → ProjectMember → ordered Task rows. The
manager role is rechecked after waiting for this fence. A missing/cross-project
task rejects the whole operation. A project-scoped UUID `Idempotency-Key` stores
the canonical request digest and trusted creator with the package itself; an
identical authorized replay returns the same object, while changed content or
actor returns 409. No TaskAssignment, Annotation, Draft or Submission is created.
UUID generation reuses the already-locked `uuid` 8.3.2 implementation, declared
as a direct dependency. It works on ordinary HTTP origins where the browser's
secure-context-only `crypto.randomUUID` is absent, preserving the UUID API
contract without changing certificate trust. The UI reads creation failure
machine codes from the actual API error envelope: unavailable tasks require
selection repair, conflicting keys require checking the existing creation,
and authorization failures discard the old creation form.

Membership cannot be edited or deleted through this API. Each item retains its
original task ID and creation time plus a nullable live FK. Deletion, relocation
or an inconsistent identity produces an unavailable item in the manager view;
it does not shrink the fixed denominator. Different packages may reference the
same task; their totals must not be added as distinct work. They never create
duplicate assignments. Project deletion retains the existing cascade semantics;
these rows are not a permanent audit ledger.

This freezes **IDs only**. Task data, media, labeling configuration and answers
are not frozen. #80 owns immutable dataset snapshots and release content; this
candidate neither imports nor requires the unmerged PR #100 implementation.

## Read authority and progress

All routes derive the principal/actor from the authenticated server-side
identity provider and check current account, organization and project authority.
Collection and detail queries share the same scope. Request actor, reviewer,
assignee, lifecycle and dynamic-filter fields are rejected. Replies and errors
are private/no-store. No task body, annotation result or submission snapshot is
returned.

Every fixed-item/live-task/pending-review aggregate retains the same live
visibility predicates. Before returning, recheck the exact original project
role and this page's package IDs in one query, including last-assignment
cancellation and the last eligible review leaving pending. A changed scope
rejects the whole response instead of returning previously materialized private
metadata or counts. Refresh READ_WORK policy from a newly loaded Project before
exposing action flags. These GETs do not acquire the project write fence.

| Project role | Visible scope | Actions |
| --- | --- | --- |
| Manager / creator | Full fixed membership, unavailable items, active assignment states and pending review counts | Create fixed list only |
| Annotator | Only packages containing own active assignments; rows/counts restricted to those assignments | Existing Editor link if EDIT is allowed |
| Reviewer | Only packages containing pending, non-self submissions eligible for the existing review queue | Existing review queue if REVIEW is allowed |

Ordinary actors receive no global task denominator, membership digest, source
note, other assignee IDs or answers. Cancelling their last assignment removes
the package from their annotation scope; revoking organization/project access
also rejects stale detail URLs. Reviewer scope retains pending submissions from
cancelled source assignments, matching the existing queue, without Editor access.

Progress is a live projection, not a stored package status. `annotate_assignment_work`
is the same SQL expression used by My Work, comparing each assignee's own valid
draft and highest submission revision. Fixed sample totals and active assignment
totals are separate. Active assignments left after assignee revocation have a
separate blocked count. Pending reviews include retained cancelled sources for
managers/reviewers; they are not interchangeable with approved work. Every read
has a timestamp and an explicit refresh action. There is no automatic repair.

## #86 and deferred work

Reads consume the existing READ_WORK operation. Creation declares the explicit
ORGANIZE_WORK operation and uses `lock_project_operation`; it never disguises
creation as assignment. With the feature disabled (default), the legacy provider
and existing single-task behavior are unchanged. With it enabled, a versioned
legacy grouping policy permits organization, while a lifecycle provider can
deny it independently. Missing/unknown providers fail closed. No state is inferred
from `is_draft` or `is_published`.

#86 owns authoritative project states, transitions and the full operation matrix.
This slice is not #86 completion. Batch dispatch, claim pools, load balancing,
overlap allocation, priority/deadline enforcement and automatic overdue handling
remain unavailable. No dispatch route is registered. Existing single-task
assignment, submit/rework and review authorization remain authoritative.

## Regression coverage

`task_packages/tests/test_packages.py` covers fixed/idempotent creation, negative
scope and actor injection, mapped identity, live revocation, deleted/moved tasks,
canonical draft states, distinct denominators, cancelled review sources, policy
and pagination. PostgreSQL-only tests use independent connections and real row
locks for same-key creation and revocation while a creation waits at the fence.
Existing assignment, Editor, submission, review and My Work tests remain in CI.

The bilingual React workspace reuses ApiProvider, i18n and existing work links.
It clears stale rows on refresh failure, ignores disposed responses, and retries
an out-of-range page once by its specific machine code. Creation retries retain
the request key/body. `fixed-task-packages.cy.ts` covers real browser creation
and manager/annotator/reviewer views in en-US and zh-CN, including no dispatch.

This local candidate is not pushed, deployed or enabled in the retained pilot.
Production, IAM recovery and Core business integration remain out of scope.
