# Browser E2E acceptance — current enterprise UI

Issue: #16

This document records the enterprise collaboration browser acceptance contract. Focused workflows #17–#20 are merged; #21 adds the single end-to-end browser journey that composes them.

## Test environment

The browser suite runs against a real Django + Label Studio frontend stack:

- Django: `http://localhost:8080`
- frontend HMR: `http://localhost:8010`
- Cypress: Electron, headless in CI
- deterministic local users:
  - Manager
  - Annotator A
  - Annotator B
  - Reviewer
- deterministic project with two task assignments

Fixture command:

```bash
poetry run python label_studio/manage.py migrate --noinput
poetry run python label_studio/manage.py collectstatic --noinput
poetry run python label_studio/manage.py seed_enterprise_e2e
```

Browser command used by CI:

```bash
cd web
yarn cypress run \
  --project apps/labelstudio-e2e \
  --config-file cypress.config.ts \
  --config baseUrl=http://localhost:8080,video=true \
  --spec apps/labelstudio-e2e/src/e2e/enterprise-current-ui.cy.ts
```

## Current UI acceptance matrix

| Scenario | Status | Evidence / notes |
|---|---|---|
| Four distinct identities can authenticate with isolated sessions | PASS | Browser uses the real `/user/login/` form for each role. |
| Annotator task scope is enforced by the backend from the browser session | PASS | From a real logged-in browser session, assigned task returns 200 and another annotator's task returns 404. |
| Reviewer project visibility does not grant labeling/task access | PASS | Reviewer session cannot retrieve assigned annotator task; Browser E2E passed this scenario. |
| Existing editor explicit Submit creates immutable Submission | PASS | #21 Enterprise Browser E2E run #141 passed the real Editor Submit/Update path with assignment id/version plus `submit_for_review=true`. |
| Membership revocation invalidates backend access | PASS | A logged-in annotator loses task API access immediately after out-of-band membership disablement. |
| Stale already-open editor Save/Submit after revocation | PASS | #21 Enterprise Browser E2E run #141 passed with a confirmed-writable real Annotator editor kept open across out-of-band revocation; stale Submit was required to fail with controlled 403/404/409 and never 500. |
| Project member/role management through browser UI | PASS | #17 merged after focused browser CI passed. |
| Task assignment administration through browser UI | PASS | #18 merged after focused browser CI passed, including stale assignment-token coverage. |
| Reviewer pending queue / immutable snapshot review UI | PASS | #19 merged after focused browser CI passed against immutable Submission snapshot/revision/hash. |
| Approve / Reject browser controls | PASS | #19 merged after focused reject/revise/approve browser CI passed. |
| Submission revision history UI | PASS | #20 merged after focused browser CI passed for immutable multi-revision history. |
| Manager approved-revision Release UI | PASS | #20 merged after focused browser CI passed with exact submission/revision/hash/snapshot validation. |
| Full browser Manager → Assign → Annotate → Reject → Revise → Approve → Release flow | PASS | #21 Enterprise Browser E2E run #141 passed the composed real-UI journey on commit `b1b342407e4b675183359ac0e6313fcb83ee1969`. |

## Findings during E2E bring-up

### Test-infrastructure findings

The initial E2E harness exposed several CI/runtime assumptions that were not part of the authorization product contract:

1. The Nx Cypress preset required the project root to be explicit when invoking Cypress directly.
2. The Cypress support/spec paths must be resolved relative to `apps/labelstudio-e2e`.
3. Django static assets must be collected before the real browser page is loaded; without `collectstatic`, `jquery.min.js` was unavailable and the base page failed before product assertions.
4. Failure screenshots and video are emitted below `web/dist/cypress/apps/labelstudio-e2e`.

These were fixed in the E2E workflow rather than suppressing browser exceptions.

### UI contract findings

Browser execution confirmed a new current-UI gap:

- the annotator can authenticate and the assigned Task API returns 200;
- another annotator's Task API returns 404;
- the project Data Manager route loads successfully;
- the same authenticated browser session can access its assigned Task API and is denied another annotator's Task API;
- project-level labeling entry visibility can vary with seeded task state and is not itself an authorization boundary; protected task access and editor writes remain server-authoritative.

