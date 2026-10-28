# NER frozen-revision arbitration v1 (#81)

Implemented on `codex/issue81-ner-arbitration`, based on main merge
`3001f8f3618100ec737c33d713c78f9d02a25407` (PR #120).
The [confirmed design](ner-arbitration-v1-design.md) defines the full contract.
Local acceptance was completed before publication. GitHub CI acceptance is
recorded separately against the PR's final head commit.

## Product behavior

A Manager dispatches one persistent NER batch's original disagreement to a
current independent Reviewer. Only that assigned Reviewer may adopt one complete
existing frozen Submission revision, with a reason. A genuine empty-entity
revision can win; missing, malformed, agreement and all-double-empty cohorts
cannot be dispatched. Manager cancellation is a separate terminal action.

The NER batch workspace opens this complete arbitration flow and returns to a
fresh read of the same source task. A separate project queue supports assigned
reviewers. The shared arbitration lifecycle UI uses a typed NER adapter for
transport, schemas, receipts and NerResult evidence. It never invokes
classification arbitration, review handoff or manual NER as a fallback.
Choosing a winner outside the displayed pair requires a verified GET containing
that source before adoption is enabled. Read refresh does not repeat a POST.
An unknown write retains its original UUID/body for explicit recovery; an
acknowledged write remains saved if the subsequent read fails. Blur, identity
changes and stale responses clear evidence and cannot transfer an old command
to a new identity. English and Chinese catalogs cover the flow.

No original Submission, Annotation, review state, metric, batch, sampling or
release policy is changed. No entity editing, merging, reassignment, batch
arbitration, NER conflict-priority sampling or handoff is added. #81/#84 stay open.

## Authorization and API

Enable only with all three NER disagreement/batch/arbitration flags and exact
`organization:project = fork_review` entries in both NER mode maps. Arbitration
defaults to off. Core-only-review is inapplicable. Existing classification
operations do not grant `organize_ner_arbitration` or `arbitrate_ner`; custom
operation providers must grant the new operations explicitly.

Server-side identity, current organization membership, current project role and
READ_WORK are authoritative for reads. Collection filtering precedes pagination
and counts. Dispatch requires Manager + organize_ner_arbitration; adoption
requires assigned independent Reviewer + arbitrate_ner. Participants, project
creator and dispatching Manager are excluded from candidate reviewers.
Manager security cancellation still checks current scope and all flags/modes,
but can work after READ_WORK/organize permission withdrawal without reading
source answers. Recovery requires current authorization for the original action.

Prefix: `/api/projects/{project}/ner-arbitrations/`.

| Endpoint | Contract |
| --- | --- |
| GET capability/ | Typed schema/rule, actor and viewer hash, independent operation capabilities |
| GET assignees/ | Paged current independent reviewers for exact NER batch/task/manifest |
| GET collection | Assigned-reviewer or Manager metadata, state filter and pagination |
| POST collection | ner_batch_id, task_id, expected_manifest_hash, assignee_id + UUID |
| GET {id}/ | Frozen full-cohort evidence; optional paired left/right selection |
| POST {id}/decision/ | expected_version, expected_input_set_hash, winner_submission_id, reason + UUID |
| POST {id}/cancel/ | expected_version, expected_input_set_hash, reason + UUID |

All bodies are exact allowlists. Actor/principal/source cannot be supplied by
the client. Writes are 201 on creation, 200 for original-key replay. Typed error
domain is NER_ARBITRATION_; invalid input is 400, scope denied 403/404, state/
input/key conflict 409, budget 422 and unsupported engine/backend 503.
Responses are private/no-store and no-referrer, checked against actual rendered
JSON bytes and representation digest. A source-unavailable detail keeps frozen
denominators and valid terminal metadata, while hiding all text, entities and
input pins; it never invents an empty-input score.

## Frozen ledger and concurrency

Separate NerArbitrationTask/Decision tables use `ner-arbitrations/v1`,
`ner-arbitration-input/v1`, and `adopt-exact-ner-submission/v1` domains.
The frozen origin contains the original full-batch manifest hash, provenance,
rule versions and complete target cohort. It stores no raw answers.
Semantic deduplication ignores batch names/times/IDs but includes every target
source pin and rule domain. Pending/resolved inputs are unique; a cancelled
input can be explicitly redispatched with a new UUID.

Independent create/decision/cancel UUID namespaces bind the exact legal body,
project/resource/action, principal, identity source and frozen actor. Receipt
replay precedes live sources and mutable processing budgets. Original create
receipts remain pending/version 1 even after terminal transitions; current state
is separate. Decision/cancel receipts preserve their original terminal fact.

Project-first fencing locks membership/user/project membership, arbitration,
NER batch, task, assignment and submission base tables in order. No nullable
join is locked. CAS admits one terminal decision or cancellation. The transaction
continues through rendering, both engine checks, final source/authority replay
and receipt checks; failures roll back and leave the UUID reusable.

Both PostgreSQL and SQLite enforce scoped unique/partial/state constraints,
immutable frozen/terminal fields and decision input/winner linkage. Nullable
user FKs are convenience links: Django clears them before user deletion, so the
trigger cannot prove why a NULL assignment occurred. Non-null rebinding is
denied; frozen IDs plus current authority remain decisive. User deletion cannot
change a frozen identity or grant permission. Project deletion deliberately
cascades its local ledger; this is not a permanent audit-retention subsystem.

## Budgets and cost

The original full batch must pass its fixed v1 storage envelope. Current
processing ceilings apply only to the entire authorized target cohort, never
to a selected pair or unrelated tasks. This does not change existing NER batch
read budgets. SQL byte gates precede JSON/Text decoding, including storage growth
between preflight and hydration. Origin, participants, winner, cancellation and
reason all have fixed storage envelopes.

New dispatch, adoption and verified detail hydrate the target twice and execute
exactly 2Q nervaluate counters, where Q excludes double-empty pairs. The final
source replay follows the last counter. Twenty nonempty inputs give 190 pairs
and 380 calls. Two empty inputs plus one nonempty input give Q=2 and four calls.
Metadata, assignees, capability, cancellation and original-key recovery use zero
snapshot loads/counters. Existing text/XML/entity/byte/work/output ceilings are
reused; whole-target preflight rejects before the first counter and never reads
other tasks to find a cheaper subset.

## Acceptance evidence (2026-10-28)

Local evidence directory:
`C:/Users/Henry/.codex/reports/ls81-ner-arbitration-20261028/`.
The directory retains failed setup attempts as well as final successful results.

| Validation | Result / evidence |
| --- | --- |
| PostgreSQL classification/NER/review regression | 394 passed; postgres-regression.log |
| Final NER arbitration API/security/budgets/storage/deletion | 23 passed; postgres-unit-final.log |
| Final independent PostgreSQL connections | 13 passed; postgres-concurrency-final.log |
| SQLite guards and API | 23 passed; sqlite-final.log |
| Shared frontend flows | 7 suites / 117 passed before the last two UI tests; frontend-regression.log |
| Final NER frontend, including two extra race/pair tests | 15 passed; frontend-final.log |
| English + Chinese real browser journeys | 2 passed; e2e-final.log, six screenshots and video |
| Migration state | No changes detected; migration-check.log |
| Ruff, scoped new-file Biome, catalogs/literal guards | Passed; retained logs |
| Development and production webpack builds | Passed; frontend-build-second.log, production-build.log |
| Owned runtime closeout | Zero listeners on 25481/25482 and zero owned Python services; runtime-closeout.json |

Browser acceptance used PostgreSQL 17.6 and an isolated server at
127.0.0.1:25482, with synthetic identities and data only. It verified Manager
dispatch/cancel/redispatch, direct annotator denial, empty revision adoption,
lost acknowledgement after a real commit, same-key/body recovery without refresh
reposting, frozen replay after a newer revision, source corruption/restoration,
and revocation clearing answers and rejecting direct adoption. Both locales
also exercised a 390px window. The PostgreSQL binary was downloaded from the
published Zonky test artifact and its checksum recorded; no Docker or Windows
service was used.

The added ner-arbitration.yml runs PostgreSQL regressions, production build and
bilingual Chrome E2E in disposable CI resources. It has not been dispatched or
accepted on GitHub in this local implementation step. An additional broad local
Biome check found formatting/style diagnostics in shared existing files; baseline
stdin checks also fail. That broad check is not reported as passing. New NER
files pass their scoped Biome check; this patch avoids a broad legacy reformat.

## Retained runtime boundary

Only this worktree and this report's synthetic native test resources are used.
No Host edits, merge, deployment or Core integration. Docker/#76 were not
started, stopped, restarted or written. The earlier old/new CHANGED,
CHANGED_DATABASE_CONTAINER_REPLACED, cause UNKNOWN, missing protected-worktree
history and NOT_RUN_AFTER_DOCKER_INTERRUPTION full-database comparison remain
unresolved historical facts. Local tests and CI do not replace that evidence.
