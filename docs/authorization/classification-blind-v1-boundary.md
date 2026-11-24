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
   Manager command routes for mixed dispatch and reassignment are wired to the
   internal services but remain hard-disabled pending the timing and resource
   boundary acceptance described below. A default-off setting is an additional
   gate, not an override of that release block. Replacement preserves the
   original decision: selected work retains its frozen reference and unselected
   work reuses its original ordinary task, without another random draw. Both
   require cancellation, a current authorized recipient who has not seen the
   ordinary source, and one idempotent successor; selected work also requires
   the existing reference eligibility proof. The successor records a forced
   0/10000 continuation, not a new sampling probability; the predecessor
   retains the original ratio and decision for audit.
   Neither the command routes nor their service tests constitute a browser workflow. The marker
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
   pinned reference and the prospective local account before reassignment; the
   mixed HTTP command remains hard-disabled, and the normal assignment service
   still denies direct quality-task reassignment. The
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
On an internal mixed-decision retry, the saved decision is only an idempotency
record: both ordinary and quality results must still pass live participant
authorization under the capture read fence. A cancelled assignment, revoked
member or invalid quality manifest therefore fails closed. The SQL quality
prefilter is not used as a substitute for object-level hash verification.
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
An internal mixed slot now receives an immutable read binding in the same
transaction as its decision, whether the selected result is ordinary or
quality work. New ordinary assignments created through the assignment service
in a sensitive project and new standalone quality assignments use the same
binding model; ordinary text stays editable. The binding records decision and
source identities when present and, for quality work, the verified reference
and eligibility-proof hashes. Handle validation checks it after the existing
live authorization; it does not replace proof, membership, exposure or content
checks. A pre-existing mixed decision without a binding is unavailable until
its manager replays the original idempotent command and the checks succeed.
Legacy non-mixed assignments and direct assignment inserts still use the old
path until a safe migration exists. The additional binding does not establish
uniform timing or authorize public dispatch.
New bindings also receive a Django-signed, immutable static attestation after
the complete quality proof and reference have been checked. It contains only
proof metadata, hashes and text lengths, not task text or answers. The current
read path verifies the signature and still performs all original live and
hash checks; legacy bindings without an attestation continue on that original
path. A forged or oversized attestation is denied. Deployments must keep
signing keys consistent across web and worker instances and retain old keys
in Django's fallback list during rotation; removing a key invalidates its
attestations, and re-signing an immutable row is not yet supported.
Binding and signed attestation are now loaded in the same bounded SQL query
for both purposes. Absence remains a legacy fallback; an oversized row is not
treated as absent. The collection SQL prefilter also does not verify that the
reference text-length records exactly match the frozen reference. The object
validator checks that set and matching exposure together in one database
snapshot. The SQL prefilter does not verify the manifest hashes or replace
object authorization. A trial of sharing the complete list query for object
reads made the measured timing gap larger and was not enabled.
Active assignment lookup now reads the quality marker and its scope with the
assignment for both ordinary and quality work. Bounded proof and reference
contents are fetched with their size checks, and live eligibility, exposure,
reference and proof existence are checked in one later snapshot. These changes
retain the full proof and reference hash checks and do not alone establish
uniform participant timing or authorize public dispatch.
For the handle paths that already hold project and task locks, authorization
reuses those locked objects after checking that the assignment belongs to the
same project. Other callers continue to load their own related objects.
On handle reads and writes that also hold the capture-epoch fence, both signed
ordinary and signed quality work now use the same live work-check entry point.
It rechecks active user, assignment and binding; quality work additionally
checks its signed static facts and current certificate, exposures, reference,
proof and mix history. It depends on the database immutability and evidence-ID
guards installed by the preceding migrations. Missing attestations, historical
scoring and unfenced callers retain the full existing proof path. Nested
resource writes reuse the already-authorized locked assignment and still
check the opaque resource's actor, assignment, task and project bindings.
The optimized path requires a transaction-local witness returned after locking
the epoch row; witnesses from exited atomic blocks cannot be reused. It uses
Django's connection atomic-block and commit-hook state and must be checked
during upgrades.
This remains default-off: equal query counts do not establish timing privacy,
and no public mixed dispatch or reassignment is enabled by this change.
An isolated PostgreSQL diagnostic with 100 warm paired reads measured 19
queries for each purpose, but ordinary and quality medians were 27.2 ms and
39.3 ms; the empirical rank probability was 0.9896. This is not a privacy
acceptance test and is evidence that the current paths remain distinguishable.
For retained active assignments, a project manager can explicitly POST up to
50 `items` to `gold/legacy-bindings/` when both gold and blind project flags
are enabled. Each item pins `id`, `task_id`, `assignee_id`, `version` and the
timezone-aware `assigned_at` value from the assignment. The server rejects a
stale snapshot before and after locking the project, affected members, capture
epoch, tasks and assignments; rechecks each assignee's current authorization;
and seals ordinary, mixed or quality evidence only after its existing checks
pass. The request is all-or-nothing and can be replayed. It cannot reconstruct
missing eligibility history or make an unqualified member eligible. This
repair endpoint does not enable participant dispatch or automatically migrate
all existing work. Manager-only GET lists active assignments missing a binding
or attestation in pages of 50 using `after`; it does not prove that existing
signatures are valid or that a project is ready for blind dispatch.
The capture-identity migration retains every known account ID independently of
the deletable capture certificate. A certificate can be issued only at the
first recorded birth; deleting a certificate or provenance event cannot make
an existing or reused account eligible again. Account birth while capture is
disabled also records the identity and cannot be certified retrospectively.
The migration disables capture and backfills identities from existing users,
history, account events and certificates. It cannot reconstruct identities
whose user and all three evidence records disappeared before the migration.
Run this migration with account and blind-work writes quiesced: its epoch and
table locks otherwise can contend with account-birth triggers. The migration
does not schedule that operational pause or activate capture afterward.
Rollback disables capture and refuses to discard an identity if no remaining
user or historical record could reconstruct it. Re-enabling the previous
implementation after rollback would restore its weaker certificate checks.
This guard does not implement the common live-read projection or establish
timing privacy.
An additional retained evidence state records account and reference provenance
events and account privilege histories. Deleting one atomically retires its
identity; the retired state cannot be reset or deleted, and recreating the old
event or unprivileged history is rejected. A reused account ID can still be
created for ordinary use but cannot regain eligibility from retired evidence.
The migration disables the current capture generation and backfills evidence
that still exists; it cannot reconstruct rows deleted before migration. Run it
with account, evidence and blind-work writes quiesced. Rollback refuses to
discard retired identities. This is one revocation dependency, not a complete
shared work-read state or a timing-privacy guarantee.
For a completed mixed slot, the completion receipt depends on the retained
submission and its actor. PostgreSQL updates or deletes of a referenced
submission take the same exclusive capture fence as other live revocations;
an in-flight reader therefore completes before that mutation can commit, and
the next read rechecks the completion. This trigger does not take the fence for
unrelated submissions. SQLite readers use the existing write reservation.
The ordinary-binding schema migration disables the current capture generation
and restores the database guards after the schema change. Once non-mixed
bindings with null decision fields exist, rolling it back requires a separate
data and compatibility plan; a failed reverse must not be treated as recovery.
An isolated PostgreSQL run with 14 warm paired reads after this change measured
quality work at a 48.1 ms median (44.2–58.4 ms, 32 SQL queries) and ordinary
work at a 22.5 ms median (20.5–32.8 ms, 20 SQL queries). The ranges did not
overlap in this synthetic sample. These observations are diagnostic rather
than production or statistical acceptance evidence; the separate quality
proof path still prevents public rollout.
After extending the binding to service-created ordinary and standalone quality
assignments, another fresh isolated PostgreSQL run measured quality at a
43.6 ms median (40.6–54.2 ms, 32 queries) and ordinary at 20.2 ms
(18.8–24.3 ms, 20 queries), again with 14 warm paired samples. This confirms
that sharing the binding has not eliminated the separate quality-read cost.
Gold evidence identities now have a retained, immutable registry. Its migration
backfills live evidence and IDs retained by quality markers or reference
provenance, and disables the existing capture generation. A new evidence row
cannot reuse a registered ID, even after project or provenance deletion. The
registry stores only IDs; project deletion still removes the evidence payload.
This closes one prerequisite for a future verified read binding. It does not
validate legacy hashes, prove a database restore retained the registry, or
replace the current per-read proof and reference validation.
New assignment inserts are also rejected if their numeric ID already belongs
to a retained opaque handle, so a deleted assignment cannot simply rebind that
handle. This does not establish whether an assignment ID was reused before
the guard was installed; existing handles still need a continuity and upgrade
strategy before this can serve as the common durable authorization evidence.
An additional paired local HTTP measurement from a browser against an isolated
server, using another newly admitted synthetic account, also showed nonoverlap:
quality reads had a 114 ms median (110–133 ms), while ordinary reads had a
93.5 ms median (86–107 ms), with 14 warm samples each. No production network or
load was represented. This reinforces the default-off gate; a successful
functional browser test does not establish timing privacy.
Participant work-list and editor reads take a capture-epoch lock after the
project lock. PostgreSQL readers share this lock; deleting a certificate,
privilege history, provenance event or reference takes its conflicting lock even when
issued directly through the ORM or SQL.
If the capture-epoch row is missing, blind participant reads and writes fail
closed for ordinary and quality assignments instead of proceeding without a
fence. A disabled but existing epoch retains its lock and its separate
eligibility behavior.
SQLite readers instead reserve the write lock: a plain read cannot fence a
concurrent commit in WAL mode. That serializes SQLite writers and participant
reads and requires throughput evaluation. The current proof is rechecked
inside the fence; this does not remove the timing distinction or prove that
every other revocation source takes the required lock.
PostgreSQL assignment insertion/rebinding and task-data updates now also take
the exclusive capture fence before their exposure triggers run. Assignment and
manager task-update services acquire it after the project lock and before task
locks when capture is active; blind sampling takes the exclusive fence before
its task and reference locks. Insertions of assignment, file, label, text-length
and reference-length evidence also take that fence when issued directly.
Direct database writes still need a compatible
lock order: a database-detected deadlock rolls back the transaction and is not
a substitute for correct ordering. The global write fence also needs throughput
and contention evaluation before rollout.
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
Direct handle writes and transitional handle adapters use the same exclusive
epoch fence before locking a task. The `lock_handle_for_write` check validates
the locked assignment once, including the full quality proof where applicable;
the enclosing editor adapter still has its own entry and response checks. This
does not yet make the evidence or timing of ordinary and quality reads uniform.
After a write, the adapter re-locks the bounded task and current assignment,
revalidates the same immutable handle, and issues the response resource under
those locks. A failed projection rolls back the write; it does not turn a
previously validated handle into lasting authorization.

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
Reusing the locked, byte-bounded task and validating its immutable handle
without a second handle/task fetch removes one query from each read. On another
isolated PostgreSQL fixture, 14 warm paired reads measured a median of 42.7 ms
(38.9–70.3 ms; 31 queries) for quality and 21.7 ms (19.5–35.8 ms; 19 queries)
for ordinary work. The difference remains observable in this diagnostic; the
common persisted evidence and live-revocation path is still required before
public mixed delivery. These measurements are not a production benchmark.
After integrating main through `61f7bbd44`, a fresh isolated PostgreSQL
fixture again measured 14 warm paired editor reads: quality median 33.9 ms
(28.9–47.2 ms, 31 queries), ordinary median 14.6 ms (12.5–17.4 ms, 19
queries). This confirms that the live proof and read paths remain distinguishable
in this diagnostic; it is not a deployment measurement or a privacy threshold.
An experiment folded the retained quality marker into the ordinary assignment
lookup while keeping full live quality-proof validation. In a fresh isolated
PostgreSQL fixture, 14 warm paired reads still differed: quality median 32.8 ms
(29.5–39.9 ms, 29 queries), ordinary median 15.0 ms (13.2–25.7 ms, 18 queries).
The experiment was removed because it did not unify the evidence or close the
observable gap. These values are diagnostic, not a production benchmark.
The optional fixed-package balanced allocator counts active assignments to
choose a recipient. A sampled task and its deferred source can distort that
count, revealing information through the chosen recipient. Balanced allocation
therefore fails closed for opted-in or previously sampled projects, including
scheduled allocation, until a common blind allocation contract is verified.
Ordinary projects retain the existing balanced allocation behavior.
Both single-package and atomic batch creation reject retained quality tasks
and active deferred sources under the project lock, before creating any
package or replay receipt. A batch is rejected as a whole.
Participant package claims recheck blind scope after acquiring the project
lock, so a claim that started before a sample was bound cannot return raw
assignment identities or create ordinary package work afterward.
Annotator package list, detail and available-pool responses materialize without
the project lock, then take it briefly to recheck the role, policy, blind scope
and returned identities. A blind transition can commit during the initial read;
if it commits first the response is denied, and if it arrives after the final
fence it waits until response assembly completes. Reviewer reads retain their
ordinary review behavior. This fence does not unify blind evidence or establish
timing privacy.
For repeatable local diagnostics, `measure_classification_blind_e2e` accepts
the two outputs of the isolated synthetic seed commands, alternates the two
reads and reports aggregate latency and query counts without returning handles.
It requires `TEST_ENVIRONMENT`, the explicit synthetic fixture flag and the
synthetic database name. It is not a production measurement or an acceptance
threshold; the command cannot prove the absence of a timing side channel.
The optional `--profile-samples` runs additional paired synthetic reads under
Python profiling and reports aggregate function names and timings without task
contents or handles. Profiling adds overhead and is not a latency measurement.
The optional `--cold-processes` argument runs up to ten first reads of each
purpose in fresh application processes, alternating their order. These reads
include first-request application initialization, while the database and its
cache remain shared; the report labels that limitation and remains diagnostic.
In an isolated PostgreSQL run, 100 warm pairs measured 28.6 ms ordinary and
40.5 ms quality medians, with a 0.9884 quality-versus-ordinary rank statistic.
Four fresh-process pairs measured 96.2 ms and 124.9 ms medians respectively;
the small cold sample and shared database cache cannot establish indistinguishability.
At commit `46c5875`, a fresh isolated PostgreSQL run measured 100 warm pairs:
27.8 ms ordinary and 41.9 ms quality medians, with rank probability 0.9922.
Ten fresh-process pairs measured 104.4 ms and 125.9 ms medians (rank 0.83);
the database cache remained shared and the result is diagnostic only.
The project-file and text-length exposure tables have indexes aligned with
their account/generation read predicates; PostgreSQL creates them using Django's
concurrent index operation. In isolated PostgreSQL experiments with 200,000
synthetic rows and no matching exposure, the text predicate changed from a
15.7 ms sequential scan to a 0.07 ms index-only scan, and the file predicate
from 8.7 ms to 0.07 ms. These worst-case probes are not production estimates
and do not remove the ordinary-versus-quality timing difference.
An equivalent 200,000-row in-memory SQLite probe changed the text predicate
from 12.8 ms to 0.05 ms and the file predicate from 7.8 ms to 0.02 ms; both
plans used the new covering indexes. These are single diagnostic runs.
After adding the indexes, a fresh small-fixture PostgreSQL diagnostic still
measured 37.3 ms ordinary and 52.2 ms quality medians over 100 warm pairs
(rank 0.9446). Four fresh-process pairs measured 114.3 ms and 136.1 ms
(rank 0.875) with a shared database cache. In 20 separately profiled reads per
kind, quality reads invoked Django expression resolution 10,820 times versus
5,460 for ordinary reads. Profiling overhead prevents treating these numbers
as production latency or a timing-privacy guarantee.
The existing personal-work flow can consume that store and open the editor by
handle when its caller explicitly supplies the disabled protocol mode. A
synthetic browser harness has exercised ordinary work in English and Chinese,
including initial submit, an update and assignment-version advancement. A
separate synthetic fixture uses the internal sampler to give a newly eligible
member both quality and ordinary work, and exercises their common opaque editor
and submission in both languages. This test explicitly constructs the disabled
protocol; it does not activate dispatch or the interface for normal users.
The synthetic browser harness also tests project-membership revocation in both
languages for ordinary and quality work: a denied stale submit closes the
editor. Its guarded test fixture enables a separate manager token only in the
synthetic organization, so the participant browser session remains active.
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
successor and retries use the original command key. Its manager endpoint is hard-disabled
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

