# Classification blind gold V1: boundary and implementation work

Status: **in progress, not enabled and not accepted**. The existing
[classification gold evidence](classification-gold-v1.md) is manager-only
and references a past submission; it is not itself blind dispatch.

The user selected quality-only treatment: a blind-gold submission must not enter
the ordinary review queue or any dataset-delivery route. The classification
score is not a review decision, approval, replacement of a Submission, or Host
release. This requires server and database enforcement, including bulk routes,
retries, prior drafts, reassignment, reviewer queues, and historical exports.
The user also selected new-member-only admission when the system can prove that
the member has not previously been exposed to the reference. Existing members
without reliable provenance default to ineligible; no operator guess is an
automatic substitute for that evidence.

## Identified constraints

1. The current reference belongs to a real previously annotated task. Its
   original author and anyone with prior access can know its answer; changing
   the current role or hiding a flag does not erase that knowledge. Any blind
   admission must exclude prior task participants and must not claim protection
   against previous privileged access, collusion or recognition of content
   without independently established provenance and an explicit threat model.
2. Task state, timestamps, identifiers, ordering and response latency can expose
   whether work is unusual. A new flag on only gold tasks or synchronous scoring
   only on a selected submission would create additional distinguishers. Review
   all participant task/list/queue/metadata paths and ensure ordinary and gold
   deliveries use the same protocol; quantify the residual timing leakage.
3. Project membership rows do not prove that a user never held privileged access
   or saw equivalent content elsewhere. An ordinary current role alone is not
   proof of independent exposure. A server-owned admission protocol needs a
   defensible provenance rule, explicit invalidation and fail-closed behavior.
   `User.date_joined` has a mutable default and is writable through the user
   update serializer, so comparing that field to a reference timestamp does not
   prove a new account. Existing membership rows can be removed or recreated.
   A prospective, server-owned, immutable account-creation evidence mechanism
   could admit accounts created after the reference; reference publication and
   account creation need a shared transactional ordering fence, not independent
   application clocks or sequence values that may commit out of order. Accounts
   without such an event and all bulk/import paths without equivalent proof must
   be denied. Membership role changes and delete/recreate paths also need a
   durable history: a current annotator role cannot prove no earlier privileged
   role. Forward-only account, role, task-assignment and file-access evidence is
   now prototyped, subject to the capture-continuity limitations below.
   Even such an event proves only this account's observed system history; it
   cannot establish that a physical person lacks another account or external
   knowledge. Host identity guarantees require separate evidence.
4. There are many review/export paths, including frozen batches and direct
   Submission access. A UI-only exclusion or a single queryset filter cannot
   guarantee quality-only treatment; an authoritative review/release predicate
   must cover every path and race, including already-created preview tokens.
   The editor persists an Annotation before the immutable Submission exists,
   so a flag or filter added only to Submission is too late: generic task and
   annotation exports, bulk actions, cached selections and storage sync must
   also exclude the work from its first visible write. A future implementation
   must bind the purpose before access and enforce it across both mutable and
   immutable representations, even after revocation or feature disablement.
   The worktree now has private, immutable assignment-purpose evidence and
   initial review/release checks. Mutable exports and storage paths now exclude
   quality task IDs while allowing unrelated ordinary tasks under the existing
   immutable-submission rules. Markers retain frozen project, task, assignment
   and assignee IDs without blocking deletion of the corresponding project.
   A marker can also pin a same-project reference ID and hash, which is copied
   on reassignment. Unbound markers exist only for internal tests; they cannot
   authorize dispatch or scoring. An internal validator checks the pinned
   reference manifest, classification controls, project configuration and task
   data against the existing frozen evidence and scorer. It is not an admission
   or scoring endpoint; its caller must lock and recheck the mutable scope.
   There is no production marker-creation or dispatch API. The marker
   insert guard rejects prior annotations, drafts and submissions, but by itself
   cannot prove that an already-visible task was never read. Atomic creation
   before assignment exposure, every review and delivery route, in-flight
   exports and cleanup behavior remain unverified; these checks do not authorize
   blind delivery yet.
   The marker is immutable at the database layer on PostgreSQL and SQLite;
   database guards reject ordinary review records and reviewed statuses for
   marked tasks. Review-batch
   planning and approved release loading use set-based marker checks; other
   comparison and handoff paths still need query-budget review before enabling.
   Annotation create, update and delete notifications now
   suppress the ordinary webhook for marked assignments, but bulk and alternate
   event routes still require an exhaustive audit.
