# Issue 86: S3 isolation and deletion acceptance

This is a proposed acceptance protocol, not evidence that the provider or the
application has passed it. Automatic annotation-object deletion and lifecycle
admission stay disabled until the deployment-specific isolation mechanism and
every applicable gate below have been demonstrated and independently reviewed.

## Test boundary

Use a dedicated disposable AWS account or an explicitly isolated test bucket,
never a production bucket or production object prefix. Record the AWS account,
region, bucket name, bucket owner, versioning state, endpoint, application and
worker build identifiers, database schema and broker topology. Use synthetic
keys and payloads only. Provision distinct identities for the old producer,
the replacement producer, the deletion executor and the observer. Record
identity identifiers and policy versions, but never access keys, session
tokens, authorization headers or complete presigned URLs.

Identify every possible writer, including direct SDK users, presigned requests,
multipart uploads, retries, queues, schedulers and synchronous fallback paths.
The isolation mechanism must cover old database and broker sessions, cloud
credentials and in-flight HTTP requests. If any writer or path is unknown, the
result is NOT_RUN/blocked, not a successful isolation check.

New annotation outbox entries use `annotation_export_v3_queued` and the
`annotation_export_v3` worker entry point. Previous v2 workers claim only
`annotation_export_queued`, so a new record is not a valid old-worker claim.
On upgraded workers the old v2 entry point fails closed. Existing v2 ledger
entries are neither converted nor automatically redispatched: they remain
blocking until the old workers, queues and external effects are isolated and
their disposition is established. A failed v3 RQ job on an old worker does
not finish the v3 ledger record; the current dispatcher can later replay that
same record. The failed RQ job remains and must be accounted for during queue
drain. This is one compatibility boundary, not proof that old cloud
identities or other legacy worker entry points are isolated.

## Evidence and order

Collect UTC timestamps, operation IDs, lifecycle generations, request IDs,
sanitized errors and the exact object version IDs. Save before/after version
inventories and database ledger states. Capture the provider policy and process
boundary that actually prevents reconnection; a checkbox, queue inventory,
credential deactivation or elapsed time alone is not proof of request drain.

1. With admission disabled, establish a versioned object history consisting of
   a synthetic original and a replacement on the same key. Verify expected
   bucket owner, endpoint, versioning, exact version and export metadata token.
   Repeat owner mismatch, absent token, changed storage identity, suspended
   versioning and denied permissions; each must fail before any deletion.
2. Start an old authenticated write and deliberately pause it after the request
   has begun. Apply the proposed process, network and credential isolation.
   Check that a new old-identity request is denied and try to finish the
   already-started request. Capture its actual terminal provider outcome, not
   just the client process exit. Repeat with timeout, lost response, multipart
   completion, presigned requests and a reconnect attempt. Any request whose
   terminal outcome or inability to commit is unknown keeps the drain blocked.
3. Exercise new producer, transition and deletion concurrently around the
   isolation boundary. Demonstrate that old code cannot reconnect to database,
   broker or object storage and that new work cannot pass the project fence
   after the transition. Include registered, failed, deferred and scheduled
   jobs and the explicit disposition of each legacy queue item.
4. Only after quiescence is proven, exercise the proposed delete against each
   synthetic object history: the target version is current, noncurrent, already
   removed, replaced, or protected by a delete marker or object lock. Verify
   both the exact version and the value returned by an unversioned GET before
   and after. A replacement must remain intact and a prior version must not
   become unexpectedly visible. Record which outcome the product actually
   requires: removing a specific version, hiding the key, or both. Do not treat
   those distinct outcomes as interchangeable.
5. Inject process death and loss of provider response after request submission.
   The original durable record must remain blocking until the original request
   is quiescent and its provider outcome is reconciled by the original key and
   version. Verify that restart, replay, revocation and a second worker do not
   delete a replacement or claim success from an unknown outcome.