Bound ordinary and quality assignments also have a retained read-identity
state. Assignment cancellation, deletion or a change to its task, project, assignee or ID
revokes that state in the same transaction; restoring the old values does not
restore the binding, while rolling back the change rolls back revocation.
Deletion of a quality binding's capture certificate, privilege history,
account/reference provenance event or pinned reference also revokes it
transactionally. Matching text-length exposure, exposure to the frozen source
task and an additional reference text length also revoke the quality binding
under the same transaction. Ordinary bindings are unaffected by these
quality-specific events. The evidence migration backfills currently missing or
invalid history, provenance, references or certificates and matching recorded
exposures while source tables are locked;
rollback of an event also rolls back the revocation. The state is retained
even if evidence is later restored. Historical events that are no longer
present cannot be reconstructed by this backfill.
Both bound read paths require the retained state in addition to all existing
live checks. At upgrade, bindings with a currently active matching assignment retain
their previous access and missing or mismatched assignments are revoked.
Identity changes before this migration cannot be reconstructed, so the state
proves continuity only from the upgrade onward. Once state exists, rolling
back this migration is blocked. This identity state covers only assignment
identity and these selected evidence events; it is not a replacement for live
eligibility, reference, exposure, membership or decision checks. File and label
exposure are also retained once per certified account and generation, and both
signed bound ordinary and quality reads evaluate that same account state. New file and label
events update this account state without inspecting whether it has quality
bindings; ordinary work remains accessible while quality work fails closed.
Migration backfills the account state while certificates and exposure tables
are locked. The state is retained after certificate deletion, only moves toward
exposure, and cannot be discarded on rollback. Existing binding revocations
from an earlier migration remain in force. PostgreSQL text and source exposure
inserts no longer update quality bindings; fenced reads check the retained
ledgers. Reference-length changes still update quality bindings specifically.
Neither projection establishes timing privacy
or enables public dispatch. Uncertified accounts and unsigned legacy reads
still use different paths and require separate compatibility and timing work.
After adding identity state and before the evidence-state migration, an isolated PostgreSQL fixture measured 100 warm
pairs: ordinary median 32.7 ms and quality median 47.7 ms, with a 0.9928
quality-versus-ordinary rank statistic. Four fresh application-process pairs
measured medians of 109.0 ms and 124.9 ms respectively. These process starts
shared the database cache and the samples are too small to establish cold
cache behavior or timing privacy. The remaining difference is a blocker.
After the evidence revocation migration and before the account-state migration,
a separate isolated PostgreSQL run
measured 100 warm pairs: ordinary median 31.0 ms and quality median 44.1 ms
(rank 0.9931). Two fresh application-process pairs, with a shared database
cache, measured medians 122.3 ms and 145.1 ms. For 100 paired rolled-back
label-exposure inserts, ordinary and quality medians were 0.45 ms and 0.51 ms
(rank 0.6621). The write probe measures a database insert, not the full HTTP
path. These small synthetic samples are diagnostic, not a privacy guarantee;
both read and write paths need further work before public mixed dispatch.

