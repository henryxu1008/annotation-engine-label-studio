"""Bounded static TextArea comparison over frozen snapshots. No ORM or I/O."""

import itertools
import re

import defusedxml.ElementTree as etree
from defusedxml.common import DefusedXmlException

from .ner import digest, fraction

SCHEMA = 'field-disagreements/v1'
RULE = 'field-lf-ascii-trim/v1'
EXPLANATION = 'field-value-differences/v1'
ENGINE = 'python-string-equality/v1'
MAX_INPUTS = 20
MAX_SOURCES = 100
MAX_FIELDS = 20
MAX_VALUE_BYTES = 4096
MAX_VALUES_BYTES = 65536
MAX_TEXT_BYTES = 65536
MAX_XML_BYTES = 65536
MAX_STORED_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024
MAX_TOKEN = 16384
TOO_LARGE = 'FIELD_TOO_LARGE'
SOURCE_INTEGRITY_FAILED = 'FIELD_SOURCE_INTEGRITY_FAILED'


class Error(Exception):
    def __init__(self, code, status=422):
        self.code, self.status = code, status


class Incomparable(Exception):
    pass


def bounded(value, maximum=None):
    from . import ner

    try:
        return ner.bounded(value, MAX_OUTPUT if maximum is None else maximum)
    except ner.Error as exc:
        raise Error('FIELD_TOO_LARGE', exc.status) from exc


def normalize(value):
    return value.replace('\r\n', '\n').replace('\r', '\n').strip(' \t\n\v\f')


def controls(xml):
    if not isinstance(xml, str):
        raise Incomparable('unsupported_config')
    if len(xml.encode()) > MAX_XML_BYTES:
        raise Error('FIELD_TOO_LARGE')
    try:
        root = etree.fromstring(xml)
    except (etree.ParseError, ValueError, DefusedXmlException):
        raise Incomparable('unsupported_config')
    nodes = list(root.iter())
    allowed = {
        'View': {'style', 'classname'},
        'Text': {'name', 'value', 'valuetype', 'encoding', 'savetextresult'},
        'TextArea': {
            'name',
            'toname',
            'perregion',
            'peritem',
            'maxsubmissions',
            'required',
            'editable',
            'rows',
            'placeholder',
            'showsubmitbutton',
        },
    }
    if root.tag != 'View':
        raise Incomparable('unsupported_config')
    for node in nodes:
        attrs = {k.lower(): v for k, v in node.attrib.items()}
        if node.tag not in allowed or len(attrs) != len(node.attrib) or not set(attrs) <= allowed[node.tag]:
            raise Incomparable('unsupported_config')
        node.attrib.clear()
        node.attrib.update(attrs)
        if node.tag != 'View' and list(node):
            raise Incomparable('unsupported_config')
    texts = [n for n in nodes if n.tag == 'Text']
    fields = [n for n in nodes if n.tag == 'TextArea']
    if len(fields) > MAX_FIELDS:
        raise Error('FIELD_TOO_LARGE')
    if len(texts) != 1 or not fields:
        raise Incomparable('unsupported_config')
    text = texts[0]
    target, binding = text.get('name'), text.get('value', '')
    if (
        not target
        or not re.fullmatch(r'\$[A-Za-z0-9_]+', binding)
        or binding == '$__proto__'
        or text.get('valuetype', 'text') != 'text'
        or text.get('encoding', 'none') != 'none'
        or text.get('savetextresult', 'yes') not in ('yes', 'none')
    ):
        raise Incomparable('unsupported_config')
    names = [f.get('name') for f in fields]
    if (
        any(not n or len(n.encode()) > 256 or '$' in n or n == target for n in names)
        or len(set(names)) != len(names)
        or any(
            f.get('toname') != target
            or f.get('perregion', 'false') != 'false'
            or f.get('peritem', 'false') != 'false'
            or f.get('maxsubmissions') != '1'
            for f in fields
        )
    ):
        raise Incomparable('unsupported_config')
    return {'to_name': target, 'field': binding[1:], 'names': sorted(names)}


def values(snapshot, control):
    annotation = snapshot.get('annotation')
    if not isinstance(annotation, dict) or annotation.get('was_cancelled') is not False:
        raise Incomparable('cancelled_or_missing')
    results = annotation.get('result')
    if not isinstance(results, list):
        raise Incomparable('missing_or_malformed')
    if len(results) > MAX_FIELDS:
        raise Error('FIELD_TOO_LARGE')
    found, used = {}, 0
    for item in results:
        if (
            not isinstance(item, dict)
            or item.get('type') != 'textarea'
            or item.get('from_name') not in control['names']
            or item.get('from_name') in found
            or item.get('to_name') != control['to_name']
            or any(k in item for k in ('parentID', 'parent_id'))
        ):
            raise Incomparable('missing_or_malformed')
        value = item.get('value')
        if (
            not isinstance(value, dict)
            or set(value) != {'text'}
            or not isinstance(value['text'], list)
            or len(value['text']) != 1
            or not isinstance(value['text'][0], str)
        ):
            raise Incomparable('missing_or_malformed')
        raw = value['text'][0]
        size = len(raw.encode())
        used += size
        if size > MAX_VALUE_BYTES or used > MAX_VALUES_BYTES:
            raise Error('FIELD_TOO_LARGE')
        found[item['from_name']] = {'raw': raw, 'normalized': normalize(raw)}
    if set(found) != set(control['names']):
        raise Incomparable('missing_field')
    return found


