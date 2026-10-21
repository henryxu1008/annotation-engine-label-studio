# Issue 76: submit failure recovery

On candidate `9bcb92bce78950ab5cb0f713ad4199837df06d6e`, a native Edge
rectangle edit reproduced data loss after both required-field validation failure
and a controlled HTTP 503 from the actual annotation POST. Width 16 was saved;
after the failures, edits to 22 and 26 disappeared after leaving and reopening.
No subsequent draft request ran. Chinese required-field guidance was English.

The shared submission marker was set before validation and never reset. It
disabled draft saves and suppressed DraftGuard. The draft was also discarded
before the API result, and the SDK event discarded that result. A failed new
annotation could consequently be classified as an existing annotation on retry.

The patch makes submit/update own their request lifecycle: validate first, keep
draft metadata and new-annotation identity on failure, reset the marker when the
request settles, and discard the draft only after success. Slow requests keep
duplicate submission blocked. SDK handlers return the API response, retain the
current task on failure (including network status zero), and exit only after
success. Standalone editor handlers without HTTP metadata remain compatible.
Review, assignment and authorization policy are unchanged.

Imperative required-choice validation uses the existing per-editor locale
runtime, shared through the MST environment. Locale changes do not recreate the
store. Custom configured required messages remain verbatim. Thrown submission
errors have localized retry guidance; HTTP errors retain existing SDK feedback.

Regression coverage includes submit/update validation and HTTP failure, rejected
requests and retry, preserved draft identity, delayed comments, slow submission,
SDK response propagation, in-place language changes and custom messages.
The bilingual Cypress journey owns a separate project/task and exercises
validation, failed submit, further editing, failed departure save, retry,
reopening and successful submission.

Fresh fixed-image build, native Edge acceptance and current-head PR CI are
separate results, recorded after execution in the local evidence directory
`C:\Users\Henry\.codex\reports\ls-issue76-submit-recovery-20261015`.
Older passing evidence does not qualify this change. PR 77 remains draft;
Issue 76 remains open. IAM operations/recovery and Core business integration
remain paused. No new Compose project, trust change, production deployment or
merge is authorized.