The new handle-addressed annotation create/update routes suppress the ordinary
annotation webhook uniformly for both sampled and non-sampled work. Consumers
that depend on these events need an alternative contract before this workflow
can be enabled; the legacy paths retain their existing behavior.
   Initial binding requires an otherwise untouched task with a single
   assignment. A later database-level reassignment preserves the quality purpose
   and records the new assignee. A default-off internal service now verifies a
   pinned reference and the prospective local account before reassignment; there
   is no HTTP route, and the normal assignment service still denies it. The
   service can admit a certified member with unrelated past assignments, but
   does not establish that quality and
   ordinary tasks are indistinguishable. The PostgreSQL guard also locks the task when any
   assignment is created, including ordinary assignments while blind delivery
   is disabled. Contention and compatibility must be measured before merging.
   Future schema migrations can also remove these enforcement triggers, notably
   when SQLite rebuilds a table. Existing markers must remain enforced across
   upgrades even when blind dispatch is disabled; a migration/restore contract
   and regression tests for this case are still required.
5. Durable scoring needs the exact reference and Submission hashes and an
   idempotent retry key. A loss of worker/queue must not discard a score, block
   normal submission or reveal the selected cohort in an annotator response.
   Reuse the existing transaction and queue primitives, and test actual worker
   and recovery behavior. No scheduler is assumed installed in a deployment.
   An internal, idempotent scorer now validates and scores the frozen quality
   submission against its pinned reference using the existing classification
   scorer, and stores an immutable score receipt. The worktree now records all
   submissions in an opted-in or marked project in the same durable outbox and
   attempts asynchronous queue dispatch; ordinary submissions are ignored by
   the worker. Pending and stale deliveries can be retried by the bounded
   `reconcile_blind_scores` command. No public score API or deployed scheduler
   is configured. The scorer requires immutable assignment-time eligibility
   evidence and a current capture generation, but the full resource-access,
   timing and migration-continuity boundary remains unverified.

## Current security prerequisites

- Data Manager query predicates must not evaluate peer annotation values or
  private activity metadata on behalf of a non-manager. The current work adds a
  server-side fail-closed filter/order boundary and regression tests. This can
  restrict existing participant views that use private aggregate sorting; the
  resulting UX and compatibility need explicit validation.
- Queue serialization must use the authenticated server-mapped actor and its
  current assignment, including on lazy annotation stubs. The current work
  narrows this path and tests the mapped-actor case. Annotation retrieval and
  editing also need task/project/author consistency, not merely an assignment
  pointer; inconsistent links must not reveal another author's annotation.
- Task metadata still needs a complete exposure audit and participant-safe
  projection before blindness can be claimed. Current work scopes the visible
  completion and updater identity, but timestamps and other task-level fields
  remain unaudited. Existing tests for hiding raw annotation payloads do not
  establish indistinguishability.

## Required before enabling or merging blind delivery

Complete and review the random allocation and versioned ratio/seed contract;
separate the confidential mapping from participant-visible records; freeze the
assignment/reference before exposure; prove reference/actor isolation; and
guard stale assignments, source deletion, changed project configuration,
reassignment and retries. The quality-only boundary and durable scoring outbox
are prototypes, not evidence that delivery is ready. Test PostgreSQL concurrency, direct and bulk
authorization, list/queue/export/file paths, revoked/stale actors, both supported
locales in a real browser, and main/PR CI. No production data, identity provider,
or target deployment is covered by isolated synthetic evidence.

## Proposed forward-only account provenance