6. With a nonempty ledger, exercise actual rollback and restart using the
   deployment's proposed compatibility boundary. Verify that no old binary can
   bypass lifecycle enforcement and all outstanding payloads, versions and
   delivery IDs survive. Current migrations deliberately reject destructive
   schema reversal; that rejection is not a successful lossless downgrade.

## Provider constraints and result

### AWS acceptance prerequisites for issue 144

The cloud test is **NOT_RUN** until a dedicated account or explicitly isolated
AWS resources and a separately scoped operator identity are available. The
operator must first record the account and bucket owner, region, endpoint,
test-only resources, every writer and intermediary, role trust policies,
permission and bucket policy versions, relevant network boundaries and the
evidence source for each request. A missing writer or unknown request remains
UNKNOWN and prevents an empty-drain conclusion. Do not reuse another workload's
bucket, IAM identity, network or broker as the test fixture.

AWS role-session revocation is not by itself a durable fence: the AWS revocation
policy denies sessions issued before its cutoff, while later role sessions can
still be obtained. Restrict the ability to obtain replacement sessions as well,
verify denial using both an existing session and a fresh assume-role attempt,
and test each permitted network path separately. Account for policy propagation
and role types to which the procedure does not apply. Never apply these controls
to a shared role. See [AWS role-session revocation](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_revoke-sessions.html).

