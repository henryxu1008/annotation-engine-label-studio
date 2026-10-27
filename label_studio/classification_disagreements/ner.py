"""Bounded NER projections over supplied immutable snapshots; no ORM or I/O."""

import hashlib
import itertools
import json
import re
from fractions import Fraction

import defusedxml.ElementTree as etree
from defusedxml.common import DefusedXmlException
from nervaluate import Evaluator

SCHEMA = 'ner-disagreements/v1'
RULE = 'ner-strict-entities/v1'
EXPLANATION = 'ner-overlap-explanation/v1'
ENGINE = 'nervaluate/0.2.0/strict-correct-counter'
MAX_INPUTS = 20
MAX_SOURCES = 100
MAX_STORED_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_XML_BYTES = 65536
MAX_TEXT_BYTES = 65536
MAX_LABELS = 100
MAX_LABEL_BYTES = 256
MAX_ENTITIES = 200
MAX_WORK = 5_000_000
MAX_OUTPUT = 1024 * 1024
MAX_TOKEN = 16384


class Error(Exception):
    def __init__(self, code, status=422):
        self.code, self.status = code, status


class Incomparable(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def bounded(value, maximum=None):
    # iterencode avoids retaining an unbounded extra serialized copy.
    maximum = MAX_OUTPUT if maximum is None else maximum
    used = 0
    for chunk in json.JSONEncoder(sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                                  allow_nan=False).iterencode(value):
        used += len(chunk.encode())
        if used > maximum:
            raise Error('NER_TOO_LARGE')
    return used


def fraction(numerator, denominator):
    value = Fraction(numerator, denominator)
    return {'numerator': str(value.numerator), 'denominator': str(value.denominator)}


def controls(xml):
    if not isinstance(xml, str):
        raise Incomparable('unsupported_config')
    if len(xml.encode()) > MAX_XML_BYTES:
        raise Error('NER_TOO_LARGE')
    try:
        root = etree.fromstring(xml)
    except (etree.ParseError, ValueError, DefusedXmlException):
        raise Incomparable('unsupported_config')
    nodes = list(root.iter())
    if root.tag != 'View' or any(n.tag not in ('View', 'Text', 'Labels', 'Label') for n in nodes):
        raise Incomparable('unsupported_config')
    for node in nodes:
        attrs = {key.lower(): value for key, value in node.attrib.items()}
        if len(attrs) != len(node.attrib) or any(k in attrs for k in (
                'visiblewhen', 'whentagname', 'whenlabelvalue', 'whenchoicevalue', 'resolver', 'alias')):
            raise Incomparable('unsupported_config')
        node.attrib.clear()
        node.attrib.update(attrs)
    texts = [n for n in nodes if n.tag == 'Text']
    labels = [n for n in nodes if n.tag == 'Labels']
    if len(texts) != 1 or len(labels) != 1:
        raise Incomparable('unsupported_config')
    text, control = texts[0], labels[0]
    name, target, field = control.get('name'), text.get('name'), text.get('value', '')
    if (not name or not target or name == target or control.get('toname') != target
            # Match the editor's simple-variable grammar only. Interpolation and
            # path resolution must not be mistaken for a literal task-data key.
            # Its compatibility getter also blocks __proto__ as an own JSON key.
            or not re.fullmatch(r'\$[A-Za-z0-9_]+', field) or field == '$__proto__'
            or text.get('valuetype', 'text') != 'text' or text.get('encoding', 'none') != 'none'
            or text.get('savetextresult', 'yes') not in ('yes', 'none')
            or control.get('choice', 'single') != 'single'
            or any(control.get(k, 'false') != 'false' for k in ('perregion', 'peritem'))
            or control.get('value') or list(text)):
        raise Incomparable('unsupported_config')
    values = [n.get('value') for n in control]
    if len(values) > MAX_LABELS:
        raise Error('NER_TOO_LARGE')
    if (not values or len(set(values)) != len(values)
            or any(n.tag != 'Label' or list(n) for n in control)
            or any(not isinstance(v, str) or not v or '$' in v for v in values)
            or sum(n.tag == 'Label' for n in nodes) != len(values)):
        raise Incomparable('unsupported_config')
    if any(len(v.encode()) > MAX_LABEL_BYTES for v in values):
        raise Error('NER_TOO_LARGE')
    return {'from_name': name, 'to_name': target, 'field': field[1:], 'labels': sorted(values)}


def entities(snapshot, control, text):
    annotation = snapshot.get('annotation')
    if not isinstance(annotation, dict) or annotation.get('was_cancelled') is not False:
        raise Incomparable('cancelled_or_missing')
    values = annotation.get('result')
    if not isinstance(values, list):
        raise Incomparable('missing_or_malformed')
    if len(values) > MAX_ENTITIES:
        raise Error('NER_TOO_LARGE')
    result = []
    for item in values:
        if (not isinstance(item, dict) or item.get('type') != 'labels'
                or item.get('from_name') != control['from_name'] or item.get('to_name') != control['to_name']):
            raise Incomparable('missing_or_malformed')
        value = item.get('value')
        if not isinstance(value, dict) or set(value) != {'start', 'end', 'text', 'labels'}:
            raise Incomparable('missing_span_text_or_malformed')
        start, end, labels = value['start'], value['end'], value['labels']
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                or not isinstance(value['text'], str) or value['text'] != text[start:end]
                or not isinstance(labels, list) or len(labels) != 1 or labels[0] not in control['labels']):
            raise Incomparable('invalid_span')
        result.append({'start': start, 'end': end, 'label': labels[0]})
    result.sort(key=lambda e: (e['start'], e['end'], e['label']))
    if any(a['end'] > b['start'] for a, b in zip(result, result[1:])):
        raise Incomparable('overlapping_or_duplicate_spans')
    return result


