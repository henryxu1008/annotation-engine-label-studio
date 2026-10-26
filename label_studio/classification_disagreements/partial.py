"""Independent Jaccard explanation; never extend the frozen exact-set report."""

import itertools
import json
from fractions import Fraction

from django.core import signing
from rest_framework.exceptions import NotFound
from review_batches.service import digest, fail

from . import batches, service

SCHEMA = 'classification-partial-match/v1'
RULE = 'classification-jaccard-nonempty/v1'
MAX_BYTES = 1024 * 1024
MAX_TOKEN = 16384


def reject(code, status=400):
    fail('CLASSIFICATION_PARTIAL_' + code, status)


def bounded_size(value, limit=None):
    # Do not allocate a second, potentially large encoded result.
    limit = MAX_BYTES if limit is None else limit
    size = 0
    for piece in json.JSONEncoder(sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).iterencode(value):
        size += len(piece.encode('utf-8'))
        if size > limit:
            reject('TOO_LARGE', 422)
    return size


def fraction(value):
    return {'numerator': str(value.numerator), 'denominator': str(value.denominator)}


def calculate(report):
    if report['status'] != 'comparable':
        return {'status': 'unavailable', 'reason': report['reason'], 'score': None, 'controls': []}
    total, controls, used = Fraction(), [], 0
    for location in report['locations']:
        values = sorted(location['values'], key=lambda v: v['submission_id'])
        pairs, subtotal = [], Fraction()
        for left, right in itertools.combinations(values, 2):
            a, b = set(left['choices']), set(right['choices'])
            # The exact-set validator rejects either empty side, including two
            # empty sides. Never inherit a library's empty/empty = 1 convention.
            if not a or not b:
                return {'status': 'unavailable', 'reason': 'missing_or_malformed', 'score': None, 'controls': []}
            common, union = a & b, a | b
            subtotal += Fraction(len(common), len(union))
            pair = {
                'left_submission_id': left['submission_id'], 'right_submission_id': right['submission_id'],
                'intersection_count': len(common), 'union_count': len(union),
                'shared': sorted(common), 'only_left': sorted(a - b), 'only_right': sorted(b - a),
            }
            used += bounded_size(pair)
            if used > MAX_BYTES:
                reject('TOO_LARGE', 422)
            pairs.append(pair)
        score = subtotal / len(pairs)
        total += score
        controls.append({'from_name': location['from_name'], 'to_name': location['to_name'],
                         'score': fraction(score), 'pairs': pairs})
    result = {'status': 'comparable', 'reason': None, 'score': fraction(total / len(controls)), 'controls': controls}
    bounded_size(result)
    return result


def decode(token, *, source=False):
    if not isinstance(token, str) or not token or len(token.encode()) > MAX_TOKEN or (not source and token.startswith('.')):
        reject('INVALID_RECEIPT')
    try:
        value = signing.loads(token, salt=service.SALT if source else SCHEMA)
    except (signing.BadSignature, ValueError, TypeError, UnicodeError):
        reject('INVALID_RECEIPT')
    if not isinstance(value, dict):
        reject('INVALID_RECEIPT')
    return value


def manual_manifest(value, project_id, task_id):
    if set(value) != {'project_id', 'task_id', 'rule', 'calculated_at', 'inputs'}:
        reject('INVALID_RECEIPT')
    if value['project_id'] != project_id or value['task_id'] != task_id or value['rule'] != service.RULE:
        raise NotFound()
    pins = value['inputs']
    if not isinstance(value['calculated_at'], str) or not isinstance(pins, list) or not 1 <= len(pins) <= service.MAX_INPUTS:
        reject('INVALID_RECEIPT')
    ids = []
    for pin in pins:
        if (not isinstance(pin, dict) or set(pin) != {'submission_id', 'assignment_id', 'task_id', 'revision',
                                                     'submitter_id', 'result_hash', 'config_hash'}
                or any(type(pin[k]) is not int or not 0 < pin[k] <= 2**53 - 1
                       for k in ('submission_id', 'assignment_id', 'task_id', 'revision'))
                or pin['task_id'] != task_id
                or (pin['submitter_id'] is not None and (type(pin['submitter_id']) is not int or pin['submitter_id'] <= 0))
                or any(not isinstance(pin[k], str) or len(pin[k]) != 64 for k in ('result_hash', 'config_hash'))):
            reject('INVALID_RECEIPT')
        ids.append(pin['submission_id'])
    if ids != sorted(set(ids)):
        reject('INVALID_RECEIPT')
    return ids


def manual_base(manifest, context, task_id):
    _, actor, project, role = context
    ids = manual_manifest(manifest, project.pk, task_id)
    authorized = set(service.sources(project, task_id, actor, role).filter(pk__in=ids).values_list('pk', flat=True))
    if len(authorized) != len(ids):
        from tasks.models import Submission
        if Submission.objects.filter(pk__in=set(ids) - authorized).exists():
            raise NotFound()
        report = service.unavailable('source_unavailable')
    else:
        rows = list(batches.load_sources(ids, strict=True).values())
        if [service.pin(row) for row in rows] != manifest['inputs']:
            reject('INPUT_CHANGED', 409)
        report = service.calculate(rows, project.pk)
    return {
        'kind': 'manual', 'project_id': project.pk, 'task_id': task_id,
        'rule': service.RULE, 'calculated_at': manifest['calculated_at'], 'origin_hash': digest(manifest),
    }, {'manifest': manifest, 'manifest_hash': digest(manifest), **report}


def batch_base(batch, context, task_id):
    _, actor, project, role = context
    batches.checked(batch, project)
    # This targeted helper checks complete frozen cohorts and the old calculation
    # hash. It never loads answers for other tasks in the batch.
    report, _ = batches.project_item_sources(batch, actor, role, task_id)
    frozen_item = next(v for v in batch.manifest['tasks'] if v['task_id'] == task_id)
    origin = {'kind': 'batch', 'project_id': project.pk, 'task_id': task_id, 'batch_id': batch.pk,
              'rule': service.RULE, 'selection': batches.SELECTION,
              'calculated_at': batch.manifest['calculated_at'], 'origin_hash': digest(frozen_item)}
    # Keep the original displayed reference: Manager batch hash or Reviewer
    # visible projection hash. The receipt itself pins only this task's origin.
    metadata = batches.metadata(batch, actor, role)
    reference = metadata.get('manifest_hash', metadata.get('projection_hash'))
    return origin, {'manifest': {'rule': service.RULE, 'calculated_at': batch.manifest['calculated_at'],
                                'inputs': report['inputs']}, 'manifest_hash': reference, **report}


def seal(origin, base, result, explained_at):
    payload = {'schema': SCHEMA, 'rule': RULE, 'origin': origin,
               'inputs': base['manifest']['inputs'], 'explained_at': explained_at, 'result_hash': digest(result)}
    token = signing.dumps(payload, salt=SCHEMA, compress=False)
    if len(token.encode()) > MAX_TOKEN:
        reject('TOO_LARGE', 422)
    return payload, token
