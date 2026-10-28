# Durable NER project queues (#81)

This slice creates independent local NER analysis records. It never writes an
annotation, Submission, ReviewDecision, arbitration, sampling or delivery rule.
#81 and #84 remain open for their remaining scopes. The manual single-task NER
GET/receipt contract remains independent and unchanged.

## Workflow and inputs

Reuse the existing frozen-batch workspace through a narrow transport/schema/
renderer adapter: list -> explicit preview/create -> frozen filtered queue ->
NER pair matrix/differences -> return with filter/page/focus. The NER adapter
cannot call classification, partial-match, classification quality-overview or arbitration APIs.
The separately gated [NER overview](ner-quality-overview-v1.md) uses an explicit
NER-only read adapter; it does not extend this queue's metric DTO or Manifest.
Reuse the existing locale header, buttons, Zod contracts and NER result renderer.

A Manager chooses an explicit task-ID set or a bounded project scope. The signed
preview pins that exact task list; new project tasks are never appended. Use
active-assignment-latest/v1: all active assignments, independently selecting the
greatest revision for each assignment, one trusted assignee/submitter per cohort.
Never compare revision numbers across assignments or substitute an older approved
revision for a missing, cancelled or unusable latest result. Pending/approved/
rejected source states are not a quality score. Missing submissions remain frozen
missing inputs; later completion never upgrades that original task. Cancelled
assignments do not enter new plans; cancellation after freezing does not erase
intact historical evidence.

Reuse ner-strict-entities/v1, ner-overlap-explanation/v1 and pinned nervaluate
0.2.0. Same text/configuration/Unicode/CRLF/empty-value boundaries as the manual
NER view. For a task: disagreement means at least one comparable pair has exact
rational F1 < 1; agreement requires at least one comparable pair and all such
pairs equal 1; otherwise unavailable. Two empty sets are never 100%. No overall
mean, new IoU threshold, fuzzy credit or annotator ranking is introduced.

## Persistent evidence

NerBatch has its own schema ner-batches/v1 and project/UUID uniqueness. Its
Manifest fixes the task/cohort (including missing inputs), exact Submission,
assignment, assignee/submitter, revision/content/config/task-data hashes,
selection/rule/explanation/engine versions, calculation hashes and counts,
creator principal/source/actor, enumeration and calculation time. Raw answers,
text and full snapshots are not copied. Existing ClassificationBatch manifests
and arbitration contracts do not accept NER data.

PostgreSQL/SQLite triggers protect all frozen fields and the primary key, even
for bulk/SQL updates. Nullable creator cleanup is permitted; rebinding is not.
Source deletion does not cascade to this record. Existing project cascade deletion
remains: this is local durable work history, not permanent audit retention.

## API and authority

Under /api/projects/{P}/ner-disagreements/batches/:

- GET capability/, collection, {B}/, {B}/items/ and {B}/items/{T}/.
- POST preview/ accepts only name and explicit project/tasks scope; no DB write.
- POST collection accepts only preview_token/expected_plan_hash and UUID
  Idempotency-Key. Only this explicit analysis creation writes new data.
- Queue state is all/agreement/disagreement/unavailable, stable task-ID order.
  Page size defaults to 50, maximum 100. Item pair selection accepts left/right
  only from the same frozen task.
- No PUT/PATCH/DELETE or editable admin.

Validate names and preview-token text with DRF's NUL/surrogate validators before
UTF-8 encoding, scoring or persistence. Names retain the existing trim/160-character
contract and valid Chinese/non-BMP text. Revalidate names inside signed previews
issued by older code before source processing; invalid text returns 400 without
creating a batch or consuming the UUID. An intact original-key recovery still
precedes preview decoding and does not normalize or replace its original intent.

Require the independent default-off ANNOTATION_NER_BATCHES_ENABLED, existing
NER flag, exact organization/project fork_review mode and supported DB. Current
trusted Manager/Reviewer plus READ_WORK governs reads. Manager creation also
requires independent ORGANIZE_NER; review/classification/arbitration permission
does not grant it. Custom operation providers must explicitly allow it. Core-only,
unknown modes, annotators and revoked/cross-project scopes deny.
Existing project-creator Manager semantics remain unchanged: editing only that
creator's membership role is not revocation. Current organization/account or
operation withdrawal remains authoritative and is exercised by negative tests.

