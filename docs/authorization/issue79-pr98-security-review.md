# PR #98 security review and fixes

Date: 2026-10-21. Reviewed head: `adacd929cdcab7083a347cfc7674fe82b69f080e`.
Fix source: `1eb1aa6a0cad517f8b6a4c3b3af5210fadb9bd5d`.
Scope: the isolated #79 worktree; no #76 runtime operations, merge, deployment
or Core integration. The earlier [local acceptance](issue79-local-acceptance.md)
remains the historical receipt for its recorded source, not this follow-up.

## Fixed findings

1. **P2: artifact delivery was not bound to the complete frozen manifest.**
   Download verified the opened file against mutable artifact-row size/hash,
   independently of the sealed manifest. Restore/maintenance drift could replace
   both file and row metadata, switch private pointers/attempts, or swap a valid
   manifest from another receipt while download still returned 200. A missing
   artifact was also absent from manifest validation. These cases require
   server-side evidence/storage drift; the review did not find an HTTP API that
   lets a client write artifact metadata.

   `sealed_artifacts` now verifies the canonical frozen input and input digest,
   receipt identity, complete artifact inventory and metadata, winning READY
   attempt, and exact release/attempt private keys. Detail, manifest/content and
   download share this gate. Download uses the validated object snapshot for
   its opened-file size/hash check. Drift quarantines the receipt with no silent
   regeneration or retry under the same artifact identity.

2. **P2: revocation could commit after final authorization but before READY.**
   Real PostgreSQL connections reproduced project-member API revocation,
   account disablement and organization soft deletion completing in that window.
   The worker then published READY. Subsequent downloads already denied the
   revoked creator; the defect was the publication/authorization ordering.

   Freeze and final publication now hold the Project fence and creator authority
   rows through commit. Organization membership is locked before User, matching
   organization soft deletion; account disablement and role maintenance serialize
   on their rows. Publication either commits before the concurrent revocation or
   observes the revoked authority and fails closed. The requester and original
   creator are still checked on each content/download request, including HEAD
   and resumed Range. Already delivered bytes cannot be recalled.

## Boundaries checked

| Boundary | Review result |
| --- | --- |
| Actor/project/object authority | Trusted server identity, active organization membership, effective project Manager, nested receipt/artifact scope; no reviewer/annotator/unscoped-superuser release grant |
| Source of truth | Exact approved Submission/revision/hash and actual ReviewDecision; native output retains frozen records, independent of mutable Annotation/Task data |
| Freeze authorization | Historical decision copied transactionally into the frozen input; it grants no future access. Live authority is required again for job/publication/download |
| Core-only-review | All new #79 batch routes fail closed, including existing receipt links and an in-flight switch before publication; authorized ordinary Submission reads remain available |
| Legacy single release | Existing `/api/submissions/{id}/release/` remains independently governed. This is not an instance-wide prohibition of every historical release endpoint or acceptance of a mixed workflow |
| #80 / Host | Host DatasetVersion, cross-system ReleaseManifest, version comparison, media pinning, cross-system revocation/retention and final business publication stay outside #79 |
| Host/Core request fields | Forged `dataset_version_id`, `release_manifest`, `core_review_decision`, actor/reviewer/mode/snapshot fields are rejected; no incoming Host decision synthesizes Fork approval |
| Standard adapter | The actual pinned SDK CoNLL2003 path remains covered. Media stays references-only; other standard formats remain disabled |

Local row locks define the validated local identity path. A future external
identity adapter must supply its own consistency/revocation protocol; this review
does not certify such an adapter or start Core integration. SHA-256 provides
byte integrity, not a signature, author attestation or Host approval.

## Verification

- Before fixes: five artifact/manifest/pointer regressions returned 200 instead
  of 409. Three real PostgreSQL revocation races committed before READY.
- After fixes: **93 PostgreSQL tests passed**, with zero failures/errors: the
  original 83 tests plus ten review cases. This includes native/real CoNLL,
  worker/retry/idempotency, scope and creator revocation, and publication races.
- Python F/E9 checks, migration drift and `git diff --check` passed. No schema
  migration is added by these fixes.
- Standalone transaction-test selections preload the application's URLconf so
  upstream lazy storage models are registered before PostgreSQL flush. This
  fixes fixture teardown parity with application startup, without relaxing
  database constraints or excluding regressions.
- Detailed local XML and harness evidence are retained under
  `C:\Users\Henry\.codex\reports\ls-issue79-20261021\review98-*`.
- Exact-head GitHub results are recorded in [PR #98 checks](https://github.com/qq550723504/annotation-engine-label-studio/pull/98/checks).
  They are separate from local test receipts and merge-SHA push/main CI.

The feature remains default-disabled. #76's existing session, worktrees, six
containers and retained data are outside this worktree's write set. Only the
owned #79 application/PostgreSQL test containers were restarted for these tests;
their data is retained and the containers are stopped after validation.
