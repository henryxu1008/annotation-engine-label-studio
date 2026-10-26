"""Task-macro read projection. Never extend an existing frozen calculator DTO."""

import itertools
from collections import Counter
from fractions import Fraction

from django.conf import settings
from django.core import signing
from django.utils.crypto import constant_time_compare, salted_hmac
from rest_framework.exceptions import APIException
from review_batches.service import digest, fail

from . import batches, partial, service

SCHEMA = 'classification-quality-overview/v1'
RULE = 'classification-batch-quality-task-macro/v1'
RULES = {'aggregation': RULE, 'exact': service.RULE, 'partial': partial.RULE, 'selection': batches.SELECTION}
MAX_BYTES = 1024 * 1024
MAX_TOKEN = 16384


def reject(reason, status=409):
    fail('CLASSIFICATION_OVERVIEW_' + reason, status)


def bounded(value):
    # Reuse the streaming encoder, without allocating a second encoded result.
    try:
        return partial.bounded_size(value, MAX_BYTES)
    except APIException as exc:
        if exc.status_code == 422:
            reject('TOO_LARGE', 422)
        raise
    except (TypeError, ValueError):
        reject('INVALID_RESULT')


def origin(batch, context, *, expected_viewer=None):
    principal, actor, project, role = context
    batches.checked(batch, project)
    visible = batches.visible_items(batch, actor, role)
    metadata = batches.metadata(batch, actor, role)
    kind = 'manifest_hash' if role == 'manager' else 'projection_hash'
    identity = digest({'principal': principal.principal_id, 'source': principal.source, 'actor': actor.pk,
                       'project': project.pk, 'organization': project.organization_id, 'role': role})
    viewers = [salted_hmac(SCHEMA + '/viewer', identity, secret=key, algorithm='sha256').hexdigest()
               for key in (settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS)]
    viewer = (expected_viewer if isinstance(expected_viewer, str)
              and any(constant_time_compare(expected_viewer, candidate) for candidate in viewers) else viewers[0])
    return {
        'scope': {'kind': 'batch', 'project_id': project.pk, 'batch_id': batch.pk,
                  'visibility': metadata['visibility'], 'reference_kind': kind, 'reference_hash': metadata[kind]},
        'source_set_hash': digest(visible),
        'viewer': viewer,
    }


def reason_counts(reasons):
    return [{'reason': key, 'count': value} for key, value in sorted(Counter(reasons).items())]


def task_scores(report):
    """Reduce existing normalized evidence; do not construct pairwise answer JSON."""
    locations = report['locations']
    values = [{v['submission_id']: set(v['choices']) for v in loc['values']} for loc in locations]
    ids = sorted(values[0])
    exact, jaccard = {key: Fraction() for key in ids}, {key: Fraction() for key in ids}
    for left, right in itertools.combinations(ids, 2):
        equal, score = True, Fraction()
        for control in values:
            a, b = control[left], control[right]
            # The exact validator has already rejected empty sets and missing controls.
            if not a or not b:
                reject('INVALID_RESULT')
            equal = equal and a == b
            score += Fraction(len(a & b), len(a | b))
        score /= len(values)
        for key in (left, right):
            exact[key] += int(equal)
            jaccard[key] += score
    peers = len(ids) - 1
    per_source = {key: (exact[key] / peers, jaccard[key] / peers) for key in ids}
    # Averaging incidence at this one task equals averaging its unordered pairs.
    return (sum(exact.values()) / (len(ids) * peers),
            sum(jaccard.values()) / (len(ids) * peers), per_source)


def project(batch, context, *, expected_viewer=None):
    try:
        return _project(batch, context, expected_viewer=expected_viewer)
    except (KeyError, TypeError, ValueError, AttributeError, ZeroDivisionError):
        reject('MANIFEST_INVALID')