After the shared account-exposure state, another isolated PostgreSQL run
measured 20 warm read pairs: ordinary median 31.0 ms and quality median
44.5 ms (rank 1.0). For 100 paired, rolled-back label inserts both medians
were 0.49 ms (rank 0.5073). A new cross-assignment sentinel text-exposure
probe measured 0.50 ms and 0.52 ms respectively (rank 0.612); it also
verified that the quality binding is revoked in the transaction and that the
revocation and exposure disappear on rollback. The write probes time database
inserts, not HTTP requests. They use a tiny synthetic fixture and do not prove
timing privacy, especially at realistic data volumes. This measurement predates
removal of the purpose-specific PostgreSQL text-exposure trigger.
The sequence reconciliation migration also compares existing quality proofs'
account and reference provenance sequences with the current immutable events.
It retains revocation for mismatches under the migration lock; it does not
promote missing proofs to valid evidence. Unsigned and missing-proof work keeps
the live compatibility checks. This backfill covers only these sequences and
does not make the projected state a complete authorization decision.
For signed PostgreSQL reads, the immutable provenance events and reference identity now
use the retained read-identity state instead of three additional live subqueries:
binding issuance checks the full proof before signing, the upgrade backfills
invalid identities and sequences, and transaction-fenced deletion revokes the
state. The signature, proof, marker and remaining live checks are still
required. This consolidation does not by itself establish timing privacy.
SQLite retains the three live checks pending separate resolution of its
retained-evidence replacement semantics.
PostgreSQL signed reads also use the retained account exposure flags for
file and label access, rather than reading those event tables a second time.
The flags are generation-bound, backfilled under migration locks and set in
the same transaction as each exposure; unsigned work keeps its live path.
SQLite retains the additional live exposure checks.
PostgreSQL signed reads also lock a retained per-account privilege state in
shared mode for both purposes. Privilege history promotion or deletion
invalidates that state transactionally; a reader holding it finishes before
the writer commits. The state is backfilled under the history-table migration
lock and cannot be reset or deleted. Ordinary work does not require a clean
quality-eligibility history, while quality work requires the state as well as
the existing live certificate check. A signed proof is issued only after
assignment history is validated, migration 0057 revoked invalid historical
bindings, and assignment history cannot be reset. PostgreSQL can therefore
read the retained privilege state instead of repeating the live privilege
history predicate. Update triggers do not
acquire the global capture epoch after locking the history row, avoiding an
inverted epoch/history lock order. SQLite retains its existing live path.
For signed reads, source-task and text-length exposures are checked directly
from immutable generation-scoped ledgers while the capture fence is held.
PostgreSQL migration 0063 removes the purpose-specific write triggers; its
rollback reconciles ledger entries into the old binding state before restoring
them. The authenticated reference-length set is checked exactly, and the
immutable proof and marker remain required. SQLite retains its existing write
and live-read behavior. Timing privacy remains unproven.
Fenced signed reads now load the immutable binding and byte-bounded signed
attestation without first building the separate legacy verification query.
The unified live check still verifies the signature, binding identity,
marker, proof and mix decision under the transaction fences. An absent
attestation falls back to the legacy verifier; malformed or oversized signed
evidence fails closed. This avoids duplicate checks and does not authorize the
loaded binding before the live check succeeds.
The bounded loader now leaves signature verification to that mandatory fenced
live check, instead of verifying the same payload twice. Regression tests cover
both purposes, unsigned legacy fallback, and forged and oversized evidence.
After the shared mixed-lifecycle predicates and packed marker projection at
`5e1ae06d4`, an isolated synthetic PostgreSQL run measured 100 warm read pairs
at 25.2/39.0 ms ordinary/quality median (rank 0.9847). Ten profiled pairs
showed 4,590/6,960 SQL compiler calls and 3,610/8,500 object-copy calls;
profiling itself adds overhead. Two fresh-process pairs measured 105.4/117.0 ms,
with a shared database cache. These are diagnostics, not cold-cache or privacy
acceptance; the two purposes still use materially different live checks.
The mixed-quality live guard now uses the exact matching decision predicate to
establish its retained history and source lifecycle together, avoiding three
redundant existence checks; unmixed quality still rejects orphan decisions or
history. On another isolated synthetic PostgreSQL fixture, 100 warm pairs after
this change measured 28.2/41.4 ms ordinary/quality (rank 0.8741); two fresh
process pairs measured 87.5/118.1 ms with a shared database cache. Different
fixture runs and a two-pair process sample do not establish an improvement or
cold-cache privacy. The timing gap and public-dispatch release block remain.
On synthetic PostgreSQL 16 at `f53a17d07`, 100 alternating warm read pairs
measured ordinary/quality medians of 27.1/41.7 ms, with a quality-versus-ordinary
rank statistic of 0.9746 and 20 SQL statements per read. Two fresh-process pairs
measured 100.6/110.3 ms medians while sharing the database cache; they do not
characterize a cold database. In a separate diagnostic process, temporarily
replacing the quality mixed-lifecycle and text-length/exposure predicates with
fixture-only placeholders produced medians of 30.7/34.5 ms (rank 0.737). This
ablation bypassed required authorization checks and is not a candidate fix or
privacy acceptance; the different samples are not a controlled speedup claim.
It shows that those predicates deserve attention in addition to global scope.

