# Second-review V1 CI integration

This extends the locally reviewed `bd2423d4f` implementation without changing its
`supervisory_only` effect, product permissions, frozen sources or release policy.
The frozen design/acceptance documents remain historical records. This stage
authorizes local CI integration, execution and independent review only.

## Closed coverage and isolation

The Enterprise Browser E2E workflow retains every original invocation and adds
one `second-review` shard: **10 shards, 25 browser calls, 11 logical evidence
producers** including prepare. The existing required check name, events,
max-parallel=3, prepare/browser/aggregate timeouts, immutable build identity,
receipt export, redaction, final-source checks and producer/view rules remain.
The new invocation runs the existing bilingual spec with its dedicated config,
Electron, video, no retries/skips, and a 300s watchdog plus 15s interrupt grace.

This shard owns the ephemeral `ui_ci_second_review_browser` PostgreSQL database
on 127.0.0.1:5432 in GitHub. Other browser shards retain `ui_ci_browser`. Before
the first migration, the runner verifies the job/private scope and actually
reads current_database, server data_directory, port, system_identifier and zero
public tables through a read-only connection. It writes a non-overwriting,
job-private bootstrap proof. The existing seeder independently checks that
proof and empty business scope. The legacy proof schema name contains `native`;
the values come from the actual connected server, including an ephemeral CI
PostgreSQL service, rather than a fabricated platform assertion.

The test-server CLI mode must match the scoped shard. Its typed fixture loader
accepts only the two locales, five independent synthetic humans, exact source
IDs/owners, historical task IDs, explicit feature flags, a stable instance UUID,
two matching org/project mode entries and the fixed synthetic operation provider.
It deep-copies settings, checks their database graph and applies process-local
overrides; LOCALE_PATHS is runtime-only. No product entrypoint imports this
bootstrap. Server startup rechecks the original cluster proof without refreshing
the original zero-table assertion. Fixtures and raw logs remain private.

The SQLite authorization job explicitly selects all seven second-review test
modules. Its PG-only concurrency/READY skips are recorded and never counted as
concurrency passes. The existing PostgreSQL directory selection already includes
the complete second-review suite.

## Budgets and partial reruns

The 160MiB aggregate report union and 256 job/artifact caps remain unchanged;
adding a producer does not raise them. A complete run can create 13 artifacts
(11 receipts, one frontend build, one aggregate map), so 20 complete attempts in
one run can exceed the artifact inventory cap. That case remains fail-closed;
use a fresh workflow run for the same authorized SHA rather than hiding history,
deleting evidence or relaxing limits. A partial second-review rerun can reuse
only proven original producers and their matching build/receipt artifacts.
Latest failed, cancelled, skipped or timed-out executions cannot borrow old green.

## Local acceptance matrix

Execution evidence, exact local commit/tree and independent findings are stored
in `C:/Users/Henry/.codex/reports/ls84-second-review-ci-integration-20261102`.
Definitions below become SUCCESS only when execution is recorded there.

| ID | Required observation |
| --- | --- |
| CI01 | 10/25/11 complete closed graph; canonical original 24 calls unchanged |
| CI02 | New spec/config/languages/watchdog and workflow DB mapping match |
| CI03 | Seven modules actually selected/executed on SQ; vendor skips explicit |
| CI04 | Actual bootstrap cluster/DB/port/zero tables checked before writes |
| CI05 | Nonempty/foreign scope rejected without migration, seed or data changes |
| CI06 | Server rechecks original proof; wrong identity or missing proof rejected |
| CI07 | Typed allowlist, independent humans, exact source graph; bad fixture denied |
| CI08 | CLI/shard mismatch rejected; settings remain frozen/deep-copied |
| CI09 | Missing new shard or new non-green execution blocks aggregate |
| CI10 | Wrong config/spec/language rejected even with valid file hashes |
| CI11 | Partial new-shard rerun preserves actual producer/view/build identities |
| CI12 | 256-artifact cap rejects before download; 160MiB bound retained |
| CI13 | Full Python/Node CI contracts and style checks pass |
| CI14 | Actual local ui_run.browser runs both locales and exports validated receipt |
| CI15 | Required gate/events/timeouts/private exports and source binding retained |
| CI16 | Final exact-head review/hash guard; old worktrees/Fork/issues preserved |
| CI17 | Real GitHub warm/cold/partial-rerun execution: NOT_RUN until authorized |

Local synthetic API replay is contract evidence, not an actual GitHub run.
Push, PR, merge, deployment, real users/Host and Issue closure remain NOT_RUN.
Docker/#76 and preexisting worktrees remain untouched. Retained runtime and full
database equality remain UNKNOWN. The unrelated existing ranged-file response
lifecycle issue stays recorded in the earlier implementation report.
