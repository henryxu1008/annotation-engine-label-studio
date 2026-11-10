"""Experimental pure comparison; caller must verify pre-annotation media provenance.

This module is not an authorization, storage or media-binding boundary and must
not be exposed by an API until the immutable media proof is implemented.
"""

import hashlib
import itertools
import json
import math
import re
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from fractions import Fraction
from io import BytesIO

import defusedxml.ElementTree as etree
from defusedxml.common import DefusedXmlException
from munkres import Munkres
from PIL import Image, UnidentifiedImageError
from pycocotools import mask as coco_mask

SCHEMA = 'bbox-disagreements/v1'
RULE = 'bbox-axis-aligned-iou50/v1'
ENGINE = 'pycocotools/2.0.11+munkres/1.1.4'
MAX_INPUTS = 20
MAX_BOXES = 20
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_XML_BYTES = 65536
MAX_WORK = 1_000_000
MAX_OUTPUT_BYTES = 1024 * 1024
SCALE = 1_000_000


class Error(Exception):
    def __init__(self, code, status=422):
        self.code, self.status = code, status


class Incomparable(Exception):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def bounded(value, maximum):
    used = 0
    for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(',', ':')).iterencode(value):
        used += len(part.encode('utf-8'))
        if used > maximum:
            raise Error('BBOX_TOO_LARGE')
    return used


def controls(xml):
    if not isinstance(xml, str) or len(xml.encode()) > MAX_XML_BYTES:
        raise Incomparable('unsupported_config')
    try:
        root = etree.fromstring(xml)
    except (etree.ParseError, DefusedXmlException, ValueError):
        raise Incomparable('unsupported_config') from None
    nodes = list(root.iter())
    if root.tag != 'View' or any(n.tag not in ('View', 'Image', 'RectangleLabels', 'Label') for n in nodes):
        raise Incomparable('unsupported_config')
    images = [n for n in nodes if n.tag == 'Image']
    controls_ = [n for n in nodes if n.tag == 'RectangleLabels']
    if len(images) != 1 or len(controls_) != 1:
        raise Incomparable('unsupported_config')
    image, control = images[0], controls_[0]
    for node in nodes:
        attrs = {key.lower(): value for key, value in node.attrib.items()}
        if len(attrs) != len(node.attrib) or any(k in attrs for k in
                ('visiblewhen', 'whentagname', 'whenlabelvalue', 'whenchoicevalue', 'resolver', 'alias')):
            raise Incomparable('unsupported_config')
        node.attrib.clear()
        node.attrib.update(attrs)
    field, name, target = image.get('value', ''), control.get('name'), image.get('name')
    if (not name or not target or name == target or control.get('toname') != target
            or not re.fullmatch(r'\$[A-Za-z0-9_]+', field) or field == '$__proto__'
            or image.get('valuelist') or control.get('choice', 'single') != 'single' or list(image)
            or control.get('value') or any(control.get(k, 'false') != 'false' for k in ('perregion', 'peritem'))):
        raise Incomparable('unsupported_config')
    labels = [n.get('value') for n in control]
    if (not labels or len(labels) > 100 or len(set(labels)) != len(labels)
            or any(n.tag != 'Label' or list(n) for n in control)
            or any(not isinstance(v, str) or not v or '$' in v or len(v.encode()) > 256 for v in labels)
            or sum(n.tag == 'Label' for n in nodes) != len(labels)):
        raise Incomparable('unsupported_config')
    return {'from_name': name, 'to_name': target, 'field': field[1:], 'labels': sorted(labels)}


def image_identity(data):
    if not isinstance(data, bytes) or not data:
        raise Incomparable('unverified_image')
    if len(data) > MAX_IMAGE_BYTES:
        raise Error('BBOX_TOO_LARGE')
    try:
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            if (image.format not in ('PNG', 'JPEG') or not 0 < width * height <= MAX_IMAGE_PIXELS
                    or getattr(image, 'n_frames', 1) != 1 or image.getexif().get(274, 1) != 1):
                raise Incomparable('unsupported_image')
            image.load()
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise Incomparable('unverified_image') from None
    return {'sha256': hashlib.sha256(data).hexdigest(), 'width': width, 'height': height}


