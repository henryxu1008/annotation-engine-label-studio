# Approved Submission batch export (#79)

The user confirmed the [design](approved-dataset-export-design.md) on 2026-10-21.
Implementation is isolated from #76 and remains **local / not merged / not
deployed / not integrated with Core**. A batch is a Fork export receipt, not a Host
DatasetVersion, Gold certificate or final business publication.

## Behavior and authority

Managers select exact approved Submission/revision/hash triples, preview them,
then create a frozen asynchronous batch. One revision per assignment is allowed;
different assignments for one task remain separate samples. Every member must
have matching historical approval evidence, an intact snapshot and consistent
project/task/assignment scope. Mixed exact label XML is refused. The XML SHA-256
is separate from the legacy process-dependent `label_config_hash`.

JSON and JSONL both preserve complete native snapshots, author/reviewer evidence
and exact large integer bytes. The first enabled standard adapter is **CoNLL2003**,
using the locked SDK Converter and its tokenization helper. It supports a bounded
single-text NER subset with nonoverlapping token-aligned spans and one label per
span. Unsupported formats/representations fail explicitly. COCO/VOC/YOLO are not
advertised by this implementation. Media references remain references, with no
claim that remote media bytes are frozen, fetched or checksummed.

Actor identity comes from the trusted identity provider and active local mapping.
Creation, worker start, publication and each frozen-content/download request
recheck live authority. Both the requester and original creator must remain
authorized for file access. Historical reviewer role removal does not rewrite
its decision. A revoked creator, changed policy, deleted source or corrupt input
cannot be replaced with a mutable Annotation. Corrupt output is quarantined;
retry cannot overwrite a READY artifact or regenerate a quarantined receipt.

There is no new release permission for a reviewer, annotator, organization admin
or unscoped superuser. Server mode and project scope apply to every method,
including HEAD and OPTIONS. Every resumed Range request authenticates again;
already delivered bytes cannot be recalled.

## Explicit configuration (reference only)

The new API is disabled by default. Configuration does not itself authorize
deployment or changes to an existing environment. A future authorized operator
must supply:

* `APPROVED_EXPORT_POLICY`: JSON with an operator-owned stable `source_namespace`
  UUID and `projects` entries keyed by `organization_id:project_id`. Each entry
  specifies `mode` and `version`. Only explicit `fork_approved` enables this path.
  A missing/invalid entry, `disabled` or `core_only_internal` denies the new API.
* `APPROVED_EXPORT_REDIS_URL`: a dedicated operator-approved Redis connection.
  This configures the `approved_exports` django-rq alias, without turning on the
  CE-wide Redis flag or changing legacy export queue behavior. Its normal worker
  command is `python label_studio/manage.py rqworker approved_exports` in the
  authorized environment. There is no synchronous generation fallback.
  HTTP health/enqueue connections have three-second connect/read timeouts,
  independently of the worker's normal blocking dequeue connection.
* `APPROVED_EXPORT_ROOT`: a private filesystem directory outside MEDIA_ROOT and
  STATIC_ROOT. Default: `BASE_DATA_DIR/approved-exports`. Do not expose it through
  a static server, signed URL, Nginx alias or cloud storage download link.

The validated background identity provider is the existing local provider.
Other providers must explicitly implement trusted `resolve_background(principal_id)`
returning a freshly validated `(Principal, mapped User)`; absent that hook they
cannot admit a batch. No initiating session or credential is put into a job.
This is an extension contract, not completed external-provider/Core integration.

Policy version changes invalidate existing batch access. Preserve namespace
ownership across restarts, and decide restore/clone ownership before enabling a
copy. Source namespace provisioning and Host handoff are #80 responsibilities.
Never use the synthetic example/test namespace in a real source connection.

CPython 3.11 is required for this native encoding profile. Standard conversion
also verifies the installed SDK's distribution version and actual `direct_url.json`
archive identity against `poetry.lock`, not just a claimed version number.
Future semantic mapping changes must advance the mapping version; jobs reject a
different frozen runtime/adapter profile.

## API, files and recovery

Base: `/api/projects/{id}/approved-dataset-releases/`. The confirmed design lists
all routes, errors and permission predicates. `capabilities/`, `candidates/` and
`preview/` validate discovery without granting later authority. POST to the base
requires `Idempotency-Key`, explicit items, output profile and strict filters.
Unknown actor/reviewer/mode/snapshot fields are rejected. Same actor/key/input
returns one receipt even across concurrent PostgreSQL connections; changed input
under that key conflicts.

