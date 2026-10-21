# Issue #76 LS candidate recheck, 2026-10-13

This is the active local candidate for [Issue #76](https://github.com/qq550723504/annotation-engine-label-studio/issues/76) and [draft PR #77](https://github.com/qq550723504/annotation-engine-label-studio/pull/77). The [2026-10-09 receipt](issue76-release-candidate.md) remains historical evidence for its different image. IAM operations/recovery and Core business integration remain paused. No merge, production deployment, new Compose project or certificate-trust change occurred.

## Reproduced user blockers

The optional login tips timeout detached `AbortController.abort` from its receiver. When tips took longer than five seconds, both login languages raised `Illegal invocation`. A bound callback now aborts the request safely. The bilingual native regression checks the real aborted signal, retained email input and successful password login. Both cases failed before the fix and pass in the current artifact.

Data Manager initialization awaited an ML-backend response after its React page had been unmounted, then accessed a null DOM root. A delayed-response navigation probe reproduced `Cannot read properties of null (reading 'dataset')` on the intermediate image. The initializer now rejects absent/already-initialized roots, and the page effect cancels stale continuations after dependency/backend loading. Workspace construction is synchronous after the final lifecycle check. Both language probes pass on the current artifact. Server authorization and schemas are unchanged.

The recheck also corrected test premises: authenticated account actions wait for the rendered actor; slow-import, export and legacy-reviewer language changes wait for the real persisted PATCH and readback; Home-dialog geometry accepts either `none` or an identity matrix and uses native scrolling at 200% CSS scale; export actions wait for the parent workspace. No application exceptions are suppressed. Failure attempts and their logs remain in the local report. Fixture management commands use their actual positional arguments and exact new actor/task/project IDs.

The collection debounce also ran queued loads after their workspace was destroyed. The callback and collection fetch now use the existing MST lifetime checks before accessing the SDK and after responses. Real bilingual navigation probes and five collection unit cases cover disposal and normal loading. A later tab reload could finish after disposal and still notify the removed SDK; tab selection/reload/save continuations and the enclosing tab initializer now stop at each asynchronous boundary. The final initializer regression reproduced the same MST non-promise-yield error before the fix and passes afterwards.

The [unit receipt](issue76-recheck-20261013-latest/unit-regression.json) records all **689/689** Data Manager tests in 23 suites passing. The nine new lifecycle cases include seven failure paths and two live controls; before/after logs are preserved. These isolated unit tests use matching locked Node 20 dependencies; native browser acceptance uses the separately built production assets.

## Fixed artifact and acceptance

| Field | Current value |
|---|---|
| Runtime source | `af1cb115c96dcd1b9a5b179938752d8b750fbab9` |
| Test source HEAD | `0253cc4d6a5a00a103e4d7b600641ee34276740a` |
| Version | `1.23.0+fork.rc76.af1cb115` |
| OCI index | `sha256:7f95d234dd58360bb610f47d9c67e89182df5d01fdfe5fb91c14020f9a9be14f` |
| Platform manifest | `sha256:279f97f8cfef3636bcd2f2286e11311f2438f4ac3633c987d6df3a38ee775495` |
| Image config | `sha256:e0aed3116cfd97ae51acce8e12b2815c5fbe9d54aa78fb35ab8aa11c5840a3c0` |
| Offline archive SHA-256 | `adf74a5d31f8961109e3de63eff01ae1e89f3335b5568406edb151dcc4191673` |

The [canonical build inputs](issue76-recheck-20261013-latest/build-inputs.json) pin Debian/Python 3.11/Node 20 and verify Git objects, links and executable bits. [Package identity](issue76-recheck-20261013-latest/candidate.json), [OCI verification](issue76-recheck-20261013-latest/offline-verification.json), negative integrity probes and a local archive load all passed. Later commits change test synchronization and handoff evidence only; they do not relabel the runtime source as the later PR HEAD.

The [native browser receipt](issue76-recheck-20261013-latest/browser-summary.json) records **48/48** in nine suites, with no failed, pending or skipped cases: login timeout 2, slow project/import and cancelled initialization 6, application locale 24, editor locale 2, export locale 6, collaboration locale 4, reviewer workspace 2, and one full journey for each language. Full journeys cover project creation, real JSON import/readback, independent members/roles, assignment, draft, submit, reject/revise/approve/release/history, re-login, stale-page revocation and reassignment. Export display/format-ID/byte checks intentionally intercept the synthetic format list and download response; they do not qualify every converter or external storage service.

The qualification combines 46 unchanged cases from the full run at `e0447a72` with two affected reviewer cases revalidated at `0253cc4d`. Only the explicit `loginLocale` setup changed; the other suites do not set that option. Per-suite execution HEADs and log hashes remain in the receipt; this does not claim a single full run at the later test HEAD.

These flows use the native production assets on loopback HTTP, PostgreSQL and new synthetic identities. They do not establish Edge/trusted-browser HTTPS acceptance. The [postflight](issue76-recheck-20261013-latest/postflight.json) verifies both replicas' backend/three frontend identities and the served bundle, six healthy services, the same database container, 161 original rows unchanged and 57 old evidence files unchanged. All temporary accounts created by this run were disabled through the existing account-security service and their sessions/password files cleaned up; original identities and test data are retained.

The PostgreSQL-only reviewer fixture command also retained an unnecessary nullable annotation join in its assignment lock. Removing that join preserves the assignment lock and uses lazy annotation access inside the existing transaction. Both reviewer-workspace cases failed on the previous native image and pass in this artifact. Browser CI now uses PostgreSQL; SQLite foundation coverage remains in Fork PR Gate. The import cases wait for the completed upload row and actual final reimport 201.

PR #77 had no review threads or formal reviews at preflight. Its prior HEAD's two checks passed, but those results do not qualify this follow-up. Current-head CI is the live PR check record; formal external review remains outstanding. The old 112 operational assertions and seven upgrade checks belong to the historical image and are not inherited here. Target-environment HTTPS, receiving environment, full second daily cleanup interval and an authorized old security/schema rollback target remain separate gates. Issue #76 remains open.

## Retained local trial

- Browser: [local projects](http://localhost:19680/projects).
- Existing project: `annotation-engine-issue76-20261009-finalized`; database and named volumes retained, with fresh private logical backups before each image replacement.
- Existing Compose file: `C:\Users\Henry\.codex\deployments\annotation-engine-issue76-20261009\finalized\compose.rc.yml`.
- Current restricted interpolation file: `C:\Users\Henry\.codex\private\ls-issue76-recheck-20261013\runtime-af1cb115.env`. The historical interpolation file remains intact; use this current file when recreating the serving image.
- Offline package: `C:\Users\Henry\.codex\deployments\annotation-engine-issue76-20261014-af1cb115\offline`.
- Full local report: `C:\Users\Henry\.codex\reports\ls-issue76-recheck-20261013`. Native test sources are in this PR; local execution/generation hashes bind the scoped runner to those sources.

Stop/start only the existing project with its current interpolation file. Retain volumes; do not run `down -v`, prune, regenerate trust, restore a database or enable IAM recovery as part of this handoff. The first image-update readback encountered Docker Desktop's alternate Windows bind-path representation; the checker now resolves only the two existing expected folders and still requires exact named-volume identity.