def differences(left, right):
    def key(e):
        return e['start'], e['end'], e['label']
    right_keys = {key(e): i for i, e in enumerate(right)}
    matches = [{'left': i, 'right': right_keys[key(e)]} for i, e in enumerate(left) if key(e) in right_keys]
    matched_left, matched_right = {m['left'] for m in matches}, {m['right'] for m in matches}
    nodes = sorted([(e['start'], e['end'], 'left', i) for i, e in enumerate(left) if i not in matched_left]
                   + [(e['start'], e['end'], 'right', i) for i, e in enumerate(right) if i not in matched_right])
    clusters = []
    for start, end, side, index in nodes:
        if not clusters or start >= clusters[-1]['end']:
            clusters.append({'start': start, 'end': end, 'left': [], 'right': []})
        group = clusters[-1]
        group['end'] = max(group['end'], end)
        group[side].append(index)
    for group in clusters:
        a, b = group['left'], group['right']
        if not a or not b:
            kind = 'left_only' if a else 'right_only'
        elif len(a) != 1 or len(b) != 1:
            kind = 'ambiguous_overlap'
        else:
            first, second = left[a[0]], right[b[0]]
            same_bounds = (first['start'], first['end']) == (second['start'], second['end'])
            kind = ('label_difference' if same_bounds else
                    'boundary_difference' if first['label'] == second['label'] else 'boundary_and_label_difference')
        group['kind'] = kind
    return {'matches': matches, 'groups': clusters}