The existing `BlindMixEvidence` cannot simply be populated for every ordinary
decision: its presence currently identifies deferred quality sources in task
assignment and package authorization. A shared lifecycle representation must
retain a distinct, explicit quality-source predicate at those consumers, backfill
existing decisions transactionally, and preserve cancellation/completion and
rollback semantics. Otherwise a superficially uniform history would hide or
block ordinary assignments. Both purposes must be verified against the resulting
real lifecycle state without adding unrelated work merely to equalize timings.
The additive `BlindMixSharedEvidence` migration retains both outcomes and
backfills existing decisions under the database write fence. New binding,
attested-read and reassignment checks require the exact retained identity. The
existing quality-only `BlindMixEvidence` and its legacy consumers are unchanged,
so old application processes continue to hide only selected sources during a
rolling upgrade. PostgreSQL and SQLite capture future decisions transactionally;
the new retained rows are immutable, and rollback refuses to discard them.
Apply the additive migration before starting code that reads the new table;
older code can continue using its unchanged legacy table during the rollout.
The PostgreSQL upgrade and downgrade require READ COMMITTED isolation so their
post-lock checks cannot use a snapshot taken before an in-flight write commits.
Rollback takes the same ordered write fences before checking retained evidence;
SQLite takes a write reservation before the corresponding check.
This is an expand phase: selected decisions still also write the legacy table,
and quality reads still evaluate additional lifecycle and exposure evidence.
It is not timing parity and does not enable public dispatch.
With this expand phase enabled, a separate synthetic PostgreSQL 16 run of 100
warm pairs measured ordinary/quality medians of 25.8/40.6 ms and a rank
statistic of 0.9947. Two fresh-process pairs measured 97.7/179.6 ms with a
shared database cache. These small, separate runs are not a controlled comparison
or a cold-database test; they demonstrate that the release blocker remains.
The subsequent `classification_blind_mix_live_lifecycle` database view checks
the live decision, shared identity, quality-only legacy history, source and
completion for both mixed purposes. Participant object reads and quality
collection/admission predicates use that same live view; it is not a cached
grant and remains subject to the existing transaction fence. Apply the view
migration before deploying readers. Before reversing that migration, stop the
readers that require the view; a later refusal to discard shared evidence can
leave the view migration reversed and requires it to be reapplied.
On a separate isolated PostgreSQL 16 fixture, 100 warm pairs after this change
measured ordinary/quality medians of 25.3/30.2 ms and rank 0.8396, with 20
statements per read. Two fresh-process pairs measured 140.4/120.7 ms with a
shared database cache. These are small diagnostic samples from separate runs,
not a controlled performance comparison, cold-database test or privacy
acceptance. Quality-only exposure and external-scope checks remain, and public
dispatch stays blocked.
In a separate isolated PostgreSQL 17 fixture, 100 warm pairs measured
26.6/30.5 ms ordinary/quality medians (rank 0.7816). Ten pairs measured after
evicting that database's eligible PostgreSQL shared buffers measured 29.2/32.2 ms
(rank 0.75), with at least 45 buffers evicted on each read and no reported
eviction failures. Both used 20 statements per read. The operating-system cache
remained shared; these limited samples are diagnostic, not a fully cold test or
proof that the timing side channel is absent.
An experimental PostgreSQL checkpoint for global storage, model and credential
scope was evaluated and discarded: it required shared table and function locks,
additional database round trips, trigger integrity checks and privilege-dependent
fallback, while the remaining quality-specific read checks still differed. On an
isolated PostgreSQL 17 fixture, 100 warm pairs measured ordinary/quality medians
of 35.0/47.8 ms (rank 0.9435; 27 statements each). Ten pairs after eviction of
the synthetic database's eligible PostgreSQL shared buffers measured 30.6/41.1 ms
(rank 0.99; at least 64 buffers evicted per sample, zero reported failures).
The latter retains operating-system caches and is not a fully cold measurement.
Different runs cannot prove a regression or improvement; neither establishes
timing privacy. The checkpoint and its experimental migration were not retained.

