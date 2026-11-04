"""Deterministic four-entry ZIP; frozen public projection, never source hydration."""
import zipfile
from collections import Counter

from jsonschema import Draft202012Validator, ValidationError

from . import audit_contract as c

TYPES = [
    'batch',
    'candidate',
    'revision',
    'initial_review',
    'difference',
    'second_review',
    'second_opinion',
    'concern_followup',
    'cancellation',
    'arbitration_decision',
    'gap',
    'handoff',
    'arbitration',
    'boundary_source_ref',
    'edge',
]
PUBLIC_FIELDS = {
    'type',
    'id',
    'source',
    'target',
    'relation',
    'source_commitment',
    'actor_ref',
    'algorithm',
    'candidate_count',
    'selection_rule',
    'history_completeness',
    'revision',
    'state',
    'submitted_at',
    'evidence',
    'decision',
    'created_at',
    'reason_state',
    'association',
    'batch_execution_provenance',
    'actor_incarnation',
    'reason',
    'gap',
    'added_count',
    'removed_count',
    'selected',
    'selection_reason',
    'confidence_covered',
    'effect',
    'outcome',
    'disposition',
    'rule',
    'mode',
}
HASH = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
COUNT = {'type': 'integer', 'minimum': 0, 'maximum': 15000}
ENUMS = {
    'type': TYPES,
    'algorithm': ['sha256-ranked-v1', 'classification-conflict-ranked/v1', 'revision-confidence-ranked/v1'],
    'selection_rule': ['frozen-batch/v1'],
    'history_completeness': ['not_provable'],
    'state': [
        'pending',
        'approved',
        'rejected',
        'superseded',
        'unavailable',
        'equal',
        'changed',
        'completed',
        'cancelled',
        'resolved',
    ],
    'evidence': ['observed_at_capture', 'frozen_receipt_verified'],
    'decision': ['approved', 'rejected', 'adopt_exact_submission'],
    'reason_state': ['absent', 'present_redacted', 'unavailable'],
    'association': ['subject_observed'],
    'batch_execution_provenance': ['not_recorded'],
    'actor_incarnation': ['not_recorded'],
    'reason': ['initial_review_unavailable', 'second_opinion_unavailable'],
    'gap': [
        'initial_review_unavailable',
        'live_origin_unavailable',
        'second_opinion_unavailable',
        'difference_input_unavailable',
        'revision_sequence_gap',
    ],
    'selection_reason': [
        'frozen_conflict',
        'frozen_agreement',
        'not_covered',
        'revision_mismatch',
        'metric_unavailable',
        'frozen_low_confidence',
        'frozen_not_low_confidence',
        'unsupported_input',
        'revision_binding_changed',
        'source_unavailable',
        'frozen_selection',
    ],
    'effect': [c.EFFECT],
    'outcome': ['confirmed', 'concern', None],
    'disposition': ['no_change', 'revision_linked'],
    'rule': ['adopt-exact-submission/v1'],
    'mode': ['dispatch', 'link_existing'],
    'relation': [
        'root_assignment_revision',
        'initial_review',
        'initial_review_gap',
        'before',
        'after',
        'candidate',
        'frozen_source',
        'supervision',
        'opinion',
        'opinion_gap',
        'cancellation',
        'retained_supervision',
        'followup',
        'retained_opinion',
        'concern_disposition',
        'retained_root_assignment_revision',
        'related_revision',
        'direct_handoff',
        'arbitration',
        'authoritative_decision',
        'cohort_boundary',
    ],
}
PROPERTIES = {key: {'enum': values} for key, values in ENUMS.items()}
PROPERTIES.update({key: HASH for key in ('id', 'source', 'target', 'source_commitment', 'actor_ref')})
PROPERTIES.update(
    {key: {'type': 'string', 'format': 'date-time', 'maxLength': 128} for key in ('submitted_at', 'created_at')}
)
PROPERTIES.update(
    candidate_count=COUNT,
    revision={'type': 'integer', 'minimum': 1, 'maximum': 2**53 - 1},
    added_count={'anyOf': [COUNT, {'type': 'null'}]},
    removed_count={'anyOf': [COUNT, {'type': 'null'}]},
    selected={'type': 'boolean'},
    confidence_covered={'type': ['boolean', 'null']},
)
assert set(PROPERTIES) == PUBLIC_FIELDS
REQUIRED = {
    'batch': ['algorithm', 'candidate_count', 'selection_rule', 'history_completeness'],
    'candidate': ['selected', 'revision', 'selection_reason', 'confidence_covered'],
    'revision': ['revision', 'state', 'evidence'],
    'initial_review': [
        'decision',
        'created_at',
        'reason_state',
        'association',
        'batch_execution_provenance',
        'actor_incarnation',
    ],
    'difference': ['state', 'added_count', 'removed_count'],
    'second_review': ['state', 'effect'],
    'second_opinion': ['outcome', 'effect', 'created_at', 'reason_state', 'actor_incarnation'],
    'concern_followup': ['disposition', 'effect', 'created_at', 'reason_state', 'actor_incarnation'],
    'cancellation': ['created_at', 'reason_state'],
    'arbitration_decision': ['decision', 'rule', 'created_at', 'reason_state', 'actor_incarnation'],
    'gap': ['reason'],
    'handoff': ['mode', 'created_at'],
    'arbitration': ['state', 'rule', 'reason_state'],
    'boundary_source_ref': ['evidence'],
    'edge': ['source', 'target', 'relation'],
}
RECORD = dict(
    type='object',
    required=['type'],
    additionalProperties=False,
    properties=PROPERTIES,
    allOf=[
        {
            'if': {'properties': {'type': {'const': 'edge'}}},
            'then': {'required': ['source', 'target', 'relation'], 'not': {'required': ['id']}},
            'else': {
                'required': ['id'],
                'not': {'anyOf': [{'required': [key]} for key in ('source', 'target', 'relation')]},
            },
        }
    ],
)
RECORD['allOf'] += [
    {'if': {'properties': {'type': {'const': kind}}}, 'then': {'required': fields}}
    for kind, fields in REQUIRED.items()
]
PROJECTION_SCHEMA = dict(
    type='object',
    additionalProperties=False,
    required=[
        'schema',
        'profile',
        'effect',
        'package_id',
        'captured_at',
        'coverage',
        'evidence',
        'history_completeness',
        'live_source_integrity',
        'records',
        'counts',
        'gaps',
    ],
    properties={
        'schema': {'const': c.SCHEMA},
        'profile': {'const': c.PROFILE},
        'effect': {'const': c.EFFECT},
        'package_id': {'type': 'string', 'format': 'uuid'},
        'captured_at': {'type': 'string', 'format': 'date-time', 'maxLength': 128},
        'coverage': {'const': 'complete'},
        'evidence': {'enum': ['complete', 'with_gaps']},
        'history_completeness': {'const': 'not_provable'},
        'live_source_integrity': {'const': 'not_checked'},
        'records': {'type': 'array', 'maxItems': 15000, 'items': RECORD},
        'counts': {'type': 'object', 'additionalProperties': {'type': 'integer', 'minimum': 0, 'maximum': 15000}},
        'gaps': {'type': 'object', 'additionalProperties': {'type': 'integer', 'minimum': 0, 'maximum': 15000}},
    },
)
VALIDATOR = Draft202012Validator(PROJECTION_SCHEMA, format_checker=Draft202012Validator.FORMAT_CHECKER)


