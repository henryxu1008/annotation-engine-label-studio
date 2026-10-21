# #79 local synthetic implementation acceptance

Date: 2026-10-21. Source: `f5ff3c6e829ce207c57849bc80548a918283207e` on
`codex/issue79-release-design`, based on `main@88809e61`. The user confirmed the
design before implementation. This receipt records **local software acceptance**,
not a merge, deployed release, Core integration or human/product acceptance.

The [operating contract](approved-dataset-export.md) describes the delivered API,
permission matrix, frozen manifest, recovery and #80/Host boundary. Actual
synthetic files and checksums are in [the evidence directory](issue79-local-acceptance/summary.json).
Its artifacts are execution receipts, distinct from the earlier design-only
example vectors.

| Check | Result |
| --- | --- |
| Isolated PostgreSQL API/worker/security/concurrency | **83 passed**: 49 new #79 cases and 34 existing Submission/review/collaboration/project-authorization regressions |
| Frontend component/race/localization | **24 passed**: 4 batch cases and 20 existing collaboration cases |
| Actual Edge en-US / zh-CN journeys | **4 passed**: native batch, real CoNLL, Manifest download, reload/history recovery, stale-page revocation and reviewer API denial |
| Three approved samples per actual batch | JSON and JSONL preserve exact selected records; frozen snapshots, XML hash, record/content/input/artifact/manifest digests verified |
| Real standard format | SDK **2.0.18** CoNLL2003; BIO text, per-output source index and archive-member checksums verified |
| PostgreSQL migration | Fresh independent database: forward, reverse to data_export.0010, forward to .0011; no retained/E2E database reversal |
| Static/build | Migration drift, Python F/E9 checks, workflow YAML and shell syntax, catalogs, migrated-literal checks, native vectors under CPython 3.11 and frontend build passed |

The PostgreSQL cases include pending/rejected/superseded/missing approvals,
changed IDs/revisions/hashes/config, malformed snapshots, project/task/org scope,
untrusted authority fields, membership/account revocation before and during jobs,
HEAD/OPTIONS/Range denial, Core-only mode separation, source cascades, corrupt
files/manifest, idempotency on two DB connections, duplicate/stale workers,
dispatch loss, expired recovery, audit/durability failures and private storage.
Same-task different-assignment samples remain distinct in actual CoNLL output.
An approved old revision remains frozen after a newer pending submission.

Source deletion cannot reopen legacy mutable API, conversion-worker or storage
delivery: retained batch evidence keeps that boundary closed. Download remains
authenticated with fresh source/creator checks; corruption quarantines the
receipt. Both native and standard files are synchronized before READY, with
directory metadata synchronized on the validated Linux backend.

Runtime: Python 3.11.17, Django 5.1.15, PostgreSQL 17.11, Redis 7.4.11,
Node 24.18.1, Cypress 14.5.0, Edge 155. The SDK archive identity is verified against
the lockfile. No #76 candidate image was used as the test application image;
the independent test dependency image uses the existing IAM dependency base and
loads #79 source from its own worktree. The future CI workflow uses Node 20 on
its disposable runner; that GitHub run is not claimed here.

All resources belonged to `issue79 / 20261021`: three dedicated containers,
one independent network, a separate PostgreSQL instance, separate browser/cache
and file roots. The final containers are stopped and retained with their data;
no volume, image, network or shared resource was pruned. #76's worktree HEAD/Git
state and six container IDs, images, StartedAt and mounts match the initial
read-only checkpoint. No #76 runtime operation or write was performed.

Not run: GitHub PR CI, merge-SHA push/main CI, merge, deployment, Core integration,
real external-provider use, human/product acceptance, maximum-size load benchmark
or other standard-format acceptance. New routes remain default-disabled until an
authorized operator supplies an explicit project policy and queue configuration.
Media is references-only; COCO/VOC/YOLO remain disabled. Cross-system versions,
durable media and Host publication remain #80/Host responsibilities.

Local detailed logs, XML, screenshots and dependency harness are retained under
`C:\Users\Henry\.codex\reports\ls-issue79-20261021`. No credentials or private
environment values are included in the committed receipt. The new workflow makes
the relevant checks reproducible without claiming that local results are CI.
