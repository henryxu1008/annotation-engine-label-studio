# Issue 76: annotation entry and bilingual usability

Scope: annotation UI and English/Chinese usability in the existing local trial.
IAM operations/recovery and Core business integration stay paused. No new Compose
project, certificate-trust change, production deployment or merge is authorized.

## Observed functional failures

An active Annotator with an assigned task could open its row, but the normal
Label all tasks toolbar called the Manager-only Data Manager action endpoint.
Its 403 response then appeared as "All tasks in the queue have been completed".
Fix `next_task` as an assigned-queue read without opening other management actions:
check the authenticated role, intersect collection queries with `Task.objects.for_user`,
and recheck the sampled object's project and authorization. HTTP failures use the
existing bilingual modal with Retry and Back to task list. Reuse the existing
`display_context.reason` contract: only 404 with `QUEUE_EXHAUSTED` means no tasks
are currently available. A project/view 404 after revocation remains a failure.
Availability wording does not claim all work was completed: locks, cancellation
or filters can also leave no eligible tasks.
Also translate task statistics and their tooltips with the existing locale runtime.

The actual retained Edge context exposed another failure after installing the
initial fixed image `17960a9572e5d19f5bf49d78f54465186de6f93a`: a new versioned
main/runtime script loaded old, unversioned lazy chunks from memory cache. Its
`645.js` response SHA was `a6e911945cbe4ad4f15293f0565b3f5656b287670c8bed10c185074a8a931a16`;
the same live server and image supplied
`c00ae7d9bd1e20b0483b2f272d85d43c382bb62fda918dff7e2c96ea02d4eb4f`.
CDP `Network.requestServedFromCache` confirmed the old resource was reused.
Fresh Electron tests passed, so fresh-context acceptance alone missed this defect.

Use Webpack's content hashes for production lazy JavaScript and extracted CSS
filenames, and version the stable vendor entrypoint with the same backend revision
as main/runtime. Retain the existing entrypoint filenames and development behavior.
This avoids clearing users' caches or disabling caching. Standalone library modes
retain their existing packaging. These are upgrade-sensitive frontend build/template
patches and must remain covered by real artifact and returning-browser acceptance.

## Evidence boundaries

- Data Manager: 697 tests across 24 suites pass; before/after error-recovery controls
  show two failures before the fix and nine passing cases after it. The queue
  response contract separately reproduces two generic-404 failures with three
  passing controls before correction; all five then pass.
- Thirteen new server regressions cover role/scope, forged IDs and actor fields,
  revocation, existing locks and object revalidation during sampling. SQLite and
  PostgreSQL runs are required through current-head PR CI.
- Eight new browser cases use the actual assigned project fixture and real toolbar;
  they cover both locales, 403/generic-404 return-to-list and transient 500 retry
  with a real 200. The two 404 server regressions check that invisible projects
  do not emit the explicit exhaustion marker.
- Selection preservation, translated keyboard help and Escape were observed working;
  no editor shortcut defect was confirmed. Annotation content/label values are user
  configuration and are not automatically translated.
- Loopback browser acceptance, native image identity, current-head source CI and
  formal GitHub review remain separate. Old artifact acceptance cannot certify the
  new cached-browser correction. Export presentation uses synthetic responses and
  does not qualify every real converter or external storage provider.

Detailed local screenshots, raw independent reviews, build inputs, per-suite logs
and fresh artifact receipts are retained under
`C:/Users/Henry/.codex/reports/ls-issue76-editor-usability-20261014/`.
Returning Edge acceptance must use the same existing context and an ordinary reload,
without clearing caches, disabling caches or changing certificate trust.