Therefore #16 does not implement or synthesize a labeling entry UI. The missing projection/navigation is a product UI gap and belongs with the task-assignment UI work in #18.


The Data Manager explorer does not guarantee that raw `task.data.text` is rendered as visible table text in the default view. Therefore task isolation is validated from the actual Data Manager task store plus the authenticated API response, not by assuming a particular default column configuration.

The supported programmatic action used by the current UI to open a task is Data Manager's `startLabeling(item)`; a bare `?task=` URL is primarily a history/state restoration mechanism. Current acceptance therefore asserts protected API/editor state rather than the presence or absence of a project-level labeling-entry button.

## Milestone status

Focused browser workflows #17–#20 are implemented, merged, and passed their dedicated CI acceptance. #21 does not add another business workflow; it proves that the same product surfaces compose correctly in one browser journey, including explicit Editor Submit/Update and stale-open-editor revocation behavior.

## Project member management UI (#17)

Route:

```text
/projects/{project_id}/settings/members
```

The Members settings entry is exposed only after the existing project-members endpoint confirms manager capability. The backend remains authoritative; direct non-manager access receives 403 and the UI returns to project Settings.

Focused browser command:

```bash
cd web
yarn cypress run \
  --project apps/labelstudio-e2e \
  --config-file cypress.config.ts \
  --config baseUrl=http://localhost:8080,video=true \
  --spec apps/labelstudio-e2e/src/e2e/project-members.cy.ts
```

The browser coverage exercises add, role change, disable/re-enable, remove, non-manager denial, and real backend validation rendering.

## Task assignment management UI (#18)

The Data Manager exposes a Manager-only `Assignments` control when a concrete task is open. The UI does not own assignment lifecycle state: it uses the existing `TaskAssignment` create/delete APIs, displays active server state, and re-fetches after every mutation.

Eligible assignees come from a manager-authorized backend helper and are limited to active users with non-deleted organization membership and an effective labeling role for the project. Direct POST validation enforces the same identity and membership constraints.

Focused browser command:

```bash
cd web
yarn cypress run \
  --project apps/labelstudio-e2e \
  --config-file cypress.config.ts \
  --config baseUrl=http://localhost:8080,video=true \
  --spec apps/labelstudio-e2e/src/e2e/task-assignments.cy.ts
```

Browser coverage exercises Manager assign, stale-session invalidation after same-user reassignment, cancel/reassign to another annotator, reviewer exclusion, and non-manager absence of mutation controls.


## Reviewer workspace UI (#19)

The Data Manager exposes a project-level `Reviews` control only after the server-authoritative reviewable queue confirms Reviewer role for the project.

The workspace:

- lists pending reviewable Submission revisions;
- displays the exact immutable `Submission.result_snapshot`, revision and result hash;
- shows submitter identity and submitted timestamp;
- exposes Approve and Reject only for pending reviewable revisions;
- requires a rejection reason;
- keeps reviewed revisions visible as immutable history while later revisions appear separately;
- never loads the current mutable Annotation as the review subject.

Focused browser command:

```bash
cd web
yarn cypress run \
  --project apps/labelstudio-e2e \
  --config-file cypress.config.ts \
  --config baseUrl=http://localhost:8080,video=true \
  --spec apps/labelstudio-e2e/src/e2e/reviewer-workspace.cy.ts
```

Browser coverage exercises revision 1 rejection, required reject reason, immutable rejected-history visibility, deterministic creation of revision 2, revision/hash separation, revision 2 approval, Reviewer annotation-edit denial, and Annotator review denial.


## Submission history and approved release UI (#20)

The Data Manager exposes a Manager-only `Releases` workspace using the existing project-manager capability boundary.

The workspace:

- paginates project Submission revisions;
- displays revision, status, submitter, review metadata, immutable result hash, and exact `Submission.result_snapshot`;
- preserves rejected, approved, pending, and superseded revisions as distinct history rows;
- exposes Release only for `approved` revisions;
- validates that the release response refers to the selected submission id, revision, and result hash;
- never releases the current mutable Annotation as a shortcut.

