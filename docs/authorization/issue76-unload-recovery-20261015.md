# Issue 76: refresh and tab-close draft recovery

On installed candidate `92aae5dc6e483af08e0f519ece53bac3bac7b8c9`, native
Edge reproduced loss of a changed required choice after an autosave failure:
a genuine reload discarded it without warning; closing the tab also discarded
it without warning. Reopening both tasks returned an empty result. These were
fresh synthetic tasks, not retained annotation data.

DraftGuard protected SPA navigation only. Refresh/close cannot wait for that
asynchronous save path. The patch adds a browser `beforeunload` check using the
existing editor bridge. It compares the current tracked state with the snapshot
acknowledged by a successful draft response (or the initial loaded history).
Pending saves/submissions and failed saves stay protected. An older response
cannot mark a newer edit saved; undo/redo compares content, not the history cursor.
Read-only annotations and standalone editors without a draft handler are excluded.

The editor bottom bar exposes localized unsaved/saving/failed state, refresh/close
guidance and a save/retry button. It reuses the existing serialized draft flush;
there is no second persistence backend or unload-time fire-and-forget save.
The browser owns the native warning text and requires prior user interaction.
Cancelling the warning preserves the editable page. After the server confirms
the retry, refresh/close is allowed and reopening restores that confirmed draft.
Choosing to discard unsaved edits, force-killing the browser or losing power is
not a guarantee of recovery. Application text follows LS locale; native warning
text follows the browser, not LS locale.

Regression coverage includes stale acknowledgments, failed/unconfirmed responses,
retry, undo/redo, listener cleanup, locale changes and bottom-bar feedback. The
bilingual Cypress journey verifies event cancellation, a real retry and clean
reload restoration. Cypress auto-accepts unload dialogs; native Edge cancellation
and real tab closure are separate acceptance steps, not inferred from that test.

Executed build, browser and exact-head CI results are recorded separately in
`C:\Users\Henry\.codex\reports\ls-issue76-unload-recovery-20261015`.
Prior passing reports do not qualify this change. PR 77 remains draft and
Issue 76 remains open. IAM operations/recovery and Core business integration
remain paused. No new Compose project, certificate trust change, production
deployment or merge is authorized.