Frozen copies and fingerprints are committed before dispatch. Queue loss after
commit leaves `dispatch_pending`; it is recoverable using the same original actor
and sealed input. Attempt leases/tokens prevent stale workers from publishing.
Status exposes retry availability for failed dispatch, transient generation or
an expired worker. Retry is an authenticated explicit action, not automatic input
reselection. The UI can recover existing receipts from server history after a
reload. No browser storage is the source of truth.

On READY, authenticated downloads provide both native files, any requested
verified standard-format ZIP, and the frozen Manifest. The Manifest binds native
record fingerprints, content digest, request/input digest, historical freeze
authorization receipt, actual artifact hashes and converter provenance. Its own
SHA-256 is detached in `X-Manifest-SHA256`; the UI downloads the original server
bytes for independent checksum verification. Standard archives include a source-to-output
index and per-member checksums. Native input/content determinism is distinct
from third-party ZIP byte determinism.

Strong artifact ETags and single ranges permit resumed downloads. Invalid/multiple
or unsatisfiable ranges return an empty 416 response. Files are hash/size checked
on the same open handle before serving. The existing ranged-file library is
reused through a small bounded reader adapter: its original size calculation
reads the entire file, so this path uses `fstat` while retaining upstream parsing
and chunk iteration. Allow-audit failure prevents content delivery.

Effective maximums: 10,000 samples, 2 MiB request, 64 MiB frozen native records,
256 MiB per artifact, 512 MiB scratch, 30-minute worker timeout; 100 rows per
page, two active jobs per project and one per actor/project. Operator-supplied
`APPROVED_EXPORT_LIMITS` in Django settings may lower these limits. Native writes
check budgets before each record; the bounded SDK adapter budgets uncompressed
and worst-case archive scratch space before conversion, then validates output
line-by-line using SDK tokens/tags. No truncated or partial READY artifact exists.
This is a bounded batch API, not unbounded live-filter enumeration or a load-test
claim at every maximum.

## Data retention and the legacy boundary

Migration `data_export.0011_approved_dataset_release` adds release, frozen item,
attempt, artifact and access-event tables. Source items retain scalar evidence
rather than cascading through mutable Assignment/Submission/User FKs. The release
protects its Project from hard deletion. Soft deletion blocks access. Retention
has no delete API here; it requires separately authorized lifecycle work.

Importantly, a frozen receipt also keeps legacy mutable API exports, conversion
workers and export storage closed after source Submission/Assignment cascades.
All those enforcement points share `data_export.boundary`. Deleting source rows
cannot turn an old mutable export path back into a delivery bypass.

Rollback of an authorized installation disables new policy entries and workers,
preserving receipt/audit tables and private bytes. Do not reverse/drop the
migration in a retained environment. Forward/reverse migration checks use a
separate disposable PostgreSQL database. No rollback may reopen legacy export.

## #80 and Core-only-review

`fork.approved-export-manifest/v1` is a local immutable export receipt. #80 owns
cross-system `ReleaseManifest v1`, Host DatasetVersion mapping, version comparison,
durable media checksums/copies, cross-system revocation/retention and historical
reproducibility guarantees. The Host retains business publication authority.

Core-only-review uses the existing authorized ordinary Submission list/detail
reads. It cannot use these batch routes even if approved Fork records accidentally
exist. This feature neither creates Fork approvals from Core decisions nor starts
Core integration, grants a reviewer role, or authorizes external delivery on Core's
behalf. Legacy single-release API behavior remains separately governed; this work
does not claim to retrofit its integrity predicate or validate a mixed workflow.

## Verification entrypoints

* PostgreSQL API/worker/integrity/concurrency cases:
  `label_studio/data_export/tests/test_approved_exports.py` (refuses SQLite).
* Existing Submission/review, enterprise collaboration and project authorization
  suites remain required in addition to the new cases.
* Bilingual component/race tests: `ApprovedDatasetExport.test.jsx`, plus existing
  `CollaborationLocale.test.jsx`.
* Real browser journeys: `approved-dataset-export.cy.ts`, using
  `cypress.approved-export.config.ts`. Local acceptance uses an isolated Edge
  profile. The new GitHub workflow uses its disposable runner's Chrome.
* `seed_approved_export_e2e` creates only fresh synthetic fixture identities and
  requires `TEST_ENVIRONMENT=1`. It refuses duplicate identities rather than
  deleting existing data. Always target an independent test database.
* `.github/workflows/approved-dataset-export.yml` makes the PostgreSQL, actual
  Converter and bilingual browser checks reproducible. Adding it locally does
  not mean GitHub PR or push/main CI has run.

The [local acceptance receipt](issue79-local-acceptance.md) records exact local
results separately from PR CI, merge,
deployment and human/product acceptance. #76 worktrees, processes, containers,
mounts, images, database and delivery package are outside the #79 write set.
