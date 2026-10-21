# #79 synthetic wire example

**DESIGN_ONLY.** These files were generated without a running Label Studio,
database, queue, media source or converter. IDs, namespace, actors, approvals and
timestamps are fictional protocol examples. No API, worker, permission, standard
format or browser acceptance is claimed. See the
[proposed design](../approved-dataset-export-design.md) for the authoritative scope.

| File | Purpose |
| --- | --- |
| `create-request.json` | Exact selection of three approved source fingerprints; `Idempotency-Key: synthetic-issue79-example` is the separate HTTP header |
| `create-response.json` | Illustrative `202` dispatch-pending response, not an actual server receipt |
| `records.json` | Three native envelopes in the specified compact UTF-8 JSON encoding |
| `records.jsonl` | Identical envelopes, one per line with final LF |
| `manifest.json` | Final canonical manifest: frozen content/input, approval evidence, both native output hashes and byte sizes |
| `SHA256SUMS` | Detached SHA-256 for manifest and native artifact bytes; no circular self-hash |

The directory-local `.gitattributes` preserves the canonical vector bytes across
Windows/Linux checkout; a platform newline conversion must not change artifact
SHA-256 values. It changes no repository-wide or #76 Git setting.

Submission IDs 601/602/603 refer to task IDs 501/502/503 and distinct assignments
801/802/803 with explicitly selected revisions 2/1/3. Records retain the complete
snapshot including original XML and a deliberately large signed legacy
`label_config_hash`. That legacy integer is not the XML SHA-256 and must not be
rounded by a JavaScript client. The Chinese sample verifies exact UTF-8 handling.
The example uses no media; `unverified_reference` describes the profile's media
guarantee, not a successful media checksum.

`create-response.json` describes the initial sealed-input state. The final
`manifest.json` describes a hypothetical later READY state. Their timestamps and
receipt UUID agree, but neither is a live execution assertion. The `status_url`
in the example response names a proposed route, not an available service.

The content digest excludes receipt/actor/time/output-format information. The
input digest additionally binds the request, creating actor, freeze time and
policy. The manifest's detached digest covers actual output hashes and generation
time as well. Changing an authorization access event later must not change any
of these sealed bytes; access events live separately in the future service.

Reproducible example verification from the repository root (PowerShell, CPython
3). The local documentation check uses CPython 3.14.6, without installing a
runtime or borrowing #76's environment. Production encoding remains pinned to
the design's CPython 3.11 contract; its application-level verification is NOT_RUN.

```powershell
@'
import copy, hashlib, json
from pathlib import Path

p = Path('docs/authorization/issue79-approved-export-example')
def c(v):
    return json.dumps(v, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')
def h(b):
    return hashlib.sha256(b).hexdigest()

m = json.loads((p / 'manifest.json').read_bytes())
rows = json.loads((p / 'records.json').read_bytes())
lines = (p / 'records.jsonl').read_bytes()
assert len(rows) == 3 and lines.endswith(b'\n')
assert rows == [json.loads(line) for line in lines.splitlines()]
assert lines == b''.join(c(row) + b'\n' for row in rows)
assert (p / 'records.json').read_bytes() == c(rows)
assert (p / 'manifest.json').read_bytes() == c(m)
assert m['input_sha256'] == h(c(m['input']))
assert m['input']['content_sha256'] == h(c(m['input']['content']))
assert m['input']['request_sha256'] == h(c(m['input']['request']))
assert m['input']['request'] == json.loads((p / 'create-request.json').read_bytes())
config = m['input']['content']['label_config']
assert m['input']['content']['label_config_sha256'] == h(config.encode('utf-8'))
assert [row['source']['id'] for row in rows] == [601, 602, 603]
for row, item in zip(rows, m['input']['content']['items'], strict=True):
    assert row['result_hash'] == h(c(row['result_snapshot']))
    assert item['record_sha256'] == h(c(row))
    assert row['result_snapshot']['project']['label_config'] == config
    assert row['review']['decision'] == 'approved'
    assert row['source'] == item['source']
for a in m['artifacts']:
    b = (p / a['name']).read_bytes()
    assert a['sha256'] == h(b) and a['size_bytes'] == len(b)
    assert a['record_count'] == len(rows)
for line in (p / 'SHA256SUMS').read_text().splitlines():
    digest, name = line.split('  ', 1)
    assert h((p / name).read_bytes()) == digest
response = json.loads((p / 'create-response.json').read_bytes())
assert response['input_sha256'] == m['input_sha256']
assert response['content_sha256'] == m['input']['content_sha256']
assert response['release_id'] == m['input']['release_id']
mutated = copy.deepcopy(rows[0])
mutated['result_snapshot']['task']['data']['text'] += '!'
assert h(c(mutated['result_snapshot'])) != rows[0]['result_hash']
print('PASS: synthetic encoding, selection, digests and tamper vector only')
'@ | py -3 -B -
```

This command writes nothing and does not import or start the application. Real
PostgreSQL negative cases, live revocation, queue recovery, private download,
SDK conversion and en-US/zh-CN E2E remain **NOT_RUN** until implementation.