def prepare(ids, snapshots):
    count = len(ids)
    if count < 2 or count != len(snapshots) or len(set(ids)) != count:
        raise Error('FIELD_INVALID_INPUT', 400)
    if count > MAX_INPUTS:
        raise Error('FIELD_TOO_LARGE')
    used = 0
    for snapshot in snapshots:
        used += bounded(snapshot, MAX_SNAPSHOT_BYTES)
        if used > MAX_SNAPSHOT_BYTES:
            raise Error('FIELD_TOO_LARGE')
    base = {'input_count': count, 'total_pairs': count * (count - 1) // 2, 'configured_fields': None}
    try:
        xmls = [s['project']['label_config'] for s in snapshots]
        if any(x != xmls[0] for x in xmls):
            raise Incomparable('config_mismatch')
        control = controls(xmls[0])
        base['configured_fields'] = len(control['names'])
        data = [s['task']['data'] for s in snapshots]
        if any(digest(d) != digest(data[0]) for d in data):
            raise Incomparable('task_data_mismatch')
        text = data[0].get(control['field']) if isinstance(data[0], dict) else None
        if not isinstance(text, str) or not text:
            raise Incomparable('empty_or_missing_text')
        if len(text.encode()) > MAX_TEXT_BYTES:
            raise Error('FIELD_TOO_LARGE')
        rows = [values(s, control) for s in snapshots]
    except (Incomparable, KeyError, TypeError, AttributeError) as exc:
        return {
            'logical': {
                **base,
                'availability': 'unavailable',
                'reason': str(exc) if isinstance(exc, Incomparable) else 'missing_or_malformed',
                'pairs': None,
                'sources': None,
                'details': None,
            }
        }
    scores, details = [], []
    for a, b in itertools.combinations(range(count), 2):
        entries = []
        for name in control['names']:
            left, right = rows[a][name], rows[b][name]
            informative = bool(left['normalized'] or right['normalized'])
            raw_equal, normalized_equal = left['raw'] == right['raw'], left['normalized'] == right['normalized']
            entries.append(
                {
                    'name': name,
                    'informative': informative,
                    'raw_equal': raw_equal,
                    'normalized_equal': normalized_equal,
                    'kind': 'both_empty'
                    if not informative
                    else 'equal'
                    if raw_equal
                    else 'format_only'
                    if normalized_equal
                    else 'value_difference',
                }
            )
        details.append({'left': ids[a], 'right': ids[b], 'fields': entries})
        denominator = sum(e['informative'] for e in entries)
        raw = sum(e['informative'] and e['raw_equal'] for e in entries)
        normalized = sum(e['informative'] and e['normalized_equal'] for e in entries)
        scores.append(
            {
                'left': ids[a],
                'right': ids[b],
                'denominator': denominator,
                'excluded_empty_fields': len(entries) - denominator,
                'raw_matches': raw,
                'normalized_matches': normalized,
                'raw_rate': fraction(raw, denominator) if denominator else None,
                'normalized_rate': fraction(normalized, denominator) if denominator else None,
                'availability': 'available' if denominator else 'unavailable',
                'reason': None if denominator else 'no_values',
            }
        )
    comparable = sum(p['denominator'] > 0 for p in scores)
    logical = {
        **base,
        'availability': 'available' if comparable else 'unavailable',
        'reason': None if comparable else 'no_values',
        'comparable_pairs': comparable,
        'unavailable_pairs': len(scores) - comparable,
        'pairs': scores,
        'details': details,
        'sources': [{'id': id_, 'values': row} for id_, row in zip(ids, rows)],
        'cost_per_pass': {
            'normalized_values': count * len(control['names']),
            'field_equalities': 2 * len(scores) * len(control['names']),
        },
    }
    # Keep values once per input; pair details contain equality flags only.
    # The public selected-pair projection has a separate output byte gate.
    bounded({'pairs': scores, 'sources': rows}, MAX_SNAPSHOT_BYTES)
    return {'logical': logical}


def public(prepared, selected=None):
    logical = prepared['logical']
    result = {k: v for k, v in logical.items() if k not in ('sources', 'details')}
    result['selected_pair'] = None
    if logical['pairs'] is not None and selected is not None:
        sources = {s['id']: s['values'] for s in logical['sources']}
        detail = next(d for d in logical['details'] if (d['left'], d['right']) == selected)
        entries = [
            {**e, 'left': sources[selected[0]][e['name']], 'right': sources[selected[1]][e['name']]}
            for e in detail['fields']
        ]
        result['selected_pair'] = {**detail, 'fields': entries}
    bounded(result)
    return result