def prepare(ids, snapshots):
    """Normalize, align and reject all budgets before entering the external scorer."""
    count = len(ids)
    if not count or count != len(snapshots):
        raise Error('NER_INVALID_INPUT', 400)
    if count > MAX_INPUTS:
        raise Error('NER_TOO_LARGE')
    used = 0
    for snapshot in snapshots:
        used += bounded(snapshot, MAX_SNAPSHOT_BYTES)
        if used > MAX_SNAPSHOT_BYTES:
            raise Error('NER_TOO_LARGE')
    base = {'input_count': count, 'total_pairs': count * (count - 1) // 2}
    try:
        xmls = [s['project']['label_config'] for s in snapshots]
        if any(xml != xmls[0] for xml in xmls):
            raise Incomparable('config_mismatch')
        control = controls(xmls[0])
        data = [s['task']['data'] for s in snapshots]
        if any(digest(value) != digest(data[0]) for value in data):
            raise Incomparable('task_data_mismatch')
        text = data[0].get(control['field']) if isinstance(data[0], dict) else None
        if not isinstance(text, str) or not text:
            raise Incomparable('empty_or_missing_text')
        if len(text.encode()) > MAX_TEXT_BYTES:
            raise Error('NER_TOO_LARGE')
        normalized = [entities(snapshot, control, text) for snapshot in snapshots]
    except (Incomparable, KeyError, TypeError, AttributeError) as exc:
        logical = {**base, 'availability': 'unavailable', 'reason': str(exc) if isinstance(exc, Incomparable)
                   else 'missing_or_malformed', 'pairs': None, 'text': None, 'sources': None, 'details': None}
        return {'logical': logical, 'engine_inputs': [], 'control': None}
    lengths = [sum(e['end'] - e['start'] for e in row) for row in normalized]
    pairs = list(itertools.combinations(range(count), 2))
    work = sum(len(normalized[a]) * len(normalized[b]) + len(normalized[b]) * lengths[a]
               + len(normalized[a]) * lengths[b]
               + len(control['labels']) * (len(normalized[a]) + len(normalized[b]) + 1) for a, b in pairs)
    if work > MAX_WORK:
        raise Error('NER_TOO_LARGE')
    scores, details = [], []
    for a, b in pairs:
        detail = differences(normalized[a], normalized[b])
        m, n, matched = len(normalized[a]), len(normalized[b]), len(detail['matches'])
        scores.append({'left': ids[a], 'right': ids[b], 'left_entities': m, 'right_entities': n,
                       'matching_entities': matched, 'f1': fraction(2 * matched, m + n) if m + n else None,
                       'availability': 'available' if m + n else 'unavailable',
                       'reason': None if m + n else 'no_entities'})
        details.append({'left': ids[a], 'right': ids[b], **detail})
        # Bound while constructing; never accumulate every oversized pair.
        bounded({'pairs': scores, 'details': details})
    comparable = sum(score['f1'] is not None for score in scores)
    logical = {**base, 'availability': 'available' if comparable else 'unavailable',
               'reason': None if comparable else 'insufficient_inputs' if count < 2 else 'no_entities',
               'comparable_pairs': comparable, 'unavailable_pairs': len(scores) - comparable,
               'text': text, 'control': control, 'sources': [{'id': id_, 'entities': row}
                                                          for id_, row in zip(ids, normalized)],
               'pairs': scores, 'details': details}
    bounded(logical)
    return {'logical': logical, 'engine_inputs': normalized, 'control': control, 'work_units': work}


def check_engine(prepared):
    """The pinned open-source exact counter must agree; never silently replace it."""
    rows = prepared['engine_inputs']
    if not rows:
        return
    for score, (a, b) in zip(prepared['logical']['pairs'], itertools.combinations(range(len(rows)), 2)):
        if not rows[a] and not rows[b]:
            continue
        # nervaluate uses inclusive intervals. No tokenization or text normalization.
        converted = [[{**e, 'end': e['end'] - 1} for e in row] for row in (rows[a], rows[b])]
        try:
            result = Evaluator([converted[0]], [converted[1]], prepared['control']['labels']).evaluate()[0]
            correct = result['strict']['correct']
            if type(correct) is not int or correct != score['matching_entities']:
                raise ValueError('Counter contract mismatch')
        except Exception as exc:
            raise Error('NER_METRIC_ENGINE_FAILURE', 503) from exc


def public(prepared, selected=None):
    logical = prepared['logical']
    value = {key: val for key, val in logical.items() if key not in ('text', 'sources', 'details', 'control')}
    value['selected_pair'] = None
    if logical['pairs'] and selected is not None:
        detail = next((d for d in logical['details'] if (d['left'], d['right']) == selected), None)
        if detail is None:
            raise Error('NER_INVALID_PAIR', 400)
        by_id = {s['id']: s['entities'] for s in logical['sources']}
        value['selected_pair'] = {**detail, 'text': logical['text'], 'control': logical['control'],
                                  'left_entities': by_id[selected[0]], 'right_entities': by_id[selected[1]]}
    return value