Focused browser command:

```bash
cd web
yarn cypress run \
  --project apps/labelstudio-e2e \
  --config-file cypress.config.ts \
  --config baseUrl=http://localhost:8080,video=true \
  --spec apps/labelstudio-e2e/src/e2e/submission-release.cy.ts
```

Browser coverage exercises rejected revision 1, approved revision 2, pending revision 3, Manager-only release visibility, rejected/pending backend rejection, approved release success, and exact revision/hash/snapshot correspondence.


## Full enterprise collaboration browser flow (#21)

The full-flow fixture uses a second isolated project in the same organization. It starts with only the Manager as a project member and two unassigned tasks, so the browser must perform the collaboration setup rather than inheriting the focused test fixtures.

The browser journey:

1. Manager adds Annotator A, Annotator B, and Reviewer through Settings → Members.
2. Manager assigns Task A and Task B through the Data Manager assignment UI.
3. Annotator task isolation and Reviewer labeling denial are checked from authenticated browser sessions.
4. Annotator A edits the real Label Studio Editor; autosave creates a draft while Submission history remains empty.
5. Explicit Editor Submit creates immutable revision 1 using the current assignment id/version and `submit_for_review=true`.
6. Reviewer rejects revision 1 with a reason.
7. Annotator A reads the rejection reason in Submission history, reopens the real Editor, changes the result, and Editor Update creates revision 2. This reuses the server-scoped submission list: annotators see their own history without reviewer or release actions; the release view requires an explicit manager capability.
8. Revision 1 remains rejected and immutable; revision 2 has a distinct hash/snapshot.
9. Reviewer approves revision 2.
10. Manager opens Releases and releases exactly approved revision 2.
11. Manager UI Disable/Enable is exercised; then a real Annotator editor is kept open while the project membership and assignment are revoked concurrently. A stale Submit/Update must fail with 403/404/409 and never 500.
12. Annotator and Reviewer sessions are denied Manager-only member, assignment, Export, Storage, Webhook, and Data Manager write surfaces.

Focused command:

```bash
cd web
yarn cypress run \
  --project apps/labelstudio-e2e \
  --config-file cypress.config.ts \
  --config baseUrl=http://localhost:8080,video=true \
  --spec apps/labelstudio-e2e/src/e2e/full-enterprise-workflow.cy.ts
```

The project-scoped fixture mutation used during the stale-open-editor step represents the concurrent Manager actor while preserving the Annotator's already-rendered editor page. The same Manager membership disable/enable path is also exercised through the product UI in the same flow; the security boundary remains the server-side rejection of the stale editor write.

## CI

The repository now contains a separate `Enterprise Browser E2E` workflow.

It is intentionally separate from `Fork PR Gate` so browser dependency installation and frontend compilation do not slow the lightweight authorization/API gate.

On failure, CI uploads:

- Cypress screenshots;
- Cypress video;
- Django server log;
- frontend HMR log.

## Completion rule

Only executed browser scenarios may be marked PASS.

GAP means that the backend contract exists but the corresponding product UI is not yet implemented.

The full workflow is browser-complete only when the #21 full-flow spec passes in CI. On commit `b1b342407e4b675183359ac0e6313fcb83ee1969`, Enterprise Browser E2E run #141 passed; Fork PR Gate run #173 also passed. Focused #17–#20 PASS rows remain supporting evidence for the composed journey.


## Historical #16 CI result

The following result belongs only to the earlier #16 current-UI milestone. It does **not** prove the composed #21 enterprise workflow:

- Fork PR Gate: PASS
- Enterprise Browser E2E: PASS
- PR: #22
- Merge commit: `7f99a7dad88340c148a3dae0f2190683f548dd00`

#16 is complete as a historical focused milestone. Its former GAP items were subsequently implemented in #17–#20. The composed #21 enterprise collaboration flow subsequently passed Enterprise Browser E2E run #141 on commit `b1b342407e4b675183359ac0e6313fcb83ee1969`.
