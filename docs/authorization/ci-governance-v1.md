# Enterprise UI CI governance V1

Implementation contract for the [approved design](ci-governance-v1-design.md) and
[24-case matrix](ci-governance-v1-acceptance.md). Those documents remain historical
design snapshots; executed local and remote evidence is recorded separately.
The user confirmed second-review V1 as supervisory_only on 2026-11-02, but this
implementation contains **CI governance only**, with no second-review product code.

## Job and source contract

The existing Enterprise Browser E2E workflow retains all main PR events, push/main,
manual dispatch, concurrency, GitHub Actions read-only permissions and the unique
required name `Current enterprise UI validation`. The other required check and
repository rules are untouched.

`prepare-ui` runs the original nine catalog/unit commands plus production build.
Nx local/remote execution caches are disabled for these commands; every unit
invocation must produce its own fresh structured Jest/Node report. The literals
package script is expanded into its same checker and Node test file with the native
JUnit reporter. Other changes to command arguments are limited to structured
report output and serial Jest execution. Actual normalized argv is checked again
by aggregation, including all three original UI test paths.

The nine fixed browser shards retain the old 22 unique specs / 23 calls, and add
the dedicated confidence journey, yielding 23 unique specs / 24 calls. Each shard
uses its own PostgreSQL service and server, original reset ordering, Electron,
config/locale values and limits. The entity-boundary call keeps SIGINT at five
minutes and SIGKILL after fifteen seconds on Linux. Source specs are not changed.
Most bilingual specs prove locale coverage through actual test titles. The four
existing collaboration-locale tests switch languages within each test; all four
frozen cases must pass instead of inventing a second invocation.

The same-name required job uses always/needs and read-only paginated Actions APIs.
It independently selects the latest actual execution for each logical job,
including prior successful attempts during a partial rerun, then verifies explicit
immutable artifact IDs and bounded structured evidence. Latest failure, cancellation,
timeout, missing/duplicate jobs, skipped/neutral results or missing invocations
cannot be masked by an older success. A failed aggregate emits a bounded static
error code/stage and sanitized job/attempt map and remains nonzero. Archival is
attempted even after failure; it never converts a failed check to success.

GitHub partial reruns may create a new successful job view whose creation is
later than its already-finished execution. Select the latest view first and
reject its failure, cancellation, incomplete or unknown status before resolving
any producer. A copied success must match one unique older actual execution:
same scope, start/end, runner identity, labels and every step. Actual producers
must have creation <= start <= end; copies must have creation > end. Intervening
failed, incomplete, newer actual or unproven executions block old-success reuse.
The observed hosted runner-group sentinel 0/null difference is normalized
explicitly, with both raw values retained; other execution drift rejects.

Artifacts, receipt attempt and build tuple bind the actual producer, not the
new API view. The execution map reports both job IDs/attempts and the copied
success flag. Terminal verification compares both identities and their execution
fingerprints. Unknown unexpanded matrix placeholders still fail closed and
require a fresh complete run; they are not silently classified or filtered.

GitHub receipts require an initially clean exact checkout and recheck cleanliness,
HEAD, inventory and recipe at critical boundaries. Browser execution rechecks
source and the installed dist after the last spec, before its receipt. Local
preflight receipts explicitly record whether the checkout was dirty; they cannot
satisfy a real GitHub required check. PR head/base and actual virtual-merge checkout
are separate values; push/main and dispatch bind their actual checkout.

## Build, reports and privacy

Build manifests bind run/producer attempt, source/inventory/recipe, all static file
paths, sizes and SHA256. The consumer downloads an explicit same-run artifact ID,
validates the wrapped tar in a private staging directory before installation,
and rechecks the dist around browser execution. Symlinks, hardlinks, traversal,
duplicate/case-aliased/device paths, private build names and hash/size drift reject.
Member limits remain 256 MiB total / 64 MiB per file / 5,000 files. The tar envelope
has a separate 272 MiB cap for bounded headers and padding.

Receipts are limited to 256 KiB / 128 records. Reports are at most 4 MiB each /
16 MiB per job; aggregation accepts at most ten receipt sets / 160 MiB reports.
Actions history is limited to 256 jobs / twenty attempts. Oversized or ambiguous
history requires a new complete run rather than truncating old failures.

Only typed reports and receipts are exported. Raw output, login fixtures, settings,
HTTP objects, cookies, passwords and request/error dumps remain outside artifact
directories. Known synthetic passwords are masked before Cypress and filtered
from narrow reports. Videos/screenshots are still generated by the original
video configuration but are deliberately not archived in this first implementation;
the receipt declares that diagnostic omission. No binary diagnostic export exists.
Before upload, the producer verifies the exact referenced file set, byte/hash
limits and privacy of every JSON member, then copies only validated bytes into
a dedicated export directory. Upload reads only that directory. The receiver
independently enforces the same exact allowlist after downloading the evidence ZIP.

Five original editor it.skip cases are preserved as exact path/fullName entries
in `ui_unit_skips.json`, with baseline locations and known/unknown reasons. They
remain SKIPPED, not passed. New skips, wildcard exceptions, missing expected skips
and entirely skipped suites fail the evidence contract.

## Native synthetic server boundary

Before any database-writing management command, validate native TEST_ENVIRONMENT,
backend, loopback host, exact port/name/data/workspace scope, and a fresh public
schema. Linux Actions uses the job-owned ephemeral service. Local acceptance
additionally requires a task-local cluster marker and actual PostgreSQL system ID /
data directory, using the same connection parameters as Django. No retained schema
is reset. Server startup checks the ownership proof again and uses no autoreloader.