The default-deny admission rule requires new, server-observed accounts, not a
mutable registration date or the current ProjectMember row. A prospective
database-triggered account-insert and reference-publication event now use one
transactionally locked ordering fence; bulk inserts and transaction rollbacks
use the same path. Pre-existing references are conservatively recorded at the
migration boundary; old accounts are not backfilled. Missing events and
unsupported database vendors fail closed in the ordering check. This evidence
is internal and is **not an admission decision**. The migration adds a database
write to each new account even when blind delivery is disabled. Its triggers,
locking behavior, migration order and future changes to the user table must be
reviewed against upstream upgrades and deployment database permissions.

The worktree now records a conservative forward-only privilege history for new
accounts. Staff/superuser status, project or organization ownership, and any
non-annotator project membership mark the account permanently ineligible for
the future blind cohort, including when role rows are later removed or changed.
Missing history denies. This is intentionally stricter than current project
authority and includes disabled memberships. It creates additional writes on
account, project, organization and membership changes even while blind delivery
is disabled. Those database triggers must be preserved and verified on upgrades.
The prototype now records each task assigned to a certified account during its
capture generation, including assignments later deleted or inserted in bulk.
The internal v2 reassignment rule permits unrelated previous assignments, but
denies a member who was ever assigned the frozen reference's source task. A
later source assignment revokes v2 quality access and scoring. The history is
retained after assignment deletion and cannot be edited or deleted normally.
To avoid treating a duplicate text under another task ID as unseen, database
triggers also retain the byte lengths of top-level text values seen through
certified assignments, including subsequent task-data changes. Admission and
continued access reject a reference if any control text has a matching length;
unknown or oversized task data also denies. Equal lengths are only a
conservative approximation, so unrelated equal-length text causes false
rejections. Text itself is not retained by this evidence. The project should
measure capture cost and eligibility loss before rollout.
Only accounts certified after this migration can satisfy the current generation;
the migration ledger disables capture, and reactivation must advance generation.
This is evidence of task assignment history, not proof that all other routes to
reference material are closed. It is not yet wired into public dispatch.
An internal, default-off first-assignment primitive now creates the copied task,
assignment, immutable purpose marker, qualification proof and idempotency
receipt in one transaction. The same admission code is used for reassignment.
An internal sampler accepts an explicit basis-point ratio, chooses with the
standard-library cryptographic random source, and stores the result against an
idempotency key and ordinary assignment. An assignment cannot be re-sampled
under a different key. Only eligible references enter the random pool; a member
who already received a reference from the same source is excluded. This is not
yet a public route, a configured Host dispatch policy, or proof that ordinary
and quality work are indistinguishable to participants.
Deleting an individual task retains its receipt and makes replay fail closed;
deleting the project removes the receipt while retaining the minimal quality
purpose tombstone.
The internal records retain numeric account and reference IDs after deletion.
Account ID reuse preserves the original birth record and marks the replacement
ineligible for blind admission, even if its privilege history row was removed
while other birth or certificate evidence remained. The history's ordinary
update guard cannot reset that mark; deleting retained evidence or restoring an
incomplete backup requires a reviewed recovery plan. Reference ID reuse also
requires such a plan.
In particular, SQLite table rebuilds can silently remove triggers. The local
worktree adds a disabled-by-default capture generation: changes to the Django
migration ledger disable it, and re-enabling requires a new generation so old
account certificates cannot become valid again. Missing epoch or certificate
also denies. There is no production activation path. This is only a partial
maintenance boundary: migration-ledger changes happen after migration work, and
unrecorded DDL or a non-atomic migration can interrupt capture before the ledger
is updated. Before enabling blind dispatch, deployments must fence migration and
maintenance against admission before any schema change, verify all required
triggers, and invalidate capture if their uninterrupted history is unknown.
Checking that the triggers exist afterward cannot establish continuity.

