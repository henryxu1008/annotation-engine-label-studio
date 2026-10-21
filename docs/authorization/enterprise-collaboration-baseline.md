# Enterprise collaboration baseline

This fork now contains the first secure collaboration baseline built on Label Studio Community Edition.

## Implemented milestones

### Identity foundation
- Local Label Studio users resolve through a stable `Principal` abstraction.
- Identity source is configurable for future platform IAM/OIDC integration.
- Interactive annotation actor fields are server-controlled.
- Client-supplied actor IDs cannot override the authenticated human actor.

### Project membership and RBAC
Project-scoped roles:
- manager
- annotator
- reviewer

Implemented behavior:
- project list/count/detail are membership-scoped;
- project creator is an effective manager for backward compatibility;
- annotator/reviewer can view an assigned project;
- only manager can mutate project settings through Project CRUD;
- direct project-ID changes do not bypass membership scope.

### Task assignment and annotation ownership
- `TaskAssignment` is first-class.
- A task may have multiple assignees.
- Annotators only access tasks with an active assignment.
- Each assignment owns at most one active annotation.
- Shared-task annotators do not receive each other's annotation results.
- Drafts are tied to assignment lifecycle.
- Assignment cancellation invalidates stale writes.
- `assignment_id + assignment_version` provide optimistic concurrency control.
- Manager can inspect assignee results but cannot use generic annotation APIs to edit another user's work.

### Immutable submission and review
- Formal submit creates immutable `Submission` revisions.
- Submission stores a snapshot and SHA-256 hash.
- Draft/autosave does not create a formal revision.
- New pending submission supersedes older pending revision.
- Approved/rejected historical revisions remain immutable.
- Only reviewer role can review.
- Self-review is rejected.
- Rejection requires a reason.
- Reviewer identity/time are server controlled.
- Manager-only release endpoint exposes only approved immutable snapshots.
- Editing and resubmitting after approval produces a new pending revision; approval is never inherited.

## Completed resource and membership surfaces

### Project-related write surface hardening
Implemented in closed #8, with the endpoint matrix and negative-path regressions
maintained in [authorization-plan.md](authorization-plan.md).

The server-side enforcement and regression coverage include:
- Data Manager actions and bulk mutations;
- import/reimport;
- export/download;
- storage configuration/sync;
- project-scoped model/prediction configuration;
- file/media/proxy paths;
- webhook/project configuration endpoints.

Project membership does not grant manager mutation rights on these paths. Every
new or upgraded endpoint still requires its own authorization audit.

### Project member/role management surface
Implemented in closed #9; the browser management surface followed in #17.

The supported manager-only API/UI flow covers:
- adding a member;
- changing role;
- disabling/re-enabling membership;
- removing membership safely;
- preventing loss of the last effective manager;
- validating organization boundaries.

## Remaining delivery gates

### Platform IAM integration
Not started.

Current source of identity remains Label Studio local users.

Future integration should add a platform-backed `IdentityProvider` and preserve the existing:
- Principal;
- project membership;
- task assignment;
- submission;
- review semantics.

### Production validation
The current Fork PR Gate verifies:
- Django migration drift;
- identity/actor spoofing;
- project authorization;
- task assignment and annotation ownership;
- stale assignment tokens;
- immutable submissions and review;
- existing task API smoke tests.

The endpoint matrix, storage/media, import/export/bulk and browser collaboration
regressions are implemented; see [browser-e2e-acceptance.md](browser-e2e-acceptance.md).
Source CI and historical local fixtures do not establish a new candidate's or
target environment's acceptance. Before production use, verify the fixed
candidate, PostgreSQL migration and safe fallback, HTTPS/cookies, cross-replica
revocation, audit durability, backup/restore and the actual cleanup schedule in
the receiving environment (#76/#48/#44). An upstream upgrade also requires its
own security and regression review.

## Current reference workflow

```text
Manager
  -> create/manage project
  -> add/manage project roles (manager-only API and UI)
  -> assign tasks

Annotator
  -> open only assigned task
  -> save draft
  -> submit annotation
  -> immutable Submission revision created

Reviewer
  -> inspect pending Submission
  -> approve or reject exact revision

Manager
  -> release approved immutable snapshot
```

This baseline intentionally does not attempt full Label Studio Enterprise parity.