Reviewer self-cohorts are excluded as whole tasks before counts, SQL pagination
and source loading. Return only visible-subset counts and projection_hash, never
the full Manifest/hash or hidden totals. Empty visible batches are absent/404.
Collection reads do not load Submission snapshots. Detail/queue/item reads verify
the complete visible frozen cohort once per validation pass. Hidden cohort
answers never enter those queries.

Missing/rebound/corrupt current sources mask the affected task's answers and
scores as unavailable without shrinking its frozen input/pair denominator or
selecting another revision. Manifest corruption rejects the batch. Changes
during a single read discard the response. Recheck principal/source/actor/role,
flags/mode, READ_WORK, sources, result and actual JSON representation after render.
Use private/no-store and no-referrer.

Creation retains the existing Project -> identity/membership -> Task ->
Assignment -> Submission fence through rendering and final revalidation. A stale
plan returns 409 with no row. Same key/body/principal/source/actor recovers the
original metadata, before preview expiry/signature/source/budget reevaluation;
current create authority and intact Manifest remain mandatory. Different body
or identity conflicts. Ack includes the original key and derived immutable plan
hash so the client cannot confirm a different frozen plan.

Unknown acknowledgements preserve original intent/key in the open workspace.
Only explicit recovery may POST again. Read refresh never creates, substitutes
a new key, dispatches or repeats review. Blur/scope/failure clears answer evidence
and ignores late responses. Confirmed creation survives later read failures as
a saved notice, not permission to repeat the command.

## Whole-request budget

Inclusive limits: 100 tasks, 20 participants/task, 2,000 total cohort entries,
64 MiB SQL stored snapshot bytes and 8 MiB cumulative canonical snapshot bytes.
Reuse the same-statement cumulative CASE/window JSON guard from manual NER so
concurrent growth cannot reach the Python decoder first. Manifest and logical/
actual response each have a 1 MiB cap. Stored Manifest loading is also SQL guarded.
Preview is uncompressed, independently salted, at most 16 KiB, expires in 15 min.

Sum W over the entire batch <= 5,000,000 per validation pass. The total library
call ceiling is 380 across both response-validation passes, not 380 per task.
Check incremental cumulative budgets and full outgoing representation before the
first scoring call. Reject 422 without truncation, automatic splitting, sampling,
async jobs or HTTP fan-out. A batch of many small cohorts can fit; selecting a
20-person cohort plus another nonempty comparison can exceed the call budget.
Reads/pagination cannot lift a frozen batch's whole visible-cohort limits.

## Verification and preserved scope

API/pure regressions cover exact-source/latest selection, missing vs empty,
new revisions, frozen source loss/corruption, mode/role/operation denial, original
key recovery after expiry/key rotation/source loss, actual DB guards, response
tampering/revocation rollback and true 100-task/20-person/2,000-entry limits.
Exact cumulative bytes/W/calls and one-over reject before scoring/decoding.

Real PostgreSQL independent connections cover same-key/different-body creators,
project fencing through rendering, revocation-first and revision-first creation,
read-time withdrawal/deletion/corruption, pinned revision stability and source
growth between SQL preflight and hydration. SQLite skips are not PostgreSQL PASS.

Frontend contracts and en-US/zh-CN browser journeys cover creation with a real
commit/lost acknowledgement, original-key recovery, readonly refresh, pair/detail/
queue navigation, fixed revisions, source loss, reviewer self exclusion, revocation,
annotator denial and late responses. The existing manual NER browser journey runs
separately with this feature off. Both use only synthetic ephemeral GitHub runners
with PostgreSQL/Nginx. No retained local service/container is started or migrated.

Do not close #81/#84, change Host, deploy or start Core integration. Local Docker
and #76 are read-only. Preserve historical CHANGED, CHANGED_DATABASE_CONTAINER_REPLACED,
cause UNKNOWN, absent protected worktree and full database comparison
NOT_RUN_AFTER_DOCKER_INTERRUPTION. Current fingerprints do not prove DB equality.
Test definitions are not run results; record results separately.
