# Read-only frozen NER revision comparison (#81)

This opt-in single-task view compares explicitly selected immutable Submission
revisions. It adds no analysis batch, annotation/review/arbitration write, sampling
rule, project aggregate, delivery authority or Host/Core integration. #81 and #84
remain open. Existing classification manifests, metrics and key recovery are unchanged.

## Inputs and formula

Only one static Labels control on one inline, unencoded Text field is supported.
The Text value must be a single top-level ASCII variable (`$text`, using only
letters, digits and underscore). Templates, nested paths, indexes and non-ASCII
variable syntax are unavailable; the reader never substitutes a literal key for
the editor's interpolation/path resolver. The editor's compatibility getter
blocks `__proto__`; that binding is also unavailable even as an own JSON key.
Select at most 20 revisions, one per trusted independent submitter/assignee. An
insufficient single input is unavailable. Full frozen configuration and task data
must agree. Unsupported/mixed/dynamic configurations are unavailable as a whole.

Each entity has one label and a nonempty Unicode-codepoint half-open [start,end)
range. Its saved text must exactly match that slice of the original frozen text.
No trimming, newline conversion, NFC, case folding or tokenization is performed.
Adjacent entities are valid; duplicate, nested or overlapping entities within one
submission are unavailable. An explicit non-cancelled empty result is an empty
entity set; missing results are not empty sets.

`ner-strict-entities/v1`: for each unordered pair A/B, F1 = 2M/(|A|+|B|), where M
counts entities with identical boundaries and label. Fractions are reduced integer
strings, never hashed floats. One empty side scores zero. Two empty sides have no
score, not 100%. Three inputs retain all three pairs; there is no overall average.

Reuse the MIT nervaluate 0.2.0 strict exact counter. This dependency supports the
fork's Python 3.10 baseline without runtime dependencies; newer releases require
3.11. Convert to the library's inclusive end with end-1 and consume only its
strict correct integer, validating it against canonical exact correspondences.
Do not expose gold/prediction or directed fuzzy counters as annotation authority.
A counter contract failure is 503, never a fallback scoring algorithm.

Unmatched entities are grouped by positive-length overlap. Report label-only,
boundary-only, combined, one-side-only or ambiguous many-entity groups. Touching
endpoints are not overlap; no greedy match or overlap threshold earns exact credit.

## GET transport, live authority and receipts

Under `/api/projects/{P}/ner-disagreements/`: capability, tasks/{T}/sources and
tasks/{T}. The latter takes either explicit submissions, or frozen with optional
left/right IDs from those same inputs. Reject unknown/repeated parameters,
unsafe/duplicate IDs and duplicate participants. No new Idempotency-Key exists.

`ANNOTATION_NER_DISAGREEMENTS_ENABLED=false` and independent exact org:project
`ANNOTATION_NER_DISAGREEMENT_MODES` fork_review entries control all reads. Require
current trusted identity, Manager/Reviewer, organization/project/task read scope
and READ_WORK. Annotators, Core-only-review and unknown modes deny. Filter Reviewer
self-inputs before counts/loading. An explicit forbidden input rejects the request.

The independently salted signed receipt pins identities, revision/content/config/
task-data hashes, rule/explanation/engine, original calculation time, result hash
and viewer HMAC. Current authority and sources are checked again after actual JSON
render. Fallback keys remain usable while trusted; retired keys reject. Receipts
contain no raw answers and promise no permanent metric retention.

New revisions or normal review transitions do not substitute frozen inputs. A
missing pinned source masks all scores/text/differences, retaining the original
input/pair denominator. Foreign/rebound or corrupt sources reject; no partial
projection survives. Cross-classification/NER receipts are invalid. Responses are
private/no-store. Browser blur/scope changes invalidate pending reads and clear
answers; refresh and pair selection are GET-only and retain the original receipt.

## Preflight and bounded cost

Sources enumerate identity metadata only, at most 100. PostgreSQL/SQLite SQL byte
preflight limits selected stored JSON to 64MiB before decoding. Canonical snapshots
then have an independent inclusive 8MiB limit. Unlike the older shared loader's
post-decode counting, this new read path does not hydrate a giant JSON value first.
The hydrating SELECT also uses a cumulative window byte sum and a CASE-projected
JSON value, with the original JSON column deferred. If rows grow after preflight,
the statement returns no oversized payload for Python to decode. A source-set
change during loading rejects 409 before normalization or receipt construction;
it cannot create a partial initial receipt or turn a race into a 500.
It does not retrofit a new limit onto legacy classification routes.

Additional inclusive limits: 64KiB XML and UTF-8 text, 100 static labels of at most
256 UTF-8 bytes, 200 entities per revision, 1MiB logical/actual output and 16KiB
uncompressed signed receipt. Compressed input receipts reject. Oversize rejects
422 without truncation, participant removal, sampling or a background job.

For pair i/j let n be entity counts, L summed entity lengths, T configured labels.
Preflight W = sum(n_i*n_j + n_j*L_i + n_i*L_j + T*(n_i+n_j+1)) <= 5,000,000 per
pass, covering the pinned library's range-set enumeration. At most two passes and
380 library calls occur. No per-pair database queries or HTTP/media resolution.
Logical, response and receipt bounds precede the first scoring call.

The repository Nginx entrypoint explicitly allows a 32KiB request line through
four demand-allocated 32KiB large-header buffers. This carries the existing 16KiB
GET receipt budget plus route/query framing; the API limit remains 16KiB.
Twenty ordinary pins already produce a receipt above the default 8KiB Nginx
request-line limit, so direct-to-Django tests alone cannot validate replay.
An external ingress must preserve at least the same request-line allowance.
uWSGI's existing 65,535-byte header buffer is unchanged. See the official
[Nginx large-client-header documentation](https://nginx.org/en/docs/http/ngx_http_core_module.html#large_client_header_buffers).

## Verification and retained environment

Pure tests exercise hand-calculated and 128 deterministic three-participant cases,
direction/order invariance, Unicode/CRLF, invalid/empty values, ambiguous overlap,
actual entity/text edges and the maximum scorer-call count. API tests cover source
metadata/preflight, role/mode/identity scope, revocation, corrupt/missing sources,
new revisions, key rotation and actual rendered-byte tampering. PostgreSQL barrier
tests use independent connections at the rendered-response boundary.

Dedicated synthetic browser journeys cover en-US/zh-CN Manager/Reviewer/Annotator,
frozen navigation, delayed responses, budgets, source deletion and revocation. A
separate real editor selection/draft reload/submission case must prove emoji and
CRLF codepoints; hand-authored fixture JSON alone is insufficient evidence.
The NER browser job runs through the adapted production Nginx config, with an
isolated 8KiB negative-control proxy. Both languages replay a real 20-participant
receipt: the old limit returns 414, the production allowance returns 200 with
the identical frozen hash, and an over-budget receipt still reaches the API's
400 rejection. This proxy validation runs only on the ephemeral GitHub runner.

Validation results are recorded separately; test definitions are not PASS. Local
Docker/#76 are read-only. Historical CHANGED, CHANGED_DATABASE_CONTAINER_REPLACED,
cause UNKNOWN, absent protected worktree and full database comparison
NOT_RUN_AFTER_DOCKER_INTERRUPTION are retained, not overwritten by current checks.