def _project(batch, context, *, expected_viewer=None):
    reference = origin(batch, context, expected_viewer=expected_viewer)
    _, actor, _, role = context
    visible = batches.visible_items(batch, actor, role)
    if len(visible) > batches.MAX_TASKS:
        reject('TOO_LARGE', 422)
    if any(len(item['cohort']) > batches.MAX_PARTICIPANTS for item in visible):
        reject('TOO_LARGE', 422)
    if sum(len(item['cohort']) for item in visible) > batches.MAX_INPUTS:
        reject('TOO_LARGE', 422)
    comparable = sum(item['result']['outcome'] != 'unavailable' for item in visible)
    counts = {'tasks': len(visible), 'comparable_tasks': comparable, 'unavailable_tasks': len(visible) - comparable,
              'unavailable_reasons': reason_counts(item['result']['reason'] for item in visible
                                                 if item['result']['outcome'] == 'unavailable')}
    reports = batches.project_items(batch, actor, role, strict=True)
    issues = [r['reason'] for r in reports if r['reason'] in ('source_unavailable', 'source_integrity_failed')]
    base = {'schema': SCHEMA, 'rules': dict(RULES), **reference, 'scope_counts': counts,
            'batch_calculated_at': batch.manifest['calculated_at'],
            'source_status': 'verified', 'current_source_issues': {'task_count': len(issues), 'reasons': reason_counts(issues)}}
    if issues:
        integrity = 'source_integrity_failed' in issues
        result = {**base, 'source_status': 'integrity_failed' if integrity else 'unavailable',
                'availability': 'unavailable', 'reason': 'source_integrity_failed' if integrity else 'source_unavailable',
                'summary': None, 'annotators': None}
        bounded(result)
        return result

    people, exact_sum, partial_sum, unanimous = {}, Fraction(), Fraction(), 0
    used = 0
    for item, report in zip(visible, reports):
        cohort = item['cohort']
        participants = sorted({entry['assignee_id'] for entry in cohort})
        for person in participants:
            if type(person) is not int or person <= 0:
                reject('MANIFEST_INVALID')
            row = people.setdefault(person, {'actor_id': person, 'tasks': 0, 'comparable_tasks': 0,
                                            'disagreement_tasks': 0, 'peer_pairs': 0, 'matching_peer_pairs': 0,
                                            'task_ids': [], '_exact': Fraction(), '_partial': Fraction(), '_reasons': []})
            row['tasks'] += 1
            row['task_ids'].append(item['task_id'])
        if report['status'] != 'comparable':
            for person in participants:
                people[person]['_reasons'].append(report['reason'])
            continue
        task_exact, task_partial, per_source = task_scores(report)
        # Pin the reduction to the old exact calculator's integer semantics.
        if task_exact != Fraction(report['matching_pairs'], report['total_pairs']):
            reject('INVALID_RESULT')
        exact_sum += task_exact
        partial_sum += task_partial
        unanimous += task_exact == 1
        by_source = {entry['source']['submission_id']: entry for entry in cohort}
        for source_id, (exact, score) in per_source.items():
            entry = by_source[source_id]
            if entry['source']['submitter_id'] != entry['assignee_id']:
                reject('MANIFEST_INVALID')
            row = people[entry['assignee_id']]
            row['comparable_tasks'] += 1
            row['_exact'] += exact
            row['_partial'] += score
            row['disagreement_tasks'] += exact != 1
            peers = len(participants) - 1
            row['peer_pairs'] += peers
            row['matching_peer_pairs'] += int(exact * peers)
    rows = []
    for person in sorted(people):
        row = people[person]
        n = row['comparable_tasks']
        result = {key: value for key, value in row.items() if not key.startswith('_')}
        result.update(unavailable_tasks=row['tasks'] - n, unavailable_reasons=reason_counts(row['_reasons']),
                      coverage=partial.fraction(Fraction(n, row['tasks'])),
                      exact_agreement=partial.fraction(row['_exact'] / n) if n else None,
                      partial_match=partial.fraction(row['_partial'] / n) if n else None)
        used += bounded(result)
        if used > MAX_BYTES:
            reject('TOO_LARGE', 422)
        rows.append(result)
    result = {**base, 'availability': 'available' if comparable else 'unavailable',
              'reason': None if comparable else 'no_comparable_tasks',
              'summary': {'coverage': partial.fraction(Fraction(comparable, len(visible))),
                          'unanimous_tasks': unanimous, 'disagreement_tasks': comparable - unanimous,
                          'exact_agreement': partial.fraction(exact_sum / comparable) if comparable else None,
                          'partial_match': partial.fraction(partial_sum / comparable) if comparable else None},
              'annotators': rows}
    bounded(result)
    return result


def decode(token):
    if not isinstance(token, str) or not token or len(token.encode()) > MAX_TOKEN or token.startswith('.'):
        reject('INVALID_RECEIPT', 400)
    try:
        value = signing.loads(token, salt=SCHEMA)
    except (signing.BadSignature, ValueError, TypeError, UnicodeError):
        reject('INVALID_RECEIPT', 400)
    if (not isinstance(value, dict) or set(value) != {'schema', 'rules', 'origin', 'summarized_at', 'result_hash'}
            or value['schema'] != SCHEMA or value['rules'] != RULES or not isinstance(value['origin'], dict)
            or not isinstance(value['summarized_at'], str) or not isinstance(value['result_hash'], str)
            or len(value['result_hash']) != 64):
        reject('INVALID_RECEIPT', 400)
    return value


def seal(data, time):
    payload = {'schema': SCHEMA, 'rules': dict(RULES),
               'origin': {key: data[key] for key in ('scope', 'source_set_hash', 'viewer')},
               'summarized_at': time, 'result_hash': digest(data)}
    token = signing.dumps(payload, salt=SCHEMA, compress=False)
    if len(token.encode()) > MAX_TOKEN:
        reject('TOO_LARGE', 422)
    return payload, token