The confidence bootstrap validates exact fixture/settings keys, fixed operation
provider, both language projects, users, profile digest, namespace and grants,
then verifies actual synthetic database rows. It loads settings only into that
CI server process; it is not a product startup option or a deployment mechanism.

The actual root PO catalogs for en_US/zh_Hans are compiled to job-private files,
verified through source/MO hashes and GNUTranslations, then loaded only through
this server's LOCALE_PATHS override. Repository-tracked .mo files stay unchanged.
Linux uses Django/GNU compilemessages; Windows local acceptance uses Babel and
records that distinct method. Neither proves the other platform's execution.

The isolated compiler uses the conventional `catalogs/locale/<language>/LC_MESSAGES`
tree and runs Django discovery from `catalogs`. Django ignores LOCALE_PATHS when
DJANGO_SETTINGS_MODULE is unset; configuring that setting alone does not make the
old nonconventional tree discoverable. Discovery regression tests use the actual
Django command with only the external compiler boundary mocked; they are not
reported as Linux/GNU execution.

## Controlled dependency cache measurements

Manual dispatch accepts `dependency_cache=warm|cold`, default warm. PR/push keep
the existing cache behavior. Cold disables setup-python/setup-node cache restore
and save, and establishes fresh job-private Poetry cache/venvs, pip, Yarn, Cypress
binary and XDG cache directories before installation. Existing caches are never
deleted. This isolates project dependencies, not the hosted OS/apt/tool image.

The mode is derived from the actual event payload and bound into the source context.
Prepare/browser receipts require the same-mode bootstrap proof; cold receipts also
require the applied environment to still match the controlled directories.
Aggregation rejects mismatched modes or isolation evidence. Requested warm does
not prove cache hits: record actual cache metadata and timings separately.
Cold/warm runs have independent identities and artifacts; no cross-run build reuse.

## Authorization gate duration isolation

PR #134 exposed a separate serial-budget problem in Fork PR Gate. At head
`72c7fe609`, its single required job exhausted the existing 25-minute job limit
while PostgreSQL regressions were still running. Successful SQLite assertions
and browser evidence do not make that cancelled required check successful.

The authorization workflow now has three explicit producers: SQLite (including
candidate/archive contracts, migration consistency, API smoke, locale and artifact
audit checks), PostgreSQL session/submission/review, and PostgreSQL comparison /
packages / project-policy checks. Each retains the original 25-minute limit,
Python 3.11, database service and test options. The frozen inventory preserves
every original database-specific selector, marker and verbosity exactly once;
product tests and fixtures are unchanged. No producer uses a matrix, conditional
skip or continue-on-error.

The unique required name remains `Authorization foundation tests`. Its five-minute
aggregate runs with `always()` and requires exactly those three dependency results
to be `success`; missing, extra, failed, cancelled, skipped or malformed results
fail closed. The existing bounded JSON parser rejects duplicate keys. This uses
GitHub Actions job dependencies rather than introducing a second scheduler.
The required name/app, branch rules, events and read-only permissions remain intact.

Splitting repeats environment setup and permits three authorization runners at
once. The theoretical timeout sum increases from 25 to 80 runner-minutes
(three times 25 plus five for aggregation); this is not a cost reduction claim.
Local measurements include every pytest process startup, database creation,
migration and teardown, but exclude hosted service/dependency setup. Actual hosted
job durations, queue time and runner-seconds must be measured again on the new
published head. Browser concurrency, 300-second call timeout / 15-second kill
grace, producer evidence rules and UI budgets are unchanged.

## Source index

- `.github/workflows/enterprise-browser-e2e.yml`: prepare / fixed matrix / required aggregate.
- `scripts/ci/ui.py`: explicit workflow commands.
- `scripts/ci/ui_inventory.py`: frozen invocation and permitted reporter-argv mapping.
- `scripts/ci/ui_common.py`: bounded JSON/files, privacy, exact context and source checks.
- `scripts/ci/ui_artifacts.py`: bounded build package and evidence ZIP handling.
- `scripts/ci/ui_results.py`: structured result and exact skip validation.
- `scripts/ci/ui_run.py`: actual prepare/browser execution, ownership and lifecycle.
- `scripts/ci/ui_server.py`: synthetic-only runtime settings and row verification.
- `scripts/ci/ui_catalogs.py`: private two-language compilation and verification.
- `scripts/ci/ui_dependencies.py`: dispatch-only project dependency isolation and proof.
- `scripts/ci/ui_aggregate.py`: read-only Actions metadata, latest attempts, strict aggregate.
- `scripts/ci/run_cypress.cjs`: locked Cypress Module API, narrow actual-result projection.
- `scripts/ci/ui_unit_skips.json`: five pre-existing explicit unit skips.
- `scripts/ci/tests/test_ui_contracts.py` and `scripts/ci/tests/run_cypress.test.cjs`:
  local contract/fault-injection regressions; synthetic job metadata is not live CI.
- `scripts/ci/tests/fixtures/github_partial_views.json`: public live API metadata
  showing one original execution and its later copied success view, including the
  observed runner-group sentinel change; no logs, fixture credentials or settings.

Local scope is source/contract/build/native synthetic acceptance. Real GitHub
matrix execution, required-check behavior, Linux GNU timeout and hosted cold/warm
budgets remain NOT_RUN until separately authorized publication. No push, PR,
merge, deployment, second-review development, Host/Core, Docker/#76 or other
worktree/runtime/data operation is included. #81/#84 remain OPEN.
