# Package-scoped continuous annotation and review (#83)

Status: isolated local candidate. Base: main 8abaa18f54b296bf7b960a3a1e40c579227806c5. No merge, push, production deployment or batch dispatch is part of this slice.

## User-visible contract

Enter package A from its existing read-only workspace. Continue using the upstream Editor, queue sampler and immutable Submission/review commands. The next task and review list must stay within A even when the same person has work in package B. Refresh keeps the package context. Exhaustion or lost access stops this scoped workflow and offers refresh/back; it must never silently fall back to the project-wide queue. An explicit return to the ordinary project workspace ends the package context.

Use one optional positive package ID, task_package, in generated links and read requests. Context is navigation/filtering, not an entitlement: removing it restores the ordinary authorized project workflow; it never grants assignment, editing or review authority. Existing unscoped callers retain their behavior.

The package's existing single-task links remain single-task Editor links. A separate continuation link enters the existing label stream (`labeling=1`), with the full fixed package as its queue instead of the last project view/filter/selection. The native Back to task list action and explicit project return link end the context after the existing save guard. A review link uses `package_review=1` to reopen the same scoped workspace on refresh. This flag only chooses UI; it grants no authority. Closing that workspace returns to the ordinary project. Scoped task/progress reads use private, no-store responses.

## Authority and reuse

- Resolve the human actor from the authenticated server principal. Require current account, organization, project role and project-operation policy.
- Resolve the fixed package and its live items using the existing original task ID/creation-time predicates. Intersect with existing authorized candidate tasks or pending, non-self Submissions. A client task list, reviewer ID, stale assignment version, filter or saved view cannot widen this result.
- Reuse the upstream get_next_task sampler and existing Task/Assignment/draft guards. Both native next-task API entry points and direct task reads must honor an explicit package context. Bad/cross-project/inaccessible context fails closed; it is not interpreted as absent.
- Reuse ReviewerWorkspace and SubmissionListAPI for a package-only pending queue. Keep the existing project-wide history view separate; do not broaden package visibility to historical content after its last eligible pending row disappears. A successful last decision remains reported as saved; an empty or subsequently unavailable scope stops without selecting another package.
- Reuse native navigation and unsaved-work protection. Carry the context through existing history/query updates; do not introduce a second task index, assignment, claim endpoint or review ledger.

## Boundaries

#80 owns immutable content/media snapshots, ReleaseManifest and release semantics. Package membership freezes IDs only; this slice creates no DatasetVersion or frozen task body.

#86 owns project states/transitions and the operation matrix. This slice consumes READ_WORK / EDIT / REVIEW and denies missing/unknown policies. It adds no lifecycle state, scheduler, deadline or automatic repair.

Managers retain their existing grouping/read authority. Annotators retain only current own assignments. Reviewers retain only currently eligible pending non-self work, including authorized submissions from cancelled source assignments. Package grouping may overlap; it does not deduplicate or allocate assignments.

## Reproduction before repair

Use two explicit synthetic packages in one project, one actor with assignments in both, and a reviewer with pending submissions from both. Record the actual generated Editor URL, subsequent next-task request/result and selected review item. Exhaust A while B remains eligible. Current package entry + native next/queue UI must reproduce a user-visible escape before changing product code.

Only fix demonstrated scope-propagation/enforcement blockers. Test en-US and zh-CN; preserve save/reload/rework semantics. Negative regressions cover changed package/task/project IDs, direct API calls, disabled feature, revoked/reassigned actor, policy denial, unavailable live item, empty A with eligible B, and unchanged ordinary project navigation. Real PostgreSQL tests check live query/authorization behavior; no new write fence is added for read navigation.

## Reproduced root cause and repair

The baseline package Editor URL omitted work context, both native next-task API entry points ignored explicit package scope, and the package review button opened the project-wide pending/history workspace. Five real API assertions failed before repair; a reviewer entering A selected B's newer submission in both display languages. Ordinary project navigation selecting B after explicitly returning to the project is expected behavior, not a package-escape defect. Package continuation did not previously have its own entry.

The candidate extracts the existing live-item/package-visibility predicates into a shared access module, then intersects native queue/task/review queries with them. It repeats current authority after materialization and validates a scoped decision inside the existing Project → Task → Assignment → Submission write fence. A mapped actor is used for scoped sampler completion state; the session manager's state is never borrowed. Package review only requests current pending work, with no project-history fallback. A saved final decision remains successful even if the subsequent read reports that the package is no longer visible.

Further reproduction found two linked continuation failures: project-wide stream history offered foreign package IDs (the Editor received 404 and redirected to the project), and rejected work was still excluded as completed because it already had an Annotation. The history response now narrows the existing actor history without pruning its global record. Explicit-assignment sampling reuses the existing personal-work classification for returned work and changed drafts with submissions, preserving legacy annotation completion. The native next-task serializer resolves the same actor and selects that actor's current working annotation (including skips), so returned work creates a new immutable revision rather than a second working annotation or an unbound draft. The old Submission/review/hash remain unchanged.

## Local execution

Reuse this chat's clean managed worktree and stopped owned PostgreSQL/test/frontend/browser containers, with a fresh synthetic database and temporary source/assets. Preserve the six retained #76 containers, all data volumes, primary dirty checkout and prior sealed reports. Do not use the other executor's fork-host-raw-live checkout. IAM recovery and Core business integration remain deferred.

## Local acceptance (2026-10-22)

- PostgreSQL: 197 selected package, assignment authorization, submission/review, project-operation, personal-work and real write-concurrency regressions passed.
- SQLite: 50 passed; 5 PostgreSQL-specific concurrency cases skipped here and covered on PostgreSQL.
- Frontend: 57 component/store regressions passed; production frontend build passed.
- Fresh Edge contexts: six annotation, review and returned-work journeys passed in en-US and zh-CN, each using two synthetic packages. Single-task link/reload, continuous submit, native previous/next, scoped history, exhaustion with B still eligible, direct out-of-package denial, package review refresh, rejection/approval, same-annotation revision 2 with preserved old hash/review, and successful final decision without project-history fallback passed. Zero page exceptions.

This is local candidate evidence, not GitHub CI or retained-pilot/production installation. No migrations, batch assignment, real #86 lifecycle provider, #80 release/content semantics, IAM recovery or Core business integration were exercised or enabled by this slice. #83 remains open.