def projection(blob):
    value = c.strict(blob, c.limits()['projection'])
    try:
        VALIDATOR.validate(value)
    except ValidationError:
        c.fail('PROJECTION_INVALID')
    if c.canonical(value) != blob.encode('utf-8'):
        c.fail('PROJECTION_INVALID')
    entities = {r['id'] for r in value['records'] if r['type'] != 'edge'}
    if len(entities) != sum(r['type'] != 'edge' for r in value['records']):
        c.fail('PROJECTION_INVALID')
    if any(
        r['source'] not in entities or r['target'] not in entities for r in value['records'] if r['type'] == 'edge'
    ):
        c.fail('PROJECTION_INVALID')
    edges = [(r['source'], r['target'], r['relation']) for r in value['records'] if r['type'] == 'edge']
    counts = dict(Counter(r['type'] for r in value['records']))
    gaps = dict(Counter(r['gap'] for r in value['records'] if 'gap' in r))
    roots = [r for r in value['records'] if r['type'] == 'batch']
    if (
        len(set(edges)) != len(edges)
        or counts != value['counts']
        or gaps != value['gaps']
        or len(roots) != 1
        or roots[0]['candidate_count'] != counts.get('candidate', 0)
        or sum(r['relation'] == 'candidate' for r in value['records'] if r['type'] == 'edge')
        != counts.get('candidate', 0)
        or sum(r['relation'] == 'frozen_source' for r in value['records'] if r['type'] == 'edge')
        != counts.get('candidate', 0)
        or value['evidence'] != ('with_gaps' if gaps else 'complete')
    ):
        c.fail('PROJECTION_INVALID')
    by_id = {row['id']: row for row in value['records'] if row['type'] != 'edge'}
    candidates = {key for key, row in by_id.items() if row['type'] == 'candidate'}
    candidate_edges = [(a, b) for a, b, relation in edges if relation == 'candidate']
    frozen_edges = [(a, b) for a, b, relation in edges if relation == 'frozen_source']
    if (
        any(a != roots[0]['id'] or b not in candidates for a, b in candidate_edges)
        or {b for _, b in candidate_edges} != candidates
        or {a for a, _ in frozen_edges} != candidates
        or any(by_id[b]['type'] != 'revision' for _, b in frozen_edges)
    ):
        c.fail('PROJECTION_INVALID')
    return value