The internal v2 admission locks and rechecks the account history in the same
transaction as assignment binding. Project upload, storage-proxy, local-file
and frozen bbox media reads now record a certified local account's prospective
file exposure before returning bytes or redirects. A file read anywhere in the
capture generation conservatively excludes that account from blind quality in
every project; this preserves ordinary reads but is stricter than proving a
specific reference was unseen. The record survives file and project deletion.
Changes to built-in import-storage configurations disable the capture epoch;
old certificates cannot regain validity when configuration is removed. Blind
quality currently denies all projects if any registered remote import storage
exists, or if the default upload backend is not the standard local filesystem.
This deliberately limits eligibility until direct links, cross-project aliases,
custom storage providers and historical access can be proven safe. Changes to
runtime storage settings, direct serving outside the application, and schema
maintenance still require an external admission fence before they begin; merely
checking the current configuration cannot prove uninterrupted past capture.
Other exposure routes and trusted identity mapping remain to be addressed.
Before public dispatch, the system must ensure later elevation cannot expose
the reference to an already active blind participant. The database prototype
rejects privilege elevation while a marked assignment remains active; canceling
the assignment first permits it. Application-level error handling, race coverage
and capture-epoch revocation are still needed before this is a usable policy.
In particular, invalidating the capture generation currently prevents scoring,
but does not revoke access through all task, annotation and related-resource
read paths. No public dispatch or reassignment endpoint may be enabled until
those paths enforce revocation consistently and race tests cover the change.
Quality labeling writes now recheck the exact admission proof and lock the
capture generation in the write transaction. Object-level assignment checks
also revalidate that proof; neither change closes every list, resource or
concurrent-response path. Cancelled quality assignments no longer grant the former assignee access to
submission history; ordinary cancelled assignments retain their existing
history behavior. This cancellation check alone does not handle an active
assignment whose capture generation or eligibility evidence is invalidated.
The task-list, personal annotation-work, participant submission-history,
visible-package and annotator package-progress queries now prefilter marked
assignments against the live epoch, certificate, assignment evidence and
provenance events. Their SQL predicate does not verify the proof's canonical
hash and is not a replacement for the object-level check. Other paths including
package metadata details and media still require review and
revocation coverage; do not enable blind delivery on this basis.
Ordinary project counters, queue counters, annotator and model-version lists,
and the cached project summary exclude marked quality tasks, including on
summary rebuild. Quality-task creation suppresses the summary update before
the marker is written in the same transaction. These ordinary counts exclude
quality work for managers too; separate quality reporting is still required.
For a participant, an opted-in or historically marked project now reports
personal active-assignment counts in the project and queue fields; its summary
uses configuration-derived columns and does not expose global label counts.
This applies before any sample is selected as well as after feature disablement
when retained markers exist. Managers continue to see ordinary project totals.
Participant task responses also suppress offline predictions and normalize
creation/update metadata uniformly for ordinary and quality tasks. Offline
predictions and model-version lists are unavailable to these participants;
managers retain access. This projection does not cover all data-manager
filters, ordering, nested identifiers or timing and therefore is not proof of
blind delivery.
The prediction API previously returned predictions across an organization.
It now scopes reads to live project membership and the actor's current task
access, and restricts writes to a locked manager scope. Earlier access through
the old endpoint cannot be reconstructed from assignment history. Therefore
any deployment that served the old endpoint after capture began must invalidate
that capture generation and admit only accounts created after a fully fenced
upgrade; running mixed old and new web processes is not a valid capture epoch.
The prior interactive-ML endpoint also allowed project members to name an
unassigned task. It now checks current task access. Ordinary batch, debug and
interactive prediction paths exclude quality tasks before any remote call.
Model training is denied for a project with retained quality markers: the
existing backend integration can either transmit every annotated task or let a
backend use project-wide credentials to fetch them. This is a deliberate
compatibility limit until a quality-excluding training contract exists.
The upstream ML setup protocol also hands a project owner's API token to the
backend. To avoid a cross-project token bypass, admission and read eligibility
now deny while any ML backend is configured, and database triggers invalidate
the capture epoch on backend insert, update or delete. Setup refuses to send
an owner token while blind mode is enabled or a live project has quality
markers; retained tombstones for deleted projects do not block setup forever.
These controls do not revoke previously issued tokens or prove they were
rotated. Blind delivery must remain disabled until those credentials are
rotated or replaced with a scoped contract and a fresh capture epoch starts.
Project task-data credentials were also returned to non-managers by the
project serializer. They are now omitted from non-manager responses. Any
remaining configured task-data login or password, including one on a retained
project, denies blind admission and access globally; changes invalidate the
capture epoch. This deliberately conservative limit is necessary because
credentials may reach data shared between projects. The migration disables the
old epoch, but cannot rotate credentials already disclosed or prove old access
was erased. Rotate or remove the exposed credentials, fence all old processes,
and start a fresh capture epoch before admitting eligible accounts.
Organization-wide labels and label links expose shared metadata and annotation
counts even before an account joins a project. Their API requests now record an
immutable, generation-bound exposure before responding; any such exposure
disqualifies that account from blind admission and revokes existing quality
access. Migration 0039 invalidates the old capture epoch because earlier label
reads were not recorded. Fence all old web and worker processes before starting
a fresh epoch. Request-wide database transactions are unsupported and deny
quality access, because exposure recording must commit before label handlers
acquire project locks. Ordinary bulk label changes exclude quality-only annotations.
Project-scoped changes require a live manager; organization-wide changes require
management access to every included project and reject instead of silently
performing a partial update. The scope is locked before writing.
The project annotator directory previously excluded only quality annotations:
a participant could distinguish a first ordinary submission from a quality
submission by observing whether a user appeared. In a sensitive project it now
returns a constant empty list to non-managers, while managers retain the
ordinary-only directory. The read holds the project and membership fence and
does not cache the response. Organization membership contribution lists and
counts now omit *all* work in sensitive projects, for every caller, while
retaining contributions in other projects. This intentionally limits the
organization-level statistics available to managers; it prevents correlating
the aggregate with the ordinary-only directory. These projections must be in
place before capture starts, including in all web processes and caches. They do
not establish that previously exposed statistics can be forgotten.
Project summary reads now require current project membership and a project
operation fence before serialization. Sensitive-project participants receive
the conservative summary projection; managers retain the full ordinary summary.
The response is private and not cacheable. Existing organization-level
contribution summaries for sensitive projects are deliberately unavailable to
managers as well as participants and need a separate privileged reporting
contract if operators require them.
The internal `create_mixed_assignment` primitive now creates and samples the
ordinary assignment in one transaction. Participant task and work queries defer
its ordinary source while the selected sample is accessible. A successful
quality submission atomically records immutable completion evidence and cancels
the source assignment, making the ordinary task available for a later normal
assignment instead of immediately showing it to the same participant. A source
remains hidden after sample cancellation or eligibility revocation until a
manager cancels or reassigns that work; it does not suddenly reappear to the
same participant. Mixed reassignment cancels the abandoned source atomically.
Previously exposed ordinary tasks fail closed, and tasks in existing fixed
packages remain ordinary to preserve package membership. A cancelled or moved
source also invalidates the selected sample. These are internal checks, not an
integration into the public assignment API. Task IDs, timestamps, aggregates,
package creation after selection and other metadata still require an exposure
audit; this is not accepted blind mixing.
An internal opaque-handle ledger now issues independent, randomly distributed
JavaScript-safe task and assignment identifiers for either ordinary or quality
assignments. Handles are retained and immutable, bind the exact actor, task,
project and assignment, and resolve only while the live assignment remains
authorized. A separate participant work-list and editor-read endpoint now
return opaque task, assignment, annotation and draft handles for current work;
the existing editor and legacy routes have not been switched to this contract.
The handle-based reader returns only text fields referenced by the supported
classification configuration, rather than unrelated task-data metadata. It
cannot conceal an answer embedded in text the configuration explicitly shows;
reference authors must avoid including such content.
Dedicated annotation and draft write routes accept opaque task, assignment and
resource handles and reuse the existing writers; the original routes still
accept raw IDs and must be fenced before blind dispatch. Any write using a handle must
resolve it again under the existing project, assignment and eligibility locks;
the handle itself is never authority. Every participant route and nested task,
assignment, annotation, draft and submission representation must use a uniform
contract before raw identifiers can be hidden safely. Project and personal
progress, queue ordering, creation and submission timing remain unresolved.
An isolated PostgreSQL measurement of the current editor-read path, before the
shared epoch-read fence was added, found 181 SQL queries for a sampled task and
46 for an ordinary task. In 14 warm local samples their medians were 113.5 ms
and 40.0 ms respectively; this is not a deployment benchmark or a statistical
security guarantee. The additional quality-proof checks make this a concrete
timing distinction. A fixed sleep or random delay would not establish
indistinguishability. Public mixed dispatch remains disabled until a common,
bounded authorization/read path and an agreed residual leakage test are in place.
The editor and work-list now avoid repeated validation of the same locked
assignment, and the global storage and eligibility predicates use fewer SQL
round trips. A second isolated warm read measurement after those changes and
the task/assignment lock restoration found 37 queries and a 29.6 ms median
for quality work versus 20 queries and 16.0 ms for ordinary work (14 samples
each). These measurements use the same synthetic fixture and are not a
production benchmark. The remaining difference still blocks public dispatch.
After adding current-content binding, a further local run using a newly admitted
synthetic account and the same reference measured 38 queries and a 29.2 ms
median for quality work versus 20 queries and 16.3 ms for ordinary work (14
samples each). This was measured before the additional work-list prefilter;
it does not establish indistinguishability or characterize deployment latency.
The work-list prefilter and object authorization now share the same SQL checks
for mixed-work source, reference binding and released-source completion. The
object path still independently checks the immutable proof and reference hashes;
a regression explicitly demonstrates that SQL visibility alone cannot replace
that integrity check. Both supported proof rules now also reject a later
matching-length text exposure at object authorization, consistent with the
collection prefilter. A legacy first-assignment proof receives the same live
revocation check as the unseen-source proof; matching lengths are a conservative
exposure signal and can revoke access even when text contents differ.
After consolidating the remaining mix checks, paired warm
reads of the same synthetic assignments still measured 33 SQL queries and a
33.8 ms median for quality work, versus 20 queries and a 16.1 ms median for
ordinary work (14 samples each). The observed ranges did not overlap. These
local timings are diagnostic, not a production benchmark or acceptance test.
The next unified-read design must distinguish immutable evidence from live
authority. The reference and eligibility manifests can be hash-checked when a
server-owned read binding is issued, only if its binding and the underlying
evidence remain immutable and migrations invalidate old bindings. Every read
must still enforce the current actor, membership, assignment/version, capture
generation, source/completion, project configuration and task content, as well
as file, label, text-length, storage, model and credential exposure. Moving the
hash check alone into a cache is insufficient: the current SQL prefilter does
not verify all of the immutable evidence. A shared permit or precomputed live
projection would need a transactional invalidation path for each of those
changes and the same bounded lookup for ordinary and sampled work before it can
replace the current checks. No such permit is implemented or enabled here.
An additional paired local HTTP measurement from a browser against an isolated
server, using another newly admitted synthetic account, also showed nonoverlap:
quality reads had a 114 ms median (110–133 ms), while ordinary reads had a
93.5 ms median (86–107 ms), with 14 warm samples each. No production network or
load was represented. This reinforces the default-off gate; a successful
functional browser test does not establish timing privacy.
Participant work-list and editor reads take a shared capture-epoch lock after
the project lock. This lets concurrent readers proceed while serializing each
read with exposure recording and revocation; the current proof is rechecked
inside that fence. The fence does not remove the timing distinction.
Any transaction spanning multiple projects must lock all involved projects in
stable ID order before it acquires the capture-epoch lock. A synthetic fixture
previously acquired a second project after holding the epoch, which deadlocked
with a concurrent participant read; its seed command now locks the complete
fixture project set first, and a PostgreSQL concurrency test covers that order.
Future public dispatch must not wrap arbitrary cross-project commands in one
transaction unless it enforces the same ordering.
The data manager has a default-off `blindProtocol` request adapter for the
handle-addressed task and write calls. Project state and column bootstrap now
use dedicated server-authorized participant endpoints, and the personal work
API returns only configured text and opaque handles. The adapter rejects every
operation outside its small allowlist rather than falling back to raw-ID APIs.
The restricted store loads and validates the personal list for the bound project,
clears prior results before refreshing, and ignores stale responses after a project
or request change. Request failures leave the list empty and end loading. These
checks alone do not prove immediate revocation of an already displayed editor.
For a blind editor, navigation waits for an in-flight submission or update and
its refresh; failed writes keep the editor open. A denied submission or update
closes the stale editor. For a missing draft, the client checks task access
before closing, preserving draft-conflict recovery when the task is still
available. Browser regressions cover revoked updates and draft writes in both
languages. There is no push channel that removes
already-rendered data at the instant a server-side eligibility change occurs.
Current assignment access also checks that the live project configuration and
task data still match the pinned reference. The participant work-list applies
the same content and configuration check before pagination and counting, so a
stale sample does not turn the entire list into an error. Scoring stays tied to
immutable submission and reference snapshots and does not require the live
project or task to remain unchanged after submission.
The disabled participant protocol also applies a common 256 KiB limit to task
data before listing or reading it, and bounds configuration, annotation results
and drafts before use. Oversized tasks are omitted before pagination and count;
existing oversized results are unavailable and oversized result writes fail.
This limit applies to ordinary work too and is a compatibility constraint for
any future rollout. Authorization is checked again under the project, capture,
task and assignment locks on each read or write; it does not push revocations
into an already-rendered page. Writers acquire the capture epoch exclusively
before task locks, avoiding shared-to-exclusive lock upgrades across projects;
this also serializes participant writes across the capture epoch and needs
throughput evaluation before any rollout.

