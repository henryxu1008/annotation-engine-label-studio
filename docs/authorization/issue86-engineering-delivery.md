# Issue 86 engineering delivery and deferred cloud work

## Scope

The #86 engineering change adds a default-off project lifecycle, audited
transitions, server-side operation fences, durable background-operation
admission, manager APIs, and a bilingual settings page. Existing projects remain
`legacy` until explicitly classified. Lifecycle state is separate from upstream
`is_draft`, `is_published`, and the task FSM.

Real-cloud old-identity/network isolation and request drain are tracked in
[#144](https://github.com/henryxu1008/annotation-engine-label-studio/issues/144).
Automatic version-safe deletion is tracked in
[#145](https://github.com/henryxu1008/annotation-engine-label-studio/issues/145)
and depends on the relevant #144 isolation evidence and defined deletion
semantics. The current code records deletion intents and does not automatically
delete cloud objects. See `issue86-s3-isolation-acceptance.md` for the boundary.

## Delivery and activation are separate

The implementation and its migrations may be reviewed with the lifecycle,
storage-admission and public-API flags left at their defaults (`false`) and the
legacy operation provider retained. This does not establish that production
activation is safe. Before enabling, operators must inventory and fence every
old web, worker, producer and provider path, establish an applicable drain,
validate rollout and preserve lifecycle enforcement through rollback. Unknown
external outcomes remain blocking. A successful local test, an empty queue or a
disabled credential alone is not evidence of cloud drain.

Nonempty lifecycle state, operation ledger or pinned versions deliberately block
destructive schema reversal. This protects data; it is not a tested lossless
downgrade to an older binary. An older binary must not receive traffic while it
can bypass the lifecycle fence.

## Local engineering checks

The disposable PostgreSQL/Redis test group is available via
`scripts/issue86-isolated-postgres-recovery.ps1 -TestEngineering`. It exercises
the project fences, import and reimport, ledger, webhook, migration, queue and
worker regressions. The isolated legacy worker fixture supports
`-TestMixedUpgrade` for old sync, old queue ownership, durable webhook and old
worker crash scenarios. Both require a compatible Linux dependency environment;
the mixed fixture additionally requires a Linux MinIO client and local images.

`scripts/issue86-isolated-ui.ps1` runs the Cypress lifecycle journeys against a
dedicated local PostgreSQL database, synthetic users and a loopback-only browser
relay. It checks English and Simplified Chinese transitions, a direct stale task
write after pause, and non-manager lifecycle access. It does not test a deployed
service, external identities, every Editor interaction, or real-cloud behavior.
The local diagnostic build may use a temporary resolver and disabled
minification; it is not a release artifact. The lifecycle UI workflow is
`.github/workflows/project-lifecycle-ui.yml`; its remote CI result must be
verified on the actual proposed change before claiming CI success.

The November 16, 2026 local checks included 200 passing PostgreSQL/Redis
engineering tests with four environment-specific skips. The three mixed-version
tests skipped in that group were run separately against the pinned 1.23 base
and passed, as did the separate old-worker crash case. The SQLite-specific
case was also run separately and passed. Four frontend Jest suites passed 24
tests, the bilingual browser fixture passed three journeys, catalog validation
passed, and `makemigrations --check --dry-run` reported no model changes.
These numbers describe the local fixtures and are not results from hosted CI.

The changed tracked Python files retain pre-existing lint/format findings from
the main baseline. A comparison with that baseline found no newly introduced
Blue formatting failures; the repository-wide lint check is not clean. New
Python files and the new lifecycle frontend files were checked separately.

## Review limitations

The current branch contains a large, default-off change and needs review of its
complete diff, including migration behavior, write-entry inventory, background
jobs and default-off compatibility. Test results are tied to the source tree
and fixture used for each run; uncommitted files do not have a release SHA.
Do not describe a local review or synthetic fixture as an independent reviewer,
production deployment, live cloud acceptance, or completion of #144/#145.
