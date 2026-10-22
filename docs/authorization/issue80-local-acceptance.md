# #80 first-version local acceptance — 2026-10-22

Status: **LOCAL_PASS / NOT_PUSHED / NOT_MERGED / NOT_DEPLOYED / CORE_NOT_RUN**.
Base: `c2758fed05665c1258e5cf50ea63db809eaca9b5` (merged #79 / PR #98).
Branch: `codex/issue80-release-snapshots`.
Worktree: `C:\Users\Henry\.codex\worktrees\ls-issue80-snapshots\annotation-engine-label-studio`.

The user confirmed the [first-version design and boundary](release-manifest-v1.md).
This receipt covers Fork snapshots and synthetic Host handoff only. No real Host
source admission, DatasetVersion creation, Core review, integration, Gold
certification or business publication was performed.

## Results

| Check | Result |
| --- | --- |
| Broad backend safety/regression suite | 457 passed, 0 failed/error/skipped; main process uses dedicated PostgreSQL 17 |
| Final Host synthetic contract suite | 8 passed, including one additional actual CoNLL handoff case; seven overlap the broad suite |
| Unique backend cases across those runs | 458; 42 are new #80 cases |
| Frontend unit regressions | 69 passed across 12 suites, including four new snapshot/revocation/stale-body cases |
| Real Edge browser, en-US/zh-CN | 8/8 passed on final runtime; text/replay/diff/history, actual media download hash, missing media and stale-page revocation in each language |
| Frontend production build | PASS; upstream build warnings retained |
| i18n catalogs and migrated literals | PASS; en-US/zh-CN catalogs and 99 scoped files; literal-scanner self-tests passed |
| New Python files / patch | Ruff PASS; `git diff --check` PASS |
| Database migration checks | Forward applied in fresh PostgreSQL test databases; no model drift; empty guard reverse/reinstall passes; retained snapshot rollback and direct READY/tamper/delete denied |
| Dependency checks | Poetry lock check and `pip check` pass; new dependencies limited to JCS and explicit existing JSON Schema dependency |
| Package schema inclusion | Built wheel contains `release-manifest-v1.schema.json`, byte-identical to source; this is a packaging check, not a deployment/distribution receipt |
| GitHub PR/main CI | NOT_RUN — branch was not pushed and no #80 PR was created |

The broad suite also runs the existing session-worker tests, whose subprocess
fixture deliberately uses its own disposable SQLite database. This does not
replace the real PostgreSQL tests for #80 authorization, persistence, concurrency
or freeze guards. No #76 database is used by either path.

## Safety and completeness covered

Cross-project and nested ID substitution; direct API/HEAD/OPTIONS/Range access;
reviewer/annotator/unscoped admin denial; account and creator revocation; both
creator authority fences at finalization; forbidden actor/reviewer/mode/Host/Core
fields; Core-only batch denial with ordinary Submission reads preserved; same-key
replay/conflict and two-connection races; duplicate worker claim; queue failure
and interrupted capture fencing; DB-level immutable header/manifest/source binding;
missing audit receipt and audit failure; native hash corruption; exact diff of
add/remove/revision/schema and same-source conflict; deterministic content versus
envelope identity, UTC filters and large IDs; private media copy/hash/source loss,
foreign/missing/remote/traversal/symlink/URL-text/oversize/changed media; private-copy
corruption/quarantine; verified ranges and If-Range; source/mapping replay, lost
Host response/lookup, namespace conflict, current authority, Host-copy corruption,
capture-time provenance rejection and real locked SDK CoNLL2003 artifact handoff.

Host receiver tests are a fixture under `data_export/tests/`, not production Host
implementation. Their receipt explicitly reports `ACCEPTED_SOURCE`, not Gold or
publication. Core-only-review never receives or synthesizes Fork approval.

## Reproduction and artifacts

The existing `.github/workflows/approved-dataset-export.yml` now includes the new
PostgreSQL suites and the new `cypress.release-snapshot.config.ts` bilingual spec,
alongside the existing #79 spec. A fresh disposable DB is seeded with
`seed_release_snapshot_e2e`; test-only credentials are ignored by Git. The seed
refuses an existing cohort rather than deleting or reusing it.

Complete small synthetic examples are retained with detached checksums:

* [Text Manifest](issue80-local-acceptance/text/manifest.json),
  [native JSONL](issue80-local-acceptance/text/records.jsonl),
  [checksums](issue80-local-acceptance/text/SHA256SUMS).
* [Media Manifest](issue80-local-acceptance/media/manifest.json), native JSON/JSONL
  and the exact pinned media bytes under that directory;
  [checksums](issue80-local-acceptance/media/SHA256SUMS).
* [English diff](issue80-local-acceptance/en-US-release-snapshot-diff.png),
  [Chinese diff](issue80-local-acceptance/zh-CN-release-snapshot-diff.png),
  [English media](issue80-local-acceptance/en-US-release-snapshot-media.png),
  [Chinese media](issue80-local-acceptance/zh-CN-release-snapshot-media.png).

Manifest artifact IDs map to native filenames; media blob IDs map to the
`media-<blob_id>.bin` filenames. These use synthetic namespace
`00000000-0000-4000-8000-000000000080`; never provision a real connection with it.
`content.output.resource_mode=references_only` describes the original #79 native
files; the independent #80 `media_profile`/media inventory supplies captured bytes.
Neither the native references nor this capture-time profile prove submission-time
media identity. All example author/principal/review facts are synthetic.

Machine reports and full logs are retained outside Git at
`C:\Users\Henry\.codex\reports\ls-issue80-20261022`, notably
`postgres-final.xml`, `host-final.xml`, `frontend-unit-green.log`,
`e2e-final-*.xml`, `e2e-final.log` and `summary.json`.

## Isolation and remaining boundary

All runtime work used the newly created `ls-issue80-20261022` network and three
`ls-issue80-*-20261022` containers. Test source and reports are the only bind mounts;
database/files/Redis are separate from #76 and #79. An existing immutable #79 test
image and this chat's inactive #79 frontend dependency cache were reused; no #76
image, source mount, browser profile, volume, process, session or credential was
modified. At completion only #80 containers are stopped and retained, without
deleting data or running broad cleanup.

Current authority remains required to access historical bytes. Source revocation
does not rewrite the Manifest or recall an already delivered Host copy. Historical
#79 source integrity gates are preserved; deleting required Submission evidence
can block Fork content access even though private snapshot bytes are retained.
Unavailable queues or revoked creators may require governed operator resolution;
no automatic reassignment, unsafe recapture, orphan deletion or retention engine
was added. Real Host binding/admission gaps, stronger submission-time media
provenance and live cross-system revocation remain independent work.