def coordinate(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Incomparable('invalid_box')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 <= number <= 100:
            raise Incomparable('invalid_box')
        return int((number * SCALE).to_integral_value(rounding=ROUND_HALF_EVEN))
    except (InvalidOperation, ValueError, OverflowError):
        raise Incomparable('invalid_box') from None


def boxes(snapshot, control, image):
    annotation = snapshot.get('annotation')
    if not isinstance(annotation, dict) or annotation.get('was_cancelled') is not False:
        raise Incomparable('cancelled_or_missing')
    result = annotation.get('result')
    if not isinstance(result, list) or not result:
        raise Incomparable('empty_or_missing_result')
    if len(result) > MAX_BOXES:
        raise Error('BBOX_TOO_LARGE')
    rows = []
    for item in result:
        if (not isinstance(item, dict) or item.get('type') != 'rectanglelabels'
                or item.get('from_name') != control['from_name'] or item.get('to_name') != control['to_name']
                or type(item.get('original_width')) is not int or item['original_width'] != image['width']
                or type(item.get('original_height')) is not int or item['original_height'] != image['height']
                or type(item.get('image_rotation')) not in (int, float) or item['image_rotation'] != 0):
            raise Incomparable('invalid_box')
        value = item.get('value')
        if not isinstance(value, dict) or set(value) != {'x', 'y', 'width', 'height', 'rotation', 'rectanglelabels'}:
            raise Incomparable('invalid_box')
        label = value['rectanglelabels']
        if (type(value['rotation']) not in (int, float) or value['rotation'] != 0
                or not isinstance(label, list) or len(label) != 1 or label[0] not in control['labels']):
            raise Incomparable('invalid_box')
        x, y, w, h = (coordinate(value[k]) for k in ('x', 'y', 'width', 'height'))
        raw_x, raw_y, raw_w, raw_h = (Decimal(str(value[k])) for k in ('x', 'y', 'width', 'height'))
        if (raw_w <= 0 or raw_h <= 0 or raw_x + raw_w > 100 or raw_y + raw_h > 100
                or w <= 0 or h <= 0 or x + w > 100 * SCALE or y + h > 100 * SCALE):
            raise Incomparable('invalid_box')
        rows.append((label[0], x, y, w, h))
    rows.sort()
    if len(set(rows)) != len(rows):
        raise Incomparable('duplicate_box')
    return rows


def iou(a, b):
    _, ax, ay, aw, ah = a
    _, bx, by, bw, bh = b
    intersection = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(0, min(ay + ah, by + bh) - max(ay, by))
    return Fraction(intersection, aw * ah + bw * bh - intersection)


def compare(left, right):
    if not left or not right:
        return {'availability': 'unavailable', 'reason': 'empty_or_missing_result', 'f1': None}
    size = len(left) + len(right)
    cardinality_bonus = (min(len(left), len(right)) + 1) * SCALE
    matrix = [[0] * size for _ in range(size)]
    values = {}
    try:
        coco = coco_mask.iou([list(row[1:]) for row in left], [list(row[1:]) for row in right], [0] * len(right))
        if coco.shape != (len(left), len(right)):
            raise ValueError('unexpected IoU dimensions')
    except Exception as exc:
        raise Error('BBOX_METRIC_ENGINE_FAILURE', 503) from exc
    for a, first in enumerate(left):
        for b, second in enumerate(right):
            overlap = iou(first, second)
            try:
                observed = float(coco[a, b])
            except (TypeError, ValueError, OverflowError) as exc:
                raise Error('BBOX_METRIC_ENGINE_FAILURE', 503) from exc
            if not math.isfinite(observed) or abs(observed - float(overlap)) > 1e-12:
                raise Error('BBOX_METRIC_ENGINE_FAILURE', 503)
            if first[0] == second[0] and overlap >= Fraction(1, 2):
                units = overlap.numerator * SCALE // overlap.denominator
                matrix[a][b] = -(cardinality_bonus + units)
                values[(a, b)] = overlap
    try:
        assigned = Munkres().compute(matrix)
    except Exception as exc:
        raise Error('BBOX_METRIC_ENGINE_FAILURE', 503) from exc
    matches = [{'left': a, 'right': b, 'iou': {'numerator': str(values[(a, b)].numerator),
                                               'denominator': str(values[(a, b)].denominator)}}
               for a, b in assigned if (a, b) in values]
    matches.sort(key=lambda m: (m['left'], m['right']))
    matched_left, matched_right = {m['left'] for m in matches}, {m['right'] for m in matches}
    score = Fraction(2 * len(matches), len(left) + len(right))
    return {'availability': 'available', 'reason': None,
            'f1': {'numerator': str(score.numerator), 'denominator': str(score.denominator)},
            'matches': matches, 'left_only': [i for i in range(len(left)) if i not in matched_left],
            'right_only': [i for i in range(len(right)) if i not in matched_right]}


def prepare(ids, snapshots, frozen_image_bytes):
    if (not ids or len(ids) != len(snapshots) or any(type(i) is not int or not 0 < i <= 2**53 - 1 for i in ids)
            or ids != sorted(set(ids))):
        raise Error('BBOX_INVALID_INPUT', 400)
    if len(ids) > MAX_INPUTS:
        raise Error('BBOX_TOO_LARGE')
    try:
        used = 0
        for snapshot in snapshots:
            used += bounded(snapshot, MAX_SNAPSHOT_BYTES - used)
        task_id, project_id = snapshots[0]['task']['id'], snapshots[0]['project']['id']
        if (type(task_id) is not int or task_id <= 0 or type(project_id) is not int or project_id <= 0
                or any(type(s['task']['id']) is not int or s['task']['id'] != task_id
                       or type(s['project']['id']) is not int or s['project']['id'] != project_id
                       for s in snapshots)):
            raise Incomparable('scope_mismatch')
        xml = snapshots[0]['project']['label_config']
        if any(s['project']['label_config'] != xml for s in snapshots):
            raise Incomparable('config_mismatch')
        control = controls(xml)
        data = [s['task']['data'] for s in snapshots]
        if any(digest(d) != digest(data[0]) for d in data):
            raise Incomparable('task_data_mismatch')
        if not isinstance(data[0], dict) or not isinstance(data[0].get(control['field']), str):
            raise Incomparable('missing_image_reference')
        image = image_identity(frozen_image_bytes)
        rows = [boxes(s, control, image) for s in snapshots]
    except (Incomparable, KeyError, TypeError, AttributeError, UnicodeError, ValueError) as exc:
        return {'availability': 'unavailable', 'reason': str(exc) if isinstance(exc, Incomparable)
                else 'missing_or_malformed', 'input_count': len(ids), 'pairs': None}
    work = sum((len(a) + len(b)) ** 3 for a, b in itertools.combinations(rows, 2))
    if work > MAX_WORK:
        raise Error('BBOX_TOO_LARGE')
    pairs = [{'left': ids[a], 'right': ids[b], **compare(rows[a], rows[b])}
             for a, b in itertools.combinations(range(len(ids)), 2)]
    outcome = {'availability': 'available' if pairs else 'unavailable',
            'reason': None if pairs else 'insufficient_inputs', 'input_count': len(ids),
            'image': image, 'control': control, 'boxes': rows, 'pairs': pairs}
    bounded(outcome, MAX_OUTPUT_BYTES)
    return outcome