def files(package, bundle):
    value = projection(package.projection)
    fmt = bundle['formatter']
    if fmt != c.formatter():
        c.fail('RUNTIME_CHANGED')
    readme = fmt['readme'].encode('utf-8')
    if c.digest(readme) != fmt['readme_sha256']:
        c.fail('INPUT_INVALID')
    records = b''.join(c.canonical(row) + b'\n' for row in value['records'])
    if len(records) > c.limits()['projection']:
        c.fail('TOO_LARGE', 422)
    payloads = {'records.jsonl': records, 'README.txt': readme}
    manifest = c.canonical(
        dict(
            schema='review-audit-manifest/v1',
            format=c.FORMAT,
            profile=c.PROFILE,
            effect=c.EFFECT,
            package_id=str(package.pk),
            captured_at=value['captured_at'],
            coverage=value['coverage'],
            evidence=value['evidence'],
            history_completeness='not_provable',
            live_source_integrity='not_checked',
            counts=value['counts'],
            gaps=value['gaps'],
            files=[
                dict(name=name, size_bytes=len(blob), sha256=c.digest(blob)) for name, blob in sorted(payloads.items())
            ],
        )
    )
    if len(manifest) > 256 * 1024:
        c.fail('TOO_LARGE', 422)
    payloads['manifest.json'] = manifest
    payloads['SHA256SUMS'] = ''.join(f'{c.digest(blob)}  {name}\n' for name, blob in sorted(payloads.items())).encode(
        'ascii'
    )
    return payloads


class CappedWriter:
    def __init__(self, target, maximum):
        self.target, self.maximum = target, maximum

    def write(self, data):
        if self.target.tell() + len(data) > self.maximum:
            c.fail('SCRATCH_LIMIT', 422)
        return self.target.write(data)

    def __getattr__(self, name):
        return getattr(self.target, name)


def write(package, bundle, target, *, maximum=None):
    payloads = files(package, bundle)
    fmt = bundle['formatter']
    if sum(map(len, payloads.values())) > c.limits()['artifact']:
        c.fail('TOO_LARGE', 422)
    capped = CappedWriter(target, min(c.limits()['artifact'], maximum or c.limits()['artifact']))
    with zipfile.ZipFile(capped, 'w', compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for name in fmt['entries']:
            info = zipfile.ZipInfo(name, tuple(fmt['timestamp']))
            info.create_system = 3
            info.external_attr = fmt['permissions'] << 16
            archive.writestr(info, payloads[name])
    return payloads['manifest.json']


def verify(handle, package):
    """Authenticate the complete fixed inventory before any Range delivery."""
    bundle = c.strict(package.input_bundle, c.limits()['input'])
    value = projection(package.projection)
    try:
        with zipfile.ZipFile(handle) as archive:
            entries = archive.infolist()
            if (
                [i.filename for i in entries] != bundle['formatter']['entries']
                or len(entries) != 4
                or any(
                    i.compress_type != zipfile.ZIP_STORED or i.file_size > c.limits()['projection'] or i.flag_bits & 1
                    for i in entries
                )
            ):
                c.fail('FILE_INVALID')
            manifest = archive.read('manifest.json')
            if manifest != package.manifest.encode('utf-8') or c.digest(manifest) != package.manifest_sha256:
                c.fail('FILE_INVALID')
            decoded = c.strict(manifest, 256 * 1024)
            for item in decoded['files']:
                blob = archive.read(item['name'])
                if len(blob) != item['size_bytes'] or c.digest(blob) != item['sha256']:
                    c.fail('FILE_INVALID')
            expected_records = b''.join(c.canonical(row) + b'\n' for row in value['records'])
            if (
                archive.read('records.jsonl') != expected_records
                or archive.read('README.txt') != bundle['formatter']['readme'].encode()
            ):
                c.fail('FILE_INVALID')
            sums = ''.join(
                f'{c.digest(archive.read(name))}  {name}\n'
                for name in sorted(bundle['formatter']['entries'])
                if name != 'SHA256SUMS'
            ).encode('ascii')
            if archive.read('SHA256SUMS') != sums:
                c.fail('FILE_INVALID')
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError):
        c.fail('FILE_INVALID')
    finally:
        handle.seek(0)