The bounded reads do not eliminate the timing side channel. On an isolated
PostgreSQL synthetic fixture, 14 warm paired API reads after two warm-ups had
median 32.4 ms (28.4–38.8 ms; 33 queries) for a quality assignment and 15.5 ms
(14.1–21.3 ms; 20 queries) for ordinary work. These diagnostic measurements are
not a production threshold or an acceptance test. The quality-only proof and
live exposure checks remain distinguishable. Do not expose mixed dispatch until
the authorization path is genuinely uniform and its timing is validated.
After adding the legacy proof revocation check, a fresh isolated PostgreSQL
fixture still measured 37.4 ms median (32.2–42.4 ms, 33 queries) for quality
and 17.0 ms (14.7–19.2 ms, 20 queries) for ordinary work, across 14 warm paired
reads. These diagnostics confirm that revocation alignment alone does not close
the timing gap. After factoring the shared exposure predicate, another fresh
fixture measured 32.2 ms (28.8–38.9 ms, 33 queries) versus 15.5 ms (14.2–21.1
ms, 20 queries), again using 14 warm paired reads. The gap remains a blocker.
Consolidating global storage, model and credential fences into the object
authorization query reduced the sampled path to 32 database queries. A fresh
paired run still measured 42.7 ms median (34.4–64.0 ms) for quality versus
20.8 ms (17.8–30.4 ms, 20 queries) for ordinary work, with 14 samples each.
This change shares the live fence predicates but does not unify the complete
ordinary and quality evidence paths or establish timing privacy.
For repeatable local diagnostics, `measure_classification_blind_e2e` accepts
the two outputs of the isolated synthetic seed commands, alternates the two
reads and reports aggregate latency and query counts without returning handles.
It requires `TEST_ENVIRONMENT`, the explicit synthetic fixture flag and the
synthetic database name. It is not a production measurement or an acceptance
threshold; the command cannot prove the absence of a timing side channel.
The existing personal-work flow can consume that store and open the editor by
handle when its caller explicitly supplies the disabled protocol mode. A
synthetic browser harness has exercised ordinary work in English and Chinese,
including initial submit, an update and assignment-version advancement. A
separate synthetic fixture uses the internal sampler to give a newly eligible
member both quality and ordinary work, and exercises their common opaque editor
and submission in both languages. This test explicitly constructs the disabled
protocol; it does not activate dispatch or the interface for normal users.
The protocol must remain disabled until the remaining legacy-route
fencing, mixed dispatch and browser tests are completed together. The
common API proxy also rejects direct legacy and raw
calls while the mode is active, including calls that bypass the store. This is
client-side defense in depth; the shared form also refuses raw `fetch` while
the guard is active and does not report rejected requests as successful. Other
application surfaces still require server-side enforcement and must not be
treated as fenced.
Its project-state endpoint now projects personal counts for participants, and
the column endpoint removes unconfigured data fields and prediction model
versions. Legacy task detail, nested annotations and drafts, annotation and
draft detail, project task list, next-task and annotator work/package routes
deny non-manager participants in sensitive projects. The legacy task-list shell
is available to reviewers with an empty task queryset under a membership lock,
so the ordinary review workspace can open without leaking participant tasks.
Legacy task detail, task-list, next-task, stream-history and project-task-list
requests now lock the project and recheck live membership before deciding
whether the project is sensitive. This orders those reads against quality-marker
creation, including transitions from an ordinary project. The lock is held
while the response is assembled; ordinary task paths that perform external
prediction evaluation can therefore increase lock hold time, which needs
throughput and timeout review before rollout.
Saved data-manager views can carry raw task IDs in filters and selections;
annotators cannot list, retrieve, create or move them into sensitive projects.
Reviewers retain project-scoped view access under the same membership fence.
Reviewers can review ordinary submissions through the project-scoped review
queue; quality submissions remain excluded. Raw submission history and detail
are unavailable to annotators in sensitive projects. Reviewer reads from such
projects require explicit project scope and hold membership locks for scoped
list and detail serialization. Managers retain legacy routes. The conservative
restriction still affects ordinary annotation work in an opted-in project and
legacy reviewer task-detail access; it is not a complete compatibility solution.
Other routes, concurrent changes
to project sensitivity and participant-facing lifecycle still need an audit.
Do not enable dispatch until the entire server boundary and ordinary review
workflow are verified together.
The sampler's low-level choice function is private: selecting an already
visible ordinary assignment cannot establish blindness. Direct reassignment of
a task selected by a mix decision is rejected, because the existing direct
reassignment primitive cannot bind the replacement to a fresh ordinary source.
A future reassignment flow must create and validate that binding atomically;
the rejection itself is not a mixed-reassignment workflow. The internal
`reassign_mixed_assignment` prototype now replaces a cancelled, unsubmitted
sample with a fresh atomic mix slot for a separately eligible member, preserving
the frozen reference. A unique, immutable predecessor link permits at most one
successor and retries use the original command key. It has no public endpoint
and does not by itself resolve identity, timing or statistics side channels.
Different applications or independently mapped principals may not supply this
proof; unsupported identity providers must fail closed until their contract is
verified. One account is not proof of one human or of no knowledge acquired
outside this system.

Unselected and selected assignments must share a participant-visible lifecycle;
the outbox path is shared by every submission in a marked or opted-in project.
An isolated PostgreSQL and Redis test exercises scoring in a separate worker
process, including a mixed quality submission. Recovery across deployed queue
and worker processes must still be validated. The persisted selection
must bind the project, exact assignment generation and reference hash before
the assignment becomes readable. Review/release exclusions must be enforced
even when the feature is disabled or the assignment is later revoked. Failure
of scoring must not roll back ordinary annotation writes. Queue failures leave
outbox rows pending, but operator scheduling, end-to-end queue recovery and
explicit degraded behavior still require validation.

The optional Compose/systemd schedule in `deploy/blind-score-reconcile/` uses
the same one-shot pattern as bbox reconciliation. It runs a bounded retry page
every five minutes, using the deployed app service's database and Redis settings.
A worker must also consume the configured approved-export RQ queue. Installing
the example timer, validating permissions, inspecting service failures and
verifying the actual worker are deployment operations; the source files do not
enable them. Backlog and permanent invalid rows require operator monitoring.
The isolated Redis test uses a real RQ simple worker, but does not prove a
separate production worker, systemd timer or live deployment is operational.
