# Issue 76: entity and rectangle editor recovery

Scope: the existing local synthetic trial only. IAM operations/recovery and Core
business integration remain paused. No new Compose project, certificate-trust
change, production deployment or merge is authorized.

## Reproduced product blocker

On candidate `d16993ba`, a normal assigned annotator created Person/Location
entities in the Chinese editor. While the test tab was offline, deleting Location
and leaving through Projects produced no save failure feedback. After reconnecting,
a second Projects click navigated away while the persisted draft still contained
both entities: the latest deletion had not been saved.

The rejection from the submitDraft event was not propagated through AppStore.
Annotation therefore never completed its save or cleared isDraftSaving. An
immediate flush returned an empty object during an in-flight save; DraftGuard
treated the missing status as permission to leave. SDK task navigation also
synthesized a successful status after waiting for saving to end.

## Small upstream-sensitive patch

- Propagate event rejections, serialize draft requests, and clear saving in a
  finally block. An explicit flush waits for autosave, then captures the latest
  edits. Save timestamps describe the captured revision rather than the response
  completion time. Failed HTTP responses do not advance the saved timestamp.
- Keep breadcrumb and next/previous task navigation blocked on failed or
  unconfirmed saves. Give localized failure feedback and allow an explicit retry.
- Translate the actual image Move, Pan, Zoom In/Out, Fit and Actual Size guidance
  through the existing locale runtime. Retain upstream automation identifiers;
  labels supplied by project owners are business data and remain unchanged.

Negative regressions reproduce network rejection, HTTP rejection, an unconfirmed
result and edits made during an autosave. Image tool tests switch locales live
while preserving selected state and actions. This patch changes no authorization
policy, schema or submission/review semantics.

## Artifact acceptance boundary

Unit/source results do not qualify a rebuilt artifact. The local acceptance report
must bind the fixed image to its source and cover English/Chinese text entities and
rectangles through actual pointer/keyboard input, draft leave/reopen/reload,
offline failure/retry, and formal submission. Check persisted text offsets,
rectangle geometry and actor identity separately from screenshots. Retain the
failed d16993ba observations as failure history rather than rewriting them as PASS.

## Undo-to-initial follow-up

The `c77261c9` fixed image reproduced another ordinary editing failure: after a
single text entity had autosaved, Undo removed it, but immediately leaving and
reopening restored the old entity. The server draft still contained the entity.

The undo cursor is an editing position, not a dirty-state check. DraftGuard and
SDK selection now use the existing history.hasChanges contract, including index
zero. TimeTraveller updates lastAdditionTime on undo/redo so the existing draft
timestamp check also saves these changes when switching tasks. No extra history
states or new persistence mechanism are introduced.

Regression coverage includes the failed guard at index zero, undo/redo timestamps
without new history entries, SDK selection, and both-language browser journeys
that autosave an edit, undo to the initial result, leave, reopen and verify the
persisted result. The candidate must obtain new native evidence; earlier manual
and CI results remain bound to their original sources.
