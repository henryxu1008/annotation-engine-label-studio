# PR #98 security review and fixes

Date: 2026-10-21. Reviewed head: `adacd929cdcab7083a347cfc7674fe82b69f080e`.
Final fix source: `5a4661a06977c3b272707b143457064fae5adf08`.
The shared organization-authority fix is in `d0c6748ce9403dc3dc391210a22e23db058fc63f`.
The initial manifest/fence fixes are in `1eb1aa6a0cad517f8b6a4c3b3af5210fadb9bd5d`.
Scope: the isolated #79 worktree; no #76 runtime operations, merge, deployment
or Core integration. The earlier [local acceptance](issue79-local-acceptance.md)
remains the historical receipt for its recorded source, not this follow-up.

## Fixed findings

1. **P1: residual project roles bypassed organization revocation.**
   After organization soft deletion, an active account with a retained project
   Manager role could still GET the legacy single-Submission release. A retained
   Reviewer could POST an approval, which would then be eligible as a source for
   an approved batch. Both real API calls returned 200 after revocation completed.
   `AuthorizationService.project_role` trusted ownership/project-role rows without
   first verifying effective organization membership.

   The shared role resolver now checks the exact project's live organization
   membership and active local account before ownership or a role can grant
   authority. This closes the dependency at its shared source rather than adding
   an independent check to just one old endpoint. Missing organization scope,
   foreign organization membership and cached inactive principals fail closed.
   A valid project membership remains authoritative for background work when the
   user merely switches their selected UI organization. Historical approvals are
   not re-certified or rewritten by this change.

2. **P1: LocalFiles bypassed private artifact and Core-only delivery gates.**
   With LocalFiles enabled and an authorized import root containing the private
   export area, a Manager of another project received 404 from the dedicated
   artifact endpoint but 200 from `/data/local-files/` for the same private bytes.
   Core-only mode did not close that route. Recursive scans and JSON/blob import
   could also expose the files, including through symlink aliases. This requires
   LocalFiles configuration overlapping the private area, not anonymous access.

   The configured private root is now reserved from LocalFiles configuration,
   scans, direct import/blob URI creation and HTTP delivery, regardless of release
   policy. Resolve aliases before the decision and deny before ETag/Range handling.
   Configuration guards apply to import/export storage, including retained
   configurations; parent scans exclude private children. An empty private root
   also fails closed rather than defaulting the producer to its working directory.

3. **P1: lexical storage prefixes granted access to sibling directories.**
   A LocalFiles root named `owned` authorized a request under `owned-other` because
   the SQL prefix match was treated as the final permission boundary. A valid
   project permission on the first storage therefore exposed files outside it.

   SQL now only selects candidates. HTTP access requires real directory
   containment within the document root and an authorized storage root, using
   standard resolved paths. The negative case retains a successful public-file
   read inside `owned`, proving normal storage was not disabled to close the hole.

4. **P2: artifact delivery was not bound to the complete frozen manifest.**
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

5. **P2: revocation could commit after final authorization but before READY.**
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
| Legacy review / single release | Shared live organization authority is now required. Their existing workflow, single-release integrity predicate and independence from the #79 batch-mode switch remain separately governed; this is not an instance-wide delivery prohibition or acceptance of a mixed workflow |
| Generic filesystem routes | Configured private artifacts cannot be served, scanned, reimported or written through LocalFiles, including aliases and a containing import root. Public files retain their existing storage/project checks and require actual directory containment |
| #80 / Host | Host DatasetVersion, cross-system ReleaseManifest, version comparison, media pinning, cross-system revocation/retention and final business publication stay outside #79 |
| Host/Core request fields | Forged `dataset_version_id`, `release_manifest`, `core_review_decision`, actor/reviewer/mode/snapshot fields are rejected; no incoming Host decision synthesizes Fork approval |
| Standard adapter | The actual pinned SDK CoNLL2003 path remains covered. Media stays references-only; other standard formats remain disabled |

Local row locks define the validated local identity path. A future external
identity adapter must supply its own consistency/revocation protocol; this review
does not certify such an adapter or start Core integration. SHA-256 provides
byte integrity, not a signature, author attestation or Host approval.

## Verification

- Before fixes: five artifact/manifest/pointer regressions returned 200 instead
  of 409; three real PostgreSQL revocation races committed before READY; legacy
  review/release API calls returned 200 after organization revocation. Five
  additional clean LocalFiles negative cases reproduced private delivery/import,
  configuration/alias and sibling-prefix failures.
- After fixes: **413 PostgreSQL tests passed**, with zero failures/errors/skips,
  across the full authorization foundation, approved-export and LocalFiles suites.
  This includes **122** batch/composed project/review/LocalFiles cases and **11** identity /
  shared-organization scope cases; these subsets overlap the total, not add to it.
  Native/real CoNLL, worker/retry/idempotency, scope/creator revocation and actual
  publication races remain covered. Twenty-four cases were added in this review.
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

The dedicated workflow also runs the existing LocalFiles view, validation,
serializer, path-normalization and export regressions. Conditional-file tests use
normal Django client iterator consumption, preserving test transaction isolation.

The feature remains default-disabled. #76's existing session, worktrees, six
containers and retained data are outside this worktree's write set. Only the
owned #79 application/PostgreSQL test containers were restarted for these tests;
their data is retained and the containers are stopped after validation.
