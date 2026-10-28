# Feature flags synchronization boundary

`Update Feature Flags` is inherited upstream automation. Its only sync job admits
the exact execution repository `HumanSignal/label-studio` through
`jobs.commit-feature-flags.if`. Forks and unknown repositories skip the whole job,
including debug, comments/reactions (even `always()`), git setup, checkout,
LaunchDarkly download, and commit/push.

| Entry | Fork | Exact upstream repository |
| --- | --- | --- |
| Existing weekday schedule | Skip | Existing behavior |
| Manual `workflow_dispatch` | Skip regardless of `inputs.ref` | Existing behavior |
| Reusable `workflow_call` | Skip regardless of ref or payload | Existing behavior |

The repository comes from GitHub's trusted `github.repository` context, never
`inputs.ref`, `client_payload`, owner alone, fork metadata, or secret availability.
For reusable workflows that context belongs to the **caller**, even when the
called file is hosted upstream. See GitHub's [job condition documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-jobs-with-conditions)
and [reusable workflow context](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations#github-context).

The schedule and both existing input contracts remain unchanged. Scheduled runs
use the default branch's workflow; they do not acquire the dispatch input's
`develop` default. Upstream's existing `GIT_PAT` and LaunchDarkly SDK key references
are preserved. This patch supplies no credentials, substitutes no `GITHUB_TOKEN`,
ignores no failures, and does not change `label_studio/feature_flags.json`.

## Verification without upstream synchronization

- Run `python -m unittest discover -s scripts -p 'test_feature_flags_workflow.py' -v`
  with the existing PyYAML dependency. These are structural policy tests, not an
  imitation of GitHub's expression engine. Check syntax with actionlint.
  actionlint 1.7.12 reports one pre-existing `events` diagnostic: the reusable
  `ref` is both required and defaulted. Retain that input contract and compare
  against this baseline; the new guard and probe must introduce no diagnostics.
- `Feature Flags Boundary` runs those tests for relevant pushes and PRs. Its
  reusable probe is separately restricted to this Fork, depends on the tests,
  has no token permissions, and forwards no third-party secrets. It calls the
  patched workflow at the **same commit**, with `ref=HumanSignal/label-studio`
  deliberately supplied as untrusted input. Never call the unpatched upstream
  workflow as a probe.
- Dispatch `feature-flags-update.yml` on the **patched Fork branch**. Inspect the
  exact SHA/run/attempt: `Commit Feature Flags` must finish `skipped`, with zero
  executed steps. Inspect the reusable child job the same way; an overall caller
  success or missing-token failure is not sufficient proof of skipping.
- Upstream admission is a static contract only; actual upstream synchronization
  is **NOT_RUN**. A skipped job is **NOT_APPLICABLE**, not a successful download
  (GitHub may summarize skipped checks as Success).
- After a separately authorized merge, inspect a new natural scheduled run on
  main. Keep historical failures; do not rerun an old unguarded workflow version.

## Scope and retained evidence

The guard protects this job in versions containing the patch. Older refs and runs
remain unguarded. Existing `/ff` dispatch/help and release workflows have separate
side effects; they are not disabled or used as test entry points. The independent
inline LaunchDarkly download in `docker-build.yml` is outside this patch. This is
not a repository-wide prohibition of every historical/third-party sync route.

The original scheduled failure on merged main `0d816ac670af16041b322317bb1c2387803eca89`
is retained as [run 40494196805](https://github.com/qq550723504/annotation-engine-label-studio/actions/runs/40494196805):
Checkout received no configured token; download and commit/push were skipped.
Adding a token would enable inherited upstream operations instead of fixing their
ownership boundary.

This work keeps #81 and #84 open. It changes no Host or product review, metric,
arbitration, sampling, or Core-only-review semantics, and authorizes no merge,
deployment, or Core integration. Docker and #76 are read-only. Historical
`CHANGED`, `CHANGED_DATABASE_CONTAINER_REPLACED`, cause `UNKNOWN`, and
`NOT_RUN_AFTER_DOCKER_INTERRUPTION` full-database comparison remain recorded;
a current container fingerprint match does not erase them or prove database equality.
