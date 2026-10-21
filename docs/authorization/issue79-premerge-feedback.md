# PR #98 pre-merge feedback closure

This follow-up starts from reviewed source `1d4f19fd71494778c4f7d8db253bbb53fa16406c`.
The merge preflight found five outstanding automated P2 suggestions. They were
checked against the implementation rather than accepted solely from bot text.

| Finding | Reproduction and correction |
| --- | --- |
| Capability Submission IDs | The bot's claimed 500 was not reproduced: DRF turned nonnumeric IDs into 404, while an empty value behaved as absent. Explicit positive bounded-integer validation now returns `INVALID_REQUEST` (400); valid inaccessible IDs remain scoped/not found. |
| History page retention | Loading another page removed earlier choices. Append/deduplicate by receipt ID, retaining earlier selections. |
| History order | UUID order could hide the newest receipt on a later page. Use descending `created_at` and ID, with a signed project/principal/order-bound cursor. A regression spans three real receipts and a creation-time tie. |
| READY replay recovery | An idempotent create replay or retry could return READY summary without artifacts, and polling stopped. Fetch detail for READY responses before restoring artifact controls; generation and current-receipt guards still reject stale responses. |
| Failed/superseded attempt scratch | Handled generation failures and superseded workers left private files behind. Remove only the directory created by that worker, after a locked database check excludes READY state or committed artifact references. Preserve other attempts, pre-existing directories and published files. Context-managed file handles also close if opening/writing fails. |

Cleanup uses standard-library `shutil.rmtree` with a resolved, exact
release/token-directory boundary. Database uncertainty or filesystem failure
retains files and logs the failed cleanup; an abrupt process kill still requires
separately governed orphan reclamation. This is not Host retention or revocation
policy and does not authorize deleting retained environments or data.

Verification evidence is retained under
`C:\Users\Henry\.codex\reports\ls-issue79-20261021\merge98-*`:

- Before fixes: three frontend regressions failed; invalid-ID handling and two
  scratch-cleanup cases failed. The history-order regression failed after its
  fixture generated each receipt normally to respect the active-job limit.
- After fixes: all four new PostgreSQL cases and all 26 related frontend
  component cases passed, including both languages and stale-response guards.
- A first broad local harness initialized Django before pytest-env applied test
  configuration, yielding CSRF login failures and dependent fixture failures.
  Full verification uses the standard pytest entry point and CI test settings;
  no product permission rule, test assertion or database constraint is relaxed.
  The existing positive Redis-client configuration test now declares its queue
  fixture explicitly, so running the full suite with the feature disabled does
  not depend on a runtime Redis URL. Its timeout/configuration assertions remain.
- Final current-head PR checks and the actual merge-SHA `push/main` checks must
  be verified separately; historical PR CI is not merge acceptance evidence.

The #80/Host and Core-only-review boundaries are unchanged. The feature remains
default-disabled. This follow-up does not deploy, start Core integration or
operate #76's session, worktrees or runtime. Only owned #79 test resources run.