Before issuing test writes, explicitly enable and verify the applicable S3 data
event logging for the isolated bucket and capture its configuration and coverage
interval. Ordinary CloudTrail Event history does not contain S3 data events, and
log delivery time is not guaranteed. A missing event, an empty object-version
list or an empty multipart page is therefore not proof that an individual
request cannot still commit. Correlate each original request with provider-side
evidence and the exact ledger entry; any unproven outcome stays UNKNOWN. See
[S3 data-event logging](https://docs.aws.amazon.com/AmazonS3/latest/userguide/enable-cloudtrail-logging-for-s3.html)
and [CloudTrail log delivery](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/get-and-view-cloudtrail-log-files.html).

For multipart completion, record and validate the **entire** response body:
S3 can send an HTTP 200 header before processing finishes and then return an
error in the response body. A lost or incomplete body is not a success receipt,
and the absence of an upload from a subsequent listing does not by itself prove
the completion outcome. Do not infer a safe retry from that absence. See
[CompleteMultipartUpload](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CompleteMultipartUpload.html).

### Local revalidation on 2026-11-17

On a Windows Docker host, the local fixtures can be run in order with the
following wrapper. The virtual environment and MinIO client must be Linux
executables; the required local Docker images and pinned baseline commits are
checked by the individual fixtures. Each stage stops on failure, and a later
stage not reached is NOT_RUN. Use `-Subnet` if the default isolated subnet
overlaps an existing network. The wrapper does not connect to AWS.

```powershell
./scripts/issue144-local-acceptance.ps1 -DependencyVenv <linux-venv-path> -MinioClient <linux-mc-path>
```

Against merged main `cdfdb46ef614a4c2fa0823dfb106a75eb55494bf`, disposable
Docker services with synthetic identities and no host-published ports produced:

| Local fixture | Result |
| --- | --- |
| Network disconnect and buffered request | PASS: a request buffered before disconnect committed afterward; new legacy-container connections were blocked. The existing SDK client's outcome remained UNKNOWN. The separate process-death proxy regression passed (1 test). |
| Pinned upstream old worker and mixed upgrade | PASS: 3 mixed-upgrade tests and 1 old-worker death test; the standalone old-worker/object-store fixture passed 2 tests. |
| S3 unknown outcomes | PASS: 4 tests. |
| Redis legacy queue and S3 multipart inventory | PASS: 14 and 15 tests respectively; inventory pages still report UNKNOWN and are not snapshots. |
| PostgreSQL isolation and lifecycle migrations | PASS: 11 tests; rejecting a destructive reverse migration with a nonempty ledger is not a successful lossless downgrade. |
| Recovery regressions | 48 passed, 2 skipped; the skipped old-worker cases were not counted as passes in this run. |
| Previous durable workers | PASS: 2 tests against the pinned previous worker; the upstream old-worker case skipped by recovery was run in the mixed-upgrade fixture above. |

The complete wrapper subsequently passed all 9 local fixture stages on the
same main baseline. Its failure path was also checked with a synthetic failing
stage: it exited unsuccessfully and did not start later stages.

These are local fixture observations, not evidence for an AWS account, an AWS
request terminal outcome, or a production rollout. AWS identity and network
isolation, in-flight reconciliation and drain, cloud mixed-version recovery,
and an actual lossless nonempty-ledger rollback remain NOT_RUN or UNKNOWN.

### Remaining cloud acceptance gates

| Gate | Required evidence | Current status |
| --- | --- | --- |
| Test boundary | A dedicated account or isolated test resources, operator authority, synthetic data, immutable identities, provider region and recorded resource ownership. | NOT_RUN: no authorized cloud environment is available. |
| All old writers fenced | Inventory old processes, SDKs, proxies, temporary identities, presigned requests and network paths; test existing and newly obtained credentials and each path after policy propagation. | NOT_RUN; unknown writers block acceptance. |
| Original requests reconciled | For submitted, timed-out, lost-response, killed-process, proxy-buffered and multipart-completion cases, correlate original request and ledger with reliable provider terminal evidence. | NOT_RUN; unresolved requests remain UNKNOWN and block drain. |
| Old queue and multipart disposition | Inspect all relevant brokers, registries and pages, account for every identified job and upload, and establish that inventory did not race continuing writers. | NOT_RUN in the cloud; non-snapshot observations alone are insufficient. |
| Mixed versions and recovery | Exercise actual old and new application builds with the isolated provider, failed jobs, process death and restart; retain request, version and ledger evidence. | NOT_RUN in the cloud. |
| Rollback and rollout | Demonstrate the proposed downgrade boundary with a nonempty ledger and preserved effects, or explicitly retain the current fail-closed rollback prohibition. Independently review the deployment-specific isolation and drain evidence before enabling lifecycle transitions. | NOT_RUN for an actual lossless downgrade; automatic deletion remains separately tracked by #145. |

On 2026-11-16, a disposable internal Docker network with a synthetic MinIO
bucket and separate administrator and old-worker identities was exercised.
Disabling the old MinIO user denied a subsequent old-worker request. After
re-enabling that user, forcibly disconnecting the old worker container denied
its subsequent request while the administrator container could still access
the bucket. The worker had no Docker control socket. These are local MinIO and
Docker results, not evidence of an AWS IAM or network policy, a full legacy
Label Studio worker, or the terminal outcome of a request already in flight.

An additional disposable PostgreSQL, Redis and MinIO test ran the Python
application from `origin/release/1.23-base` commit
`2a9bfbcbf0a844b999de97e601d16050a893f5fb` in a separate process using an RQ
`SimpleWorker` and the available compatible dependency environment. With a
paused test policy, a nonempty unknown ledger and a synthetic pinned version,
the current implementation refused an export but the old worker wrote a newer
version without the token and left the original database pin and blocking
ledger unchanged. After the fixture's independent MinIO user was disabled, a
new old-worker job failed and no additional version appeared. This is a real
legacy application process against local fixtures, not a release image, an AWS
test, a network-fence proof or evidence about requests already in flight.
In the same fixture the baseline worker failed to import a v3 RQ job, leaving
its v3 ledger record queued; current code then executed that same record against
MinIO. The read-only queue inspection found the actual failed old-worker job
before recovery and reported its disposition as UNKNOWN; the failed job remained
in Redis for explicit disposition after the current worker completed the record.
The opt-in regression is `io_storages/tests/test_s3_legacy_worker_isolated.py`;
it requires the disposable loopback services, Redis, `/fixture/mc`, an extracted
trusted source tree at `/legacy`, and both `ISSUE86_DISPOSABLE_S3_TEST=1` and
`ISSUE86_DISPOSABLE_REDIS_TEST=1` with `ISSUE86_LEGACY_APP_ROOT=/legacy`.
On a Windows Docker host with the required local images, run the pinned,
read-only-source fixture with:

```powershell
./scripts/issue86-isolated-legacy-worker.ps1 -DependencyVenv <linux-venv-path> -MinioClient <linux-mc-path>
```

The script uses an internal Docker network and synthetic credentials, publishes
no host ports, and removes only containers and the network it creates. Its
default subnet can be overridden with `-Subnet` when it conflicts with an
existing network. It must not be pointed at production services or interpreted
as an AWS acceptance test.

An additional local network-namespace test runs the pinned legacy application's
`S3ExportStorage` client in a separate container. After a successful request,
the legacy client submits a synthetic PUT to a test intermediary, which holds the
complete signed request. The test forcibly disconnects that container from its
only network, verifies the object is not yet present, and releases the
intermediary. MinIO then confirms the PUT and the observer sees a versioned
object even though the legacy container is disconnected. A new TCP
connection then fails, while an independent observer can still access the
bucket using the same, still-active old identity. In the observed run, a new
request through the previously used SDK client did not produce a terminal
result within the bounded observation period and was classified UNKNOWN. The
probe process then exited; this is not evidence that a provider-side request
was drained. No Docker socket is mounted into the legacy container.

```powershell
./scripts/issue86-isolated-network-fence.ps1 -DependencyVenv <linux-venv-path> -MinioClient <linux-mc-path>
```

This fixture uses an internal network and synthetic resources, publishes no
host ports, and removes only its own containers and network. Override `-Subnet`
if needed. It demonstrates a local network boundary; it does not validate AWS
identity or network controls, the full old RQ worker in that namespace, or
terminal outcomes for arbitrary requests submitted before the disconnect. The
intermediary is a controlled test fixture, not a deployed production proxy.
The same script also runs the separate buffered-proxy regression described
below, which demonstrates a committed PUT after its originating process dies;
it is a different scenario from the network-disconnect observation.

A separate opt-in loopback MinIO test buffers an entire signed PUT in an
intermediary, kills the originating process, then forwards the request. The
provider accepts it and returns a version after the process has exited. This
models one intermediary buffering behavior and demonstrates why process death
alone is not a drain proof; it does not characterize an AWS network path.

AWS documents that a request started before a presigned URL expires may
continue after expiry; the cited example is a download and does not establish
PUT revocation semantics. AWS also documents that a version-specific DELETE
permanently removes that version without inserting a delete marker. These facts
do not establish a provider-side in-flight write drain or a safe automatic
deletion protocol. See [presigned URL expiration](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html)
and [S3 versioning workflows](https://docs.aws.amazon.com/AmazonS3/latest/userguide/versioning-workflows.html).

Report each gate as PASS, FAIL, NOT_RUN or UNKNOWN, with its evidence and exact
deployment/provider scope. Do not promote local MinIO results to AWS results.
If the provider cannot furnish enforceable isolation and terminal-outcome
evidence, leave deletion manual and the operation ledger blocking; do not infer
success from the absence of a recently observed request.

For a deployment that actually used an IAM **user**, an observer with
`sts:GetCallerIdentity`, `iam:GetUser` and `iam:ListAccessKeys` permissions can
inventory that user's long-term keys with:

```sh
python label_studio/manage.py check_s3_legacy_iam_user \
  --account-id <account-id> --user-name <user-name> --user-id <historical-immutable-user-id>
```

Obtain the immutable user ID from historical deployment records rather than
looking up the current user by name: the same name can refer to a recreated
principal. The command checks the account and user identity, paginates the key
inventory, and reports counts only. Incomplete inventory, access denial and
identity mismatch fail closed. It accepts standard AWS IAM and STS endpoints
only, makes no cloud changes, and always reports isolation and request drain as
UNKNOWN. An inactive or empty key list does not rule out temporary credentials,
other identities, presigned requests, network access, or in-flight requests.
It does not support proving isolation for roles, root, federated or unknown
principals. IAM is eventually consistent; a point-in-time result does not stop
keys being re-enabled or created. See [IAM ListAccessKeys](https://docs.aws.amazon.com/IAM/latest/APIReference/API_ListAccessKeys.html)
and [temporary credential permissions](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_temp_control-access_disable-perms.html).

For a queued, version-pinned S3 annotation deletion, the application provides
the read-only target check below. Supply the UUID already recorded in the
ledger and the independently verified 12-digit AWS bucket-owner account; do not
paste credentials into the command or its output. The configured storage
identity needs permission to inspect the bucket's versioning and the specific
object version, and to list object versions. The command paginates the exact
key's history and reports whether the captured version is noncurrent, current
with other object versions, or current without other object versions. It fails
closed on missing, ambiguous, inaccessible, or excessively long inventories.
The classification is deliberately conservative: another object version is
reported even when an intervening delete marker could hide it. The listing is
not a snapshot while writers remain active; this check is point-in-time and does
not attest to isolation, request drain or safe deletion. A missing target after
a lost response is not evidence that an operation can be acknowledged.

```sh
python label_studio/manage.py check_s3_deletion_target \
  --operation-id <pending-operation-uuid> --expected-owner <trusted-account-id>
```

For newly admitted S3 annotation PUTs, the operation ledger now commits the
target key, request token, subject and storage binding before issuing the PUT.
Configure `expected_bucket_owner` on the S3 export storage before the PUT to
bind the 12-digit AWS account to that request and send AWS
`ExpectedBucketOwner` with the PUT. The inspection commands require the same
owner and an owner-bound storage identity. Historical v1 bindings cannot be
upgraded by setting an owner afterward; they remain blocked for manual
reconciliation. The nullable field preserves legacy writes, which do not gain
owner-bound evidence. Custom S3 endpoints are not accepted for AWS inspection,
even if they accept or ignore the owner header. Reversing migration 0024 is
blocked while any owner is configured, any storage operation exists, or any
S3 version binding remains, because legacy and owner-bound hashes have no
independent algorithm marker.
Newly admitted annotation PUTs use an SDK client configured for one total
attempt, overriding the environment's ordinary SDK retry setting. A failed or
lost response remains blocking rather than prompting an automatic SDK retry.
This reduces duplicate attempts; it does not prove that an already submitted
request, an intermediary, other writers, older clients, or provider-side work
has stopped. It does not establish cloud isolation or authorize deletion. See
[Boto3 retry configuration](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/retries.html).

Within one PostgreSQL database, a transaction-scoped advisory lock serializes
pre-send admission for the same S3 object key across projects and S3 export
configurations. A nonterminal earlier export for that key blocks the
new PUT; any nonterminal export whose key is unknown blocks all S3
export keys. New pre-send records commit the object key under that same lock
before their worker can pause or send anything. Same-key pre-send contenders
may both be rejected and retried later. Equal keys in different buckets are
deliberately over-serialized. A blocked pre-send child is cancelled only when
its v2 record still has no request token; its queued parent is retained for
delayed redispatch. Failures after
submission remain uncertain and blocking. This database fence does not cover
other databases, old binaries, external writers or already in-flight traffic;
those still require separate deployment and cloud isolation.

The same key lock now guards creation of a pending S3 deletion. A nonterminal
S3 export for that key, or an export with unknown key, prevents the
intent from committing. Conversely, a committed pending deletion for that key,
or a deletion with unknown key, prevents a later PUT from reaching the provider.
This includes records from other projects and storage configurations in the
same database. Multiple deletion intents may coexist (including those created
by one annotation deletion); this is not an executor or an authorization to
delete. Any future executor must independently serialize and reconcile those
intents, old writers, object history and in-flight provider requests.

Run the isolated PostgreSQL key-fence and existing S3 admission regressions:

```powershell
./scripts/issue86-isolated-postgres-recovery.ps1 -DependencyVenv <linux-venv-path> -TestS3Fence
```

This is a local database concurrency test, not an AWS or automatic-deletion
acceptance result.

With storage admission enabled, the annotation deletion signal locks the
project before reading S3 links, their pinned versions, or storage settings.
It also rejects a stale annotation whose persisted project or task identity
changed. The database regression pauses deletion after pin inspection and
confirms that a supported concurrent export cannot replace the pin in that
window; after the deletion intent commits, that export remains blocked.
Legacy binaries and other writers that do not take the project lock are not
covered by this guarantee.

Before an admitted PUT, the worker refreshes the annotation under the project
lock and verifies its key against the committed identity. This prevents a
stale in-memory annotation from overwriting a later supported project edit; a
changed key rejects the PUT and retains the token-bearing operation as unknown.
Writers which bypass the project lock are outside this guarantee.
Managers may use `cancel_s3_preflight` on the existing project storage-operation
resolution API only for a v2 S3 export record with a recorded key and no request
token or version. The service checks project scope and permissions, then takes
the same key lock before marking it cancelled. If cancellation wins, the
worker's later attempt to bind a token fails before the provider call; if the
worker binds first, cancellation is refused. The reason and actor are recorded.
This does not reconcile v1, token-bearing or externally submitted operations;
their original unknown outcome still blocks. A queued parent can be retried by
the existing dispatcher, subject to its lifecycle admission checks.

The manager request is `POST /api/projects/<project-id>/storage-operations/<operation-id>/resolve/`
with JSON `{"action":"cancel_s3_preflight","reason":"<reason>","attested":false}`.
The public lifecycle and admission feature gates must already be enabled.
The existing lifecycle settings page offers this action only when the inventory
reports `can_cancel_s3_preflight: true`; the inventory does not expose the
request token, version or internal payload. This boolean is a point-in-time
hint, not authorization: the service rechecks under the key lock, and the page
requires a refresh after an unsuccessful or unknown result.
The cross-project blocker lookup is intentionally conservative and can scan a
large operation ledger; validate its query plan and throughput against the
deployment's data volume before enabling this path.

If the request fails or its response is lost, those fields remain on the
blocking record. For a running or uncertain record with an explicit credential
binding, the following read-only command checks a bounded number of versions for
the original token:

```sh
python label_studio/manage.py check_s3_export_request \
  --operation-id <blocking-operation-uuid> --expected-owner <trusted-account-id>
```

The command returns one bounded page of observed matching version IDs, an
encrypted continuation cursor when needed, and always reports UNKNOWN. Use
`--cursor <cursor> --page-size 25` with the same operation and owner to inspect
the next page; page sizes are 1–50 and a traversal is limited to 100 pages.
[AWS ListObjectVersions](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectVersions.html)
defines the provider continuation markers and expected-owner check.
Cursors expire after one hour and are bound to the record, storage identity,
owner and page size. Every invocation in a traversal must use the same stable
Django `SECRET_KEY`; key rotation invalidates outstanding cursors. The pages are
separate observations, not a snapshot: a
matching version shows an observed object with the token, while absence, a match
or multiple matches do not prove the original worker and retries have stopped.
No total is inferred across pages. If a provider read fails, the same unexpired
cursor may be retried as a new observation; this does not freeze earlier pages.
The command never resolves the ledger entry and is not an authorization to
replay or delete. Existing records without a precommitted token, changed
credentials, custom endpoints, incomplete pagination, unversioned/null
versions or missing permissions fail closed. This facility does not provide
old-process isolation or provider-side request draining.

An additional read-only observation is available for a blocking S3 export or
pending deletion whose original storage identity and expected owner are bound:

```sh
python label_studio/manage.py check_s3_multipart_uploads \
  --operation-id <operation-uuid> --expected-owner <trusted-account-id>
```

Each page reports counts for incomplete multipart uploads on the exact recorded
key; similarly prefixed keys are counted as inspected but excluded from the
match count. Use `--cursor <cursor> --page-size 25` to continue, keeping the
same operation, owner, stable Django `SECRET_KEY` and page size. The encrypted
cursor expires in one hour; uploads IDs and provider messages are not printed.
The traversal is limited to 100 pages with at most 50 uploads per page. AWS
[ListMultipartUploads](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListMultipartUploads.html)
defines its marker and expected-owner semantics. Separate pages are not a
snapshot. An empty page does not exclude a newly initiated upload, an ordinary
PUT, a presigned request, a completion already in flight, another key or bucket,
or a writer outside the recorded identity. Counts do not correlate uploads to
the original request. The command never aborts uploads or resolves a ledger
record; its outcome is always UNKNOWN. Legacy records without a stable binding,
custom endpoints, owner mismatch, suspended versioning, invalid pagination or
missing permissions fail closed and need separate investigation.

For a blocking legacy record that has an exact recorded key but no historical
storage binding, an operator can instead inspect the **current** configured
bucket by adding `--legacy-current-configuration` to that command. This mode
requires explicit current credentials, an independently supplied expected owner,
and a standard AWS endpoint. It returns `CURRENT_CONFIGURATION_ONLY` and
`UNKNOWN`; it does not bind the old record or establish that the old request
addressed this bucket, account, prefix, or identity. A changed current
configuration invalidates the continuation cursor. Records without a reliable
key, unsupported provider endpoints, owner mismatch and inaccessible versioning
remain uninspectable with this command. This legacy-only observation can list
uploads while the current bucket is unversioned or versioning is suspended and
reports the observed versioning state; neither state permits automatic deletion.
Do not use a zero count as evidence to clear the legacy record or authorize deletion.

`scripts/issue86-isolated-multipart.ps1` tests observation against disposable
loopback MinIO. This MinIO build returns a truncated same-key listing without
`NextKeyMarker`; the check rejects that incomplete continuation, while a large
enough single page remains inspectable. Its fixture explicitly bypasses the
AWS endpoint identity check; it does not validate AWS owner enforcement or
real-cloud request drainage. AWS-style pagination is covered by isolated
provider-response tests, not by this MinIO build.

For the local lost-response and process-death regressions, the pinned legacy
fixture script also accepts `-TestS3Unknown`. This exercises a real SDK PUT
against local MinIO with a deliberately dropped response, verifies that a
separate project's same-key deletion and PUT remain blocked, and checks that a
dispatcher does not replay a running operation after its process dies. The
observed token and version do not establish provider request drain or permit
resolution. The fixture is local, not AWS acceptance.

The queue drain inventory also has a reproducible local Redis regression:

```powershell
./scripts/issue86-isolated-queue-inventory.ps1 -DependencyVenv <linux-venv-path>
```

It runs with a fresh Redis on an isolated Docker network namespace. It verifies
that orphan queue keys, expired started/deferred/failed/scheduled/canceled
records, stale worker registrations and unexpected key types block admission
without invoking RQ cleanup or modifying those records. This exercises local
Redis behavior only; it is not evidence that real old processes or cloud
requests have been isolated or drained. See the queue inventory limitations in
`authorization-plan.md` before using its result.

The following local PostgreSQL/Redis regression exercises the nonempty-ledger
rollback guard, mixed outbox records, persisted webhook payloads, version pins,
and isolation observations including existing sessions and prepared transactions:

```powershell
./scripts/issue86-isolated-postgres-recovery.ps1 -DependencyVenv <linux-venv-path>
```

It uses a disposable network namespace and synthetic roles. A rejected destructive
schema reversal preserves the records in this test; it is not a successful
production downgrade or a cloud isolation result.
