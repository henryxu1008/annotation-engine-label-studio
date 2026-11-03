# Review handoff focus lifecycle

This patch covers the shared classification, NER and field review-to-arbitration
history view. It addresses the retained field-handoff keyboard timing risk from
PR #132; it adds no dependency or server endpoint.

## Cause and behavior

The former passive effect depended on `busy`. When asynchronous history and
capability reads completed, the create button could be committed and receive
focus before that effect moved focus back to the heading. Refresh, denial and
window-blur completion also refocused the heading. A deterministic React
Profiler regression reproduces the commit-before-passive-effect interleaving:
the old view emits `H3 -> BUTTON -> H3` instead of retaining the chosen button.
The historical CI attempt did not capture an event timeline, so its precise
failure interleaving remains unproven.

Focus now follows navigation rather than network state. A layout effect focuses
the history heading when entering a source or returning from arbitration. It
runs during the DOM commit before the view becomes available for interaction.
Read completion, refresh completion, denial and blur do not move focus. Returning
to history still reloads current authorization and history; it does not reuse
stale rows. The existing return to the original review-batch control is retained.

The fix is local to focus management. The current authorization checks, request
generation fences, frozen-source validation, budgets, command recovery and
arbitration behavior remain in their existing implementations. Review decisions,
rework and publication retain their separate authorities. Fork opt-in settings
remain authoritative.

## Acceptance matrix

| ID | Required evidence |
| --- | --- |
| HF1 | Old production component fails the deterministic newly-ready-button and deliberate-focus regressions; failures identify focus movement. |
| HF2 | Classification, NER and field history retain the newly ready button's focus in en-US and zh-CN after asynchronous reads. |
| HF3 | Successful, denied and blur-fenced reads preserve deliberate focus outside the history view; denial/blur do not reveal a create action. |
| HF4 | A manual refresh does not steal subsequent user focus. |
| HF5 | Initial entry and return from arbitration focus the heading during commit, including while the returned history is still loading. |
| HF6 | Existing handoff authorization, stale-read fencing and return-to-batch/filter/focus component tests pass with the complete Label Studio unit suite. |
| HF7 | A fresh isolated synthetic database and current production build complete the full field-handoff journey in both languages. |
| HF8 | Native Enter produces exactly one trusted keydown and click on the intended focused AUT button, both for opening the workspace and confirming dispatch. No synthetic click, hidden retry or enlarged timeout substitutes for this check. |
| HF9 | Production build, catalog/literal guards and diff checks pass. Shared-file Biome diagnostics are compared with the identical baseline scope. |
| HF10 | Original local branches, primary checkout and previous evidence are preserved. No push, PR, merge, deployment, Docker/#76 or Issue closure is performed. |

Execution results and source binding are recorded separately under
`C:/Users/Henry/.codex/reports/ls84-handoff-focus-20261103`.