The next shared-read design needs one transactionally maintained authorization
projection for both purposes, rather than an additional quality-only checkpoint.
Its validity must cover account and project membership, assignment and binding
identity, capture generation, privilege and exposure changes, reference and
source evidence, mixed-decision history, and external storage/model/credential
scope. A change to any applicable source must invalidate the projection in the
same transaction, including deletion, replacement, backfill and migration
paths. Ordinary work must remain usable where its own authorization is valid;
it must not inherit a quality-only admission requirement merely to equalize work.
Before replacing the current live checks, compare both implementations on the
same synthetic fixtures and negative mutations, test concurrent revocation at
the response fence on both databases, and measure alternating warm and genuinely
characterized cold reads. Unknown coverage or unavailable invalidation must
fail closed for quality work. Keep public mixed dispatch disabled until these
checks and the remaining browser and release boundaries are met.

Managers can inspect frozen blind scores through the project-scoped
`gold/blind-scores/` collection and its detail endpoint when classification
gold is enabled. The collection returns at most 100 metadata rows per page;
`next_before` is the exclusive cursor for the next page. The detail checks the
bounded manifest, its hash and its submission, assignment, marker and reference
identities. Both endpoints recheck manager authority while holding the project
operation fence and use private, no-store responses. They do not enable blind
dispatch, provide an annotator score endpoint, or implement alert events.
The existing classification-gold workbench can load these scores and page
through them on request. It clears displayed score data after a denied read or
project change; server authorization remains the boundary. The browser test
exercises a synthetic frozen score and denied reviewer/annotator requests in both
locales; the frozen score is produced through the same submission and scoring
paths using a separate synthetic participant.
After the earlier single-verification change, an isolated PostgreSQL fixture's 100 warm read
pairs measured ordinary/quality medians of 26.4/36.2 ms (rank 0.9724); two
fresh-process pairs measured 121.8/128.1 ms with a shared database cache.
These diagnostic samples do not show equivalent timing or establish cold-cache
privacy, and the remaining quality-specific checks still block public dispatch.
An earlier PostgreSQL diagnostic after the bounded-loader change measured 100 warm pairs
at 24.1/33.2 ms ordinary/quality median (rank 0.9481), and two fresh-process
pairs at 95.0/117.4 ms with a shared database cache. The paths remain
distinguishable and the process samples do not establish cold-cache behavior.
After requiring the capture fence in the bounded loader, a second 100-pair
warm run measured 24.1/34.0 ms (rank 0.9486). Two fresh-process pairs
measured 106.2/114.3 ms and also shared the database cache. These synthetic
results do not establish timing privacy.
An additional isolated PostgreSQL 17 diagnostic evicted eligible buffers for
the synthetic database using the developer-testing `pg_buffercache_evict`
function before each read. Ten alternating pairs measured ordinary/quality
medians of 25.0/33.2 ms (rank 0.96), with at least 41 buffers evicted per
sample and no reported eviction failures. This exercises a colder PostgreSQL
buffer pool only: operating-system caches remain shared, the sample is small,
and neither full cold-cache behavior nor timing privacy is established. See
the [PostgreSQL 17 pg_buffercache documentation](https://www.postgresql.org/docs/17/pgbuffercache.html).
PostgreSQL editor concurrency tests also cover source and text exposure while
a real handle read is in progress. The exposure writer waits for the fenced
read; after it commits, both a subsequent read and a stale annotation submission
are denied. These checks establish the tested revocation ordering, not equivalent
read timing or end-to-end protection of every exposure route.
Migration recording disables the capture epoch as in the existing capture
protocol. Operators must not reactivate capture until every serving process
uses the ledger-checking code; an old process cannot safely use the removed
per-binding write triggers. This change does not activate or deploy capture.
After migration 0063, a synthetic PostgreSQL run measured 100 paired,
rolled-back text-exposure inserts at 0.44/0.44 ms median (rank 0.5016) and
source-exposure inserts at 0.41/0.42 ms (rank 0.5017), ordinary/quality. These
measure only SQL inserts on a small fixture, not full HTTP writes or a privacy
guarantee. A separate 100-pair warm read run measured 29.4/38.7 ms median
(rank 0.9124); two fresh-process pairs measured 106.0/121.8 ms with a shared
database cache. Read timing and real cold-cache behavior remain unresolved.
In 20 additional profiled reads per purpose, ORM expression resolution was
called 5,960 times for ordinary work and 10,000 times for quality work; SQL
compilation was also higher for quality work (12,240 versus 16,280 calls).
Profiling adds overhead and the counts are diagnostic, but they locate part of
the remaining cost in the distinct query construction and compilation paths.
After this consolidation, 100 warm PostgreSQL pairs in an isolated synthetic
fixture measured ordinary/quality medians of 29.6/37.7 ms (rank 0.9242).
Two fresh-process pairs measured 110.6/116.8 ms with a shared database cache;
the sample is too small to establish cold-cache behavior. The direct text
exposure insert probe measured 0.49/0.53 ms median (rank 0.6656). These
diagnostics do not establish timing privacy or authorize public dispatch.
After adding the privilege state, 100 warm PostgreSQL pairs in a synthetic
fixture measured ordinary/quality medians of 32.7/43.4 ms (rank 0.958), with
20 SQL statements per read. Two fresh-process pairs measured 99.3/120.1 ms,
sharing the database cache; they do not establish cold-cache behavior.
Before the privilege state, after file-exposure consolidation, 100 warm
PostgreSQL pairs in another synthetic fixture
measured ordinary and quality medians of 28.6 ms and 39.1 ms (rank 0.9681).
Two fresh-process pairs measured 114.3 ms and 115.8 ms with a shared
database cache. The very small fresh-process sample does not establish cold
cache behavior; the warm results still show a substantial timing difference.
In a separate isolated PostgreSQL fixture after consolidation, 100 warm
paired reads measured ordinary and quality medians of 29.6 ms and 41.2 ms.
Two fresh-process pairs measured 109.7 ms and 127.0 ms with a shared database
cache. These synthetic samples show that a timing gap remains; they are not
production estimates or proof of cold-cache behavior.

Completion read state is retained after submission deletion, or a change to
its identity, assignment or submitter. Revocation is irreversible even if the same
submission number or actor is later restored; a rolled-back change does not
revoke the completion. Existing completion records are conservatively
revoked during migration because historical submission-number reuse cannot be
ruled out. They require a separate recovery process before further access;
this migration does not reconstruct their history. Once state exists, rolling
back this migration is intentionally blocked. This state covers completion
evidence only and is not the common live-read projection.
On PostgreSQL the retained state cannot be updated directly: submission
deletion or identity changes take the capture fence before their trigger
revokes it. This preserves the lock order while excluding a direct state
update that could otherwise race a response or deadlock a submission change.
After adding it, an isolated PostgreSQL diagnostic measured 100 warm pairs:
ordinary median 29.1 ms and quality median 41.7 ms (rank probability 0.9729).
Four fresh-process pairs measured medians of 106.1 ms and 121.1 ms; this small
sample shares the database cache and does not establish timing privacy.

Migration 0066 adds a live database view for the exposure evidence used by
both ordinary and quality reads. It reads current account, marker, proof,
source and text exposure records under the existing capture fence; it does not
cache authorization decisions. Quality reads still compare the source with the
signed proof and the current reference lengths with the signed length set, and
retain external-resource and SQLite legacy checks. Migrating to 0066 must
precede starting readers that use the view. Stop those readers before rolling
back the migration. The view can be recreated from retained evidence and does
not itself enable capture or public mixed dispatch.
With 0066, 100 paired warm reads in an isolated PostgreSQL 16 synthetic
fixture measured ordinary/quality medians of 24.8/29.1 ms (rank probability
0.8305), with 20 SQL statements for each. Two fresh-process pairs measured
88.7/106.2 ms median with a shared database cache. The difference remains
observable in these samples; the small fresh-process sample does not measure a
fully cold database or establish timing privacy. Public dispatch stays disabled.
Migration 0067 tightens the SQLite view's source identifier parsing: SQLite
can classify an out-of-range JSON integer as an integer while extracting it as
a real. The view now requires an actual SQLite integer before conversion. The
signed source comparison remains mandatory on both database backends. Existing
0066 installations must apply 0067 to replace the view definition.
Migration 0068 adds a live reference-length view used by both work purposes.
It returns a sorted set of at most 1,024 lengths; a larger set returns NULL
without aggregating an unbounded result. Ordinary work must have an empty set.
Quality work compares the live set to its authenticated signed lengths in the
same fenced read. For signed sets larger than the view limit, the existing exact
count and extra-value checks remain in use. The view does not cache a grant or
replace external-resource checks. Apply it before starting readers; stop those
readers before reversing it. This consolidation does not establish timing
privacy or enable public dispatch.
On one isolated PostgreSQL 16 synthetic fixture, sequential 100-pair warm
diagnostics before and after this change measured ordinary/quality medians of
25.7/29.4 ms (rank 0.7586) and 27.6/29.5 ms (rank 0.6363), respectively, with
20 statements per read. Two fresh-process pairs after the change measured
102.6/108.4 ms with a shared database cache. These sequential runs and small
fresh-process sample do not establish a controlled improvement, a cold-cache
result, or timing privacy; external-scope and other quality checks still differ.
An isolated PostgreSQL 17 diagnostic after the change evicted eligible database
buffers before each of 50 alternating paired reads. Ordinary/quality medians
were 29.7/30.7 ms (rank 0.5976), with at least 47 pages evicted per read and
no reported eviction failures. The operating-system cache remained shared, so
this is not a fully cold test or evidence of timing privacy.

The built-in storage fence covers the five import-storage tables named in
migration 0030. Four of those models also use concrete parent tables for
configuration. Migration 0069 fences parent updates and deletes when an import
child exists; export-only parent rows do not invalidate capture. Upgrade and
rollback disable the current capture epoch because prior parent writes were
not fenced. Quiesce storage configuration writes during migration, and establish
a fresh generation with independently verified provenance before enabling capture.
PostgreSQL concurrency tests exercise insertion, update and deletion of each
built-in import storage and an ML backend, plus credential changes: a writer
waits for an in-flight capture reader and disables capture after it commits.
Handle tests also check that those changes revoke quality reads while ordinary
work remains readable.

The runtime storage list can also be supplied by a configured function. A
dynamically registered storage table is not covered by these static triggers.
Checking whether it currently contains rows does not prove that its writes
were fenced or that a later removal preserves exposure history. A shared
external-scope projection must account for registry and deployment changes
transactionally before those live checks can be replaced.

Migration 0070 supplies a live built-in external-scope view to both ordinary
and quality handle reads. The quality guard applies the global credential,
model and all built-in import-storage flags regardless of registry membership:
the built-in storage API routes remain mounted even if a type is omitted from
the configurable registry. Admission and collection checks use the same scope.
Other registered models fail closed, even when their database tables are empty,
including entries that merely claim a built-in model label. Their independent
data sources and serving routes cannot be certified by an empty table.
Ordinary work validates the view is
available without adopting quality-only denial conditions. The view is not a
cached grant and does not establish continuity for a dynamically added or
removed storage provider. Apply the migration before starting these readers;
stop them before reversing it. Keep public mixing disabled until the dynamic
registry, remaining timing, and browser acceptance boundaries are resolved.
Both work purposes now fetch the epoch generation and enabled flag through the
same fenced projection. Quality work still requires the signed generation to
match an enabled epoch; ordinary work does not inherit that eligibility rule.
This removes the purpose-specific epoch predicate, but other purpose-specific
checks remain and this alone does not establish timing privacy.
For an ordinary assignment bound to a mixed decision, the common lifecycle
projection already verifies that decision and its retained history. Its read
does not issue a second, unused decision-existence subquery; legacy ordinary
bindings without a decision retain the separate absence check. This removes a
real query-shape difference without substituting artificial delay for evidence.
Migration 0071 adds a read-only binding lifecycle projection: both ordinary and
quality reads consult the same projection, which validates the retained mixed
decision and source lifecycle or, for legacy bindings, the absence of mixed
history. Applying or reversing the migration disables the capture epoch through
the existing migration fence; stop dependent readers before rollback and roll
out the view to every reader before starting a new generation. This consolidates
one predicate, not the remaining signed
evidence checks, and does not establish timing privacy.
On one isolated PostgreSQL 16 synthetic fixture, sequential 150-pair warm
diagnostics before and after this change measured ordinary/quality medians of
27.6/29.6 ms (rank 0.6727) and 25.7/26.1 ms (rank 0.5339), respectively, with
20 SQL statements per read. Two new-process pairs after the change measured
107.4/95.6 ms while sharing the database cache. An independent PostgreSQL 17
diagnostic evicted that synthetic database's eligible shared buffers before
each of 50 alternating paired reads: medians were 28.3/28.7 ms (rank 0.5228),
at least 58 pages were evicted each time, and no eviction failures were
reported. The sequential runs are not controlled comparisons, and operating
system caches remained shared; these diagnostics do not prove timing privacy.
After removing the purpose-specific epoch predicate, another isolated
PostgreSQL 16 run measured 150 warm pairs: ordinary/quality medians were
27.7/26.7 ms with rank probability 0.4099 and 20 queries for each purpose.
Two fresh-process samples per purpose measured medians of 108.6/124.2 ms,
with the database cache shared. These sequential, synthetic observations are
not controlled comparisons or a cold-cache privacy guarantee.

The project workbench selects its participant protocol through the authenticated
`work-mode` endpoint before initializing the data manager. The response binds
the project and session user, is not cacheable, and is rechecked against live
project membership under the project/actor lock. A sensitive non-manager uses
the existing opaque work list and editor; it does not fetch model backends or
initialize the legacy task grid. A failed or stale mode response fails closed.
Managers and ordinary projects retain the legacy workbench. This UI routing is
not an authorization grant: every work read and write still checks permissions,
and it does not establish dynamic-storage continuity or timing privacy. Public
mixed dispatch remains disabled until those release boundaries are verified.

The optional Compose/systemd schedule in `deploy/blind-score-reconcile/` uses
the same one-shot pattern as bbox reconciliation. It runs a bounded retry page
every five minutes, using the deployed app service's database and Redis settings.
A failed enqueue remains pending and rotates behind older unattempted records,
so a repeatedly failing row does not monopolize a bounded retry page.
A worker must also consume the configured approved-export RQ queue. Installing
the example timer, validating permissions, inspecting service failures and
verifying the actual worker are deployment operations; the source files do not
enable them. Backlog and permanent invalid rows require operator monitoring.
The isolated Redis test uses a real RQ simple worker, but does not prove a
separate production worker, systemd timer or live deployment is operational.
