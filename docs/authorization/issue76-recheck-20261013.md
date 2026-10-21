Historical receipt for the superseded 89c794e1 artifact. See the [active fixed candidate](issue76-recheck-20261013-latest.md).

# Issue #76 LS candidate recheck, 2026-10-13

This is the active local candidate for [Issue #76](https://github.com/qq550723504/annotation-engine-label-studio/issues/76) and [draft PR #77](https://github.com/qq550723504/annotation-engine-label-studio/pull/77). The [2026-10-09 receipt](issue76-release-candidate.md) remains historical evidence for its different image. IAM operations/recovery and Core business integration remain paused. No merge, production deployment, new Compose project or certificate-trust change occurred.

## Reproduced user blockers

The optional login tips timeout detached `AbortController.abort` from its receiver. When tips took longer than five seconds, both login languages raised `Illegal invocation`. A bound callback now aborts the request safely. The bilingual native regression checks the real aborted signal, retained email input and successful password login. Both cases failed before the fix and pass in the current artifact.

Data Manager initialization awaited an ML-backend response after its React page had been unmounted, then accessed a null DOM root. A delayed-response navigation probe reproduced `Cannot read properties of null (reading 'dataset')` on the intermediate image. The initializer now rejects absent/already-initialized roots, and the page effect cancels stale continuations after dependency/backend loading. Workspace construction is synchronous after the final lifecycle check. Both language probes pass on the current artifact. Server authorization and schemas are unchanged.

The recheck also corrected test premises: authenticated account actions wait for the rendered actor; slow-import language changes wait for the real persisted PATCH; Home-dialog geometry accepts either `none` or an identity matrix and uses native scrolling at 200% CSS scale; export actions wait for the parent workspace. No application exceptions are suppressed. Failure attempts and their logs remain in the local report. Fixture management commands use their actual positional arguments and exact new actor/task/project IDs.

## Fixed artifact and acceptance

| Field | Current value |
|---|---|
| Runtime source | `89c794e1b1f4ac18e1daf81d04637de7fdccb54a` |
| Test source HEAD | `6e8a7679b91fc72f5e6640054a9e4374e7312aa6` |
| Version | `1.23.0+fork.rc76.89c794e1` |
| OCI index | `sha256:5c971369049009227a0e3b854f7cbb1c5ffdf536cea3cb01a43f0078f928228f` |
| Platform manifest | `sha256:0b6865e45f4d3c764c5f6854a2712b7834f3b2bfb85ebc27bee72ce891cf94c8` |
| Image config | `sha256:84ee35d8971f6d7a08af329095013c501933506351f2527071868924ad21b699` |
| Offline archive SHA-256 | `a6b7f657a57030bd3f00c6a96f726d10e12904bbc6ea080389899e64a130ca34` |

The [canonical build inputs](issue76-recheck-20261013/build-inputs.json) pin Debian/Python 3.11/Node 20 and verify Git objects, links and executable bits. [Package identity](issue76-recheck-20261013/candidate.json), [OCI verification](issue76-recheck-20261013/offline-verification.json), negative integrity probes and a local archive load all passed. Later commits change tests/evidence only; they do not relabel the runtime source as the later PR HEAD.

The [native browser receipt](issue76-recheck-20261013/browser-summary.json) records **44/44** in eight suites, with no failed, pending or skipped cases: login timeout 2, slow project/import and cancelled initialization 4, application locale 24, editor locale 2, export locale 6, collaboration locale 4, and one full journey for each language. Full journeys cover project creation, real JSON import/readback, independent members/roles, assignment, draft, submit, reject/revise/approve/release/history, re-login, stale-page revocation and reassignment. Export display/format-ID/byte checks intentionally intercept the synthetic format list and download response; they do not qualify every converter or external storage service.

These flows use the native production assets on loopback HTTP, PostgreSQL and new synthetic identities. They do not establish Edge/trusted-browser HTTPS acceptance. The [postflight](issue76-recheck-20261013/postflight.json) verifies both replicas' backend/three frontend identities and the served bundle, six healthy services, the same database container, 161 original rows unchanged and 57 old evidence files unchanged. All temporary accounts created by this run were disabled through the existing account-security service and their sessions/password files cleaned up; original identities and test data are retained.

PR #77 had no review threads or formal reviews at preflight. Its prior HEAD's two checks passed, but those results do not qualify this follow-up. Current-head CI is the live PR check record; formal external review remains outstanding. The old 112 operational assertions and seven upgrade checks belong to the historical image and are not inherited here. Target-environment HTTPS, receiving environment, full second daily cleanup interval and an authorized old security/schema rollback target remain separate gates. Issue #76 remains open.

## Retained local trial

- Browser: [local projects](http://localhost:19680/projects).
- Existing project: `annotation-engine-issue76-20261009-finalized`; database and named volumes retained, with fresh private logical backups before each image replacement.
- Existing Compose file: `C:\Users\Henry\.codex\deployments\annotation-engine-issue76-20261009\finalized\compose.rc.yml`.
- Current restricted interpolation file: `C:\Users\Henry\.codex\private\ls-issue76-recheck-20261013\runtime-89c794e1.env`. The historical interpolation file remains intact; use this current file when recreating the serving image.
- Offline package: `C:\Users\Henry\.codex\deployments\annotation-engine-issue76-20261013-89c794e1\offline`.
- Full local report: `C:\Users\Henry\.codex\reports\ls-issue76-recheck-20261013`. Native test sources are in this PR; local execution/generation hashes bind the scoped runner to those sources.

Stop/start only the existing project with its current interpolation file. Retain volumes; do not run `down -v`, prune, regenerate trust, restore a database or enable IAM recovery as part of this handoff. The first image-update readback encountered Docker Desktop's alternate Windows bind-path representation; the checker now resolves only the two existing expected folders and still requires exact named-volume identity.

## CI concurrency correction

The first follow-up PR HEAD `b53707d89` passed authorization CI and the existing,
locale and full collaboration browser phases, but its final import phase failed.
The retained CI log/screenshot shows SQLite `database is locked` and
`SessionInterrupted`, plus an import-location assertion while the final commit
was still pending. The candidate itself uses PostgreSQL. Browser integration CI
now uses an ephemeral PostgreSQL 17 Actions service, matching that backend;
SQLite authorization foundation coverage remains in Fork PR Gate. This adds no
local Compose project or runtime service.

The import regression now waits for the rendered completed upload row and the
actual `reimport` 201 before asserting navigation/readback. The same fixed native
image passed [all four strengthened cases](issue76-recheck-20261013/browser-focused-summary.json)
at test HEAD `9fdda5c8ed97ad57098e9033411f52764eac41df`. The other 40 qualified
native cases have unchanged source hashes; their existing receipt remains bound
to the same image. Both receipts are retained, with the [CI correction record](issue76-recheck-20261013/ci-correction.json).
The seven additional synthetic identities and their temporary credentials were
also retired. Final current-head CI remains the separate live PR record.
