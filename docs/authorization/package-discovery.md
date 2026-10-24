# Package summaries, state lookup and selected-package My Work (#83)

This slice extends the existing fixed-package/read-only work projection. It
adds no model, migration, assignment command, persisted progress, allocator or
review decision. #83 stays open; dispatch, claims, deadlines, #86 lifecycle,
#80 content/release identity, IAM recovery and Core integration stay outside it.

## Smallest user flow

1. The existing package list shows a role-scoped work summary and its read time.
   Creation stays in the existing flow. Lists never sum overlapping packages.
2. A package's existing detail supports a server-side state filter before
   pagination. Counts still describe the full authorized package; the paginated
   result count describes the filtered view. Manager rows are samples, annotator
   rows are own active assignments, and reviewer rows are eligible pending
   non-self Submissions. These are different units, not completion percentages.
3. My Work reuses that paginated package list as a read-only picker, including
   its summary and permission checks. Selection is explicit, never inferred
   from the first package containing a task. My Work remains personal even for a
   manager. Selecting/clearing a package resets the work page/filter; refresh,
   pagination and language changes preserve the current selection and filter.
4. Selected-package Editor links carry the existing task_package context.
   Reviewers use the existing package_review entry. An annotator can continue
   the existing whole-package queue when no state filter is active. A state
   filter locates single work items; it does not create a new sampling algorithm.
   Clear the filter to resume the whole package. Returning from package detail
   preserves the package-list page. Native navigation retains unsaved-work guards.

## Scope and counts

Manager summaries reuse fixed/live sample counts, active-assignment work states,
unassigned/unavailable samples, pending review and revoked-assignee counts.
Annotator summaries expose only that actor's current active assignments.
Reviewer summaries expose only current pending non-self work, including the
existing cancelled-source semantics. No answer, task body or other actor identity
is added. Package list aggregation is batched, rather than one detail request
per package.

A fixed task may belong to both A and B. Each package counts its membership
independently; there is no cross-package grand total. Inside a package, a task
is counted once in sample totals; multiple active assignments remain separate.
My Work intersects its existing authorized query with selected package IDs using
a subquery, so overlapping membership never duplicates assignments/submissions.
Its unscoped personal counts remain unchanged.

Detail accepts one optional state: existing WORK_STATES for managers/annotators,
plus unassigned, unavailable and pending_review for managers; reviewers may use
pending_review only. Invalid/repeated/inapplicable filters reject. A manager
state view matches samples with at least one matching assignment; its row is
still a sample, not a second assignment list. Full counts never shrink to the
current page or selected state.

## Enforcement and recovery

All paths reuse trusted actor resolution, visible_packages, live_items,
annotate_assignment_work, PackageScope and the existing READ_WORK/EDIT/REVIEW
policy. Package context narrows an existing entitlement. Changed IDs, a disabled
feature, lost role/organization/package scope or unknown policy fail closed.
Explicit invalid scope never falls back to project work. Scoped My Work also
rechecks materialized assignment ID/version/task or pending ID/revision/task
before returning and refreshes operation flags. Reads acquire no project write
fence and never repair or reassign work.

Stale async responses cannot replace another project/package/filter. Failed
reads clear old rows/actions; read-only retry retains the requested scope.
Only the existing pagination machine code may recover page 1, preserving state
and package. A denied scope is an error, not an empty page or a project fallback.

## Acceptance

Use two overlapping synthetic packages, separate annotator/reviewer identities,
mixed states and more than one page. Verify role-specific list/detail agreement,
no double counting, filter-before-pagination, selected-package My Work links,
whole-package continuation, scoped review, language switches, read failure and
retry, stale responses, ID swapping, revocation/reassignment and READ_WORK-only
policy. Preserve existing grouping/assignment/review regressions and retained
containers. Real en-US/zh-CN browser evidence is distinct from component tests.
Only actual reproduced blockers are repaired. No push, merge or deployment is
part of this local candidate.
