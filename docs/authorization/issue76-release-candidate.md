# Issue #76 local release candidate (historical 2026-10-09 receipt)

The active local image and current startup interpolation file are documented in the [2026-10-13 recheck](issue76-recheck-20261013-latest.md). All artifact identities and PASS results below belong only to the historical `38bc51f9` image; do not inherit them for the current artifact.

This unmerged candidate delivers the first two steps of [#76](https://github.com/qq550723504/annotation-engine-label-studio/issues/76). Target-environment operations remain under #48/#44. Core integration is deferred under [DPP #294](https://github.com/qq550723504/data-product-platform/issues/294); this change does not alter its Core-only-review contract or DPP files. [Draft PR #77](https://github.com/qq550723504/annotation-engine-label-studio/pull/77) is the sole writer result.

## Ownership and identity

The initial inventory found the main checkout clean, no other worktree/open PR, no active LS build or application process/container, and completed environment-error receipts in the two earlier candidate chats. No other chat was restarted. The source baseline was `f3b69385f5d892f89d1853f0cecbd739eed69783`, including #74/#75. Its matching push/main CI passed; those checks do not certify this new image.

| Field | Observed value |
|---|---|
| Candidate source | `38bc51f9211388e47b000851badffdab60e4144f` |
| Source tree | `1c5889592a9b43419ba90f55bbdeda9162e13589` |
| Branch | `codex/ls-issue76-candidate` |
| Version | `1.23.0+fork.rc76.38bc51f9` |
| Platform | `linux/amd64` |
| OCI index | `sha256:887f17229d79f3ab11a279cd6bf76b40885abd28b124027401a77fb4c8848380` |
| Selected platform manifest | `sha256:0e18ea502f9f530ece0846c83afe71cd6344cbe67c29e1f288d5f4417ec80604` |
| Image config | `sha256:2e5f9727217c6f64ecf40fa76409778d936044eb6460876d6ebfcd8253fb587d` |
| Offline archive SHA-256 | `8602dd1e4eb761de9dfbbf693aad6b311fcede895c141bfec59d298e38ad0b6b` |
| Archive size | 410,092,032 bytes |
| Build context SHA-256 | `0d0f6c4c8494d9fb67bd35095f26e94dcd7e3d6508429dbcf234ec1499f7ba08` |

Later PR commits add acceptance tests and this handoff only. The image is bound to the source above; it must not be represented as an image built from a later documentation/test HEAD or merged main. Runtime source differences must trigger a new candidate build and new evidence.

## Reproduced blockers and narrow fixes

1. Native PostgreSQL rejected both Submission creation and review with `FOR UPDATE cannot be applied to the nullable side of an outer join`. Their lock queries eagerly joined unused nullable `annotation`/`submitted_by` relations. Removing these unused joins preserves the existing assignment/submission locks, authenticated actor checks and immutable snapshots. The existing submission, collaboration and assignment regression suites passed 68 tests against PostgreSQL with the patch, and the PostgreSQL CI step now executes these suites. The final built service separately executes fixture creation and browser Submission/review flows, recorded below.
2. With the project GET deliberately held, ImportPage exposed a file input while ProjectProvider still held `{}`; both language cases failed on the original native image. The import view and Finish action now require a concrete project ID, and empty Cancel does not delete uploads against an absent ID. The regression releases the project response, imports real JSON, finishes and reads back the imported task. ProjectProvider's empty-object representation conflates loading/failure; this patch applies the existing Data Manager readiness rule without broad provider changes.

3. The new real-import browser regression failed in CI with protected `View[]` and locally with protected `User[]` writes. Completing import reloads the Data Manager while its bootstrap requests are still pending. Their completions attempted to mutate the destroyed store. Request/Project/Users/Tabs and parent bootstrap continuations now use the library's existing `isAlive` gate before state changes or error notifications. Four new destruction cases fail on the old code; all 11 focused tests pass after the patch. The new image reruns its own acceptance, without inheriting the superseded `897e655f` candidate's 112 operational checks.

The older export-locale check counted preview requests immediately after the modal shell appeared. The project-identity readiness fix makes that assumption invalid; it now waits for the actual 200 sample-preview response before checking that opening import persists no project data. The two original failures and corrected rerun are retained; runtime code did not change for this harness fix.

The exact-head authorization CI also exposed an existing random-email collision in `UserFactory` (279 passed, 1 failed, 6 skipped; SQLite unique-email constraint during a 21-account fixture). Factory Boy's existing `Sequence` now generates unique synthetic emails. This changes only the test fixture; both existing parameter-budget cases passed in a disposable SQLite diagnostic container with only that fixture mounted read-only. Coverage was not collected there. Fresh exact-head CI validates the complete suite separately from artifact acceptance.

The PostgreSQL legacy fixture uses historical migration models, which bypass the ordinary token-creation signal. Its missing account Token caused an initial page check to return 500. The fixture preparation now supplies that normal prerequisite; this was a harness correction, with no product patch. Earlier failed/canceled build and diagnostic source-overlay attempts are excluded from final-candidate acceptance.

## Supported build and integrity verification

Use `scripts/build_fixed_candidate.py` and `deploy/candidate/Dockerfile.debian`. The runtime/backend are Debian bookworm/Python 3.11, and frontend compilation uses Node 20 Alpine. The repository's original Python Alpine Dockerfile is **NOT_RUN** for this candidate. The new entry point reconstructs all Git blobs, executable bits and symlinks from a full commit, supplies standalone Git metadata, resolves and pins base indices/platform manifests, and records installer, recipe, archive, context and lockfile checksums. See [recorded inputs](issue76-candidate/build-inputs.json) and [native metadata](issue76-candidate/candidate-native-metadata.json).

Use an amd64 Docker builder; other build-host architectures are not qualified by this receipt. Example (PowerShell, use a new exclusive output directory):

```powershell
python scripts/build_fixed_candidate.py --repository '<isolated checkout>' `
  --source 38bc51f9211388e47b000851badffdab60e4144f `
  --branch codex/ls-issue76-candidate --output '<new absolute directory>' `
  --version 1.23.0+fork.rc76.38bc51f9 --tag annotation-engine-label-studio:1.23.0-fork-rc76-38bc51f9 `
  --node-image mirror.gcr.io/library/node@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 `
  --python-image mirror.gcr.io/library/python@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89
```

A rebuild records its own installer/OS-package resolution and output digests; byte-identical reconstruction is not promised. Use the actual verified archive when this exact identity is required. The manifest/checksum must arrive via the approved release channel; a checksum is not a signature or proof of provenance.

The local offline directory is `C:\Users\Henry\.codex\deployments\annotation-engine-issue76-20261009\finalized\offline`. It contains `label-studio-rc76.tar`, `candidate.json`, `SHA256SUMS` and `verify_candidate_bundle.py`, with no runtime credentials, database backup or certificate key. Availability is local only. Receiving environment/distribution destination remains unspecified; no registry publication or external upload occurred.

At an authorized receiver, before loading:

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath '<received package>\label-studio-rc76.tar'
python scripts/verify_candidate_bundle.py '<received package>\label-studio-rc76.tar' '<received package>\candidate.json'
docker load -i '<received package>\label-studio-rc76.tar'
docker image inspect annotation-engine-label-studio:1.23.0-fork-rc76-38bc51f9
```

The verifier checks the archive checksum, every OCI blob, descriptor sizes, platform, config, layers and source/version labels without extraction or registry access. Wrong archive checksum and wrong config identity were rejected. Local load was exercised; receiver execution is **NOT_RUN**. Docker Desktop's containerd image store reports the OCI index as `.Id`; do not confuse it with the image config digest. After load, compare runtime backend/frontend metadata and static bundle hashes against the recorded native metadata.

## Acceptance scope and remaining gates

[Aggregate local receipt](issue76-candidate/local-validation.json) and [all 112 operational assertions](issue76-candidate/operations-checks.json) bind these new observations to the candidate. The 40 browser tests use acceptance sources through `ce6de9dd4e2fca7bb01f97788f81ab4657187550`; the later factory-only repair is `9761dd3b08054caa39c84a5df5a7d884ed5b2aa1`. Neither changes runtime code after the image source.

| Check | Status |
|---|---|
| Canonical source/context and native Debian build | PASS |
| Backend and all three frontend commit/branch identities; compiled locales; executable symlinks | PASS |
| Offline archive, 26 blobs, OCI chain, negative integrity checks and local load | PASS |
| PostgreSQL pre-locale upgrade and safe English display fallback on both replicas | PASS (7 new checks; flags/counters/audit retained; no schema downgrade) |
| Native HTTPS cookie/session/revocation/cleanup/restore | PASS (92 new checks; [receipt](issue76-candidate/operations-summary.json)) |
| Native static resources/read-only audit/checkpoint negatives | PASS (20 additional checks, 112 total; [receipt](issue76-candidate/additional-summary.json)) |
| Candidate loopback HTTP bilingual browser journeys | PASS (40 tests across 7 suites; [receipt](issue76-candidate/browser-summary.json)) |
| Trusted browser HTTPS, receiving environment, second complete 86400-second cleanup cycle | NOT_RUN |
| Old English security/schema artifact rollback target | NOT_RUN; no old artifact selected |
| Production deployment, registry publication, Core integration | NOT_RUN |

HTTPS requests in the operational harness use a process-local CA file with normal certificate/hostname verification. System/browser trust was not modified. Browser flows use the actual built assets over loopback HTTP and remain separate from trusted-browser HTTPS acceptance. Cleanup's real service, failure/restart and recovery can be observed within the local test; a forced restart is not proof of a complete second daily interval.

## Retained local handoff

The isolated release directory is `C:\Users\Henry\.codex\deployments\annotation-engine-issue76-20261009\finalized`; Compose project is `annotation-engine-issue76-20261009-finalized`. [Recorded Compose](issue76-candidate/compose.recorded.yml) refers to the preserved local `scripts/` and `private/` directories. It is a synthetic acceptance configuration, not an authorized production deployment manifest. App sessions are DB-only with Secure/HttpOnly/SameSite=Lax host-only cookies, both replicas share the authoritative DB/secret, and signup/analytics/error reporting are disabled.

- Browser trial: `http://localhost:19680/projects`.
- Replica HTTPS endpoints: `https://localhost:19643`, `https://localhost:19681`; restore test endpoint `https://localhost:19682` is stopped after verification.
- Synthetic admin credentials are referenced only in restricted `private/runtime.env`; Cypress role fixtures remain local under the separate browser runner. Do not commit or upload them. Certificates expire after the bounded trial; regenerate within an authorized local environment without changing system trust.
- Operational receipts, redacted logs, private backup/checkpoint, browser logs and source-generation hashes are retained locally. Historical #72 digests/112 assertions and four #48 rc5 data volumes remain preserved, with no inherited PASS.

Stop/start only this project, retaining volumes:

```powershell
Set-Location 'C:\Users\Henry\.codex\deployments\annotation-engine-issue76-20261009\finalized'
docker compose --env-file private/runtime.env -f compose.rc.yml stop
docker compose --env-file private/runtime.env -f compose.rc.yml start db app app-b nginx nginx-b session-cleanup
```

Do not run `down -v` or volume pruning. Rerunning the bounded restore acceptance against its already populated restore volume is not supported: use a fresh exclusive synthetic project. Before an actual rollback/cutover, select and authorize the artifact/schema compatibility and retain the latest trusted security checkpoint; never choose an older insecure/schema-incompatible artifact merely to return to English.

Exact source CI, latest PR HEAD CI/review, merge/main CI and runtime/target acceptance are separate records. This draft is unmerged. No #48/#44 target-environment gate or #76 completion is inferred from these local receipts.
