"""Read-only task-macro reduction of verified frozen text-field evidence."""
from collections import Counter
from fractions import Fraction

from django.core import signing
from django.utils import timezone

from . import field_api, fields
from . import field_batches as batches

SCHEMA = 'field-quality-overview/v1'
RULE = 'field-batch-quality-task-macro/v1'
RULES = {
    'aggregation': RULE,
    'normalization': fields.RULE,
    'explanation': fields.EXPLANATION,
    'engine': fields.ENGINE,
    'selection': batches.SELECTION,
    'queue': batches.QUEUE_RULE,
}
MAX_BYTES = 1024 * 1024
MAX_TOKEN = 16384
MAX_PAIR_VISITS = 3800
MAX_PEER_UPDATES = 7600


def reject(reason, status=409):
    raise fields.Error('FIELD_OVERVIEW_' + reason, status)


def bounded(value):
    return fields.bounded(value, MAX_BYTES)


def ratio(numerator, denominator):
    return fields.fraction(numerator, denominator) if denominator else None


def reasons(counter):
    return [{'reason': key, 'count': n} for key, n in sorted(counter.items()) if n]


def origin(batch, context, *, expected_viewer=None):
    _, actor, project, role = context
    batches.checked(batch, project)
    visible = batches.visible_items(batch, actor, role)
    meta = batches.metadata(batch, context)
    kind = 'manifest_hash' if role == 'manager' else 'projection_hash'
    return {
        'scope': {
            'kind': 'batch',
            'organization_id': project.organization_id,
            'project_id': project.pk,
            'batch_id': batch.pk,
            'visibility': meta['visibility'],
            'reference_kind': kind,
            'reference_hash': meta[kind],
        },
        'source_set_hash': fields.digest(visible),
        'viewer': field_api.viewer(context, expected_viewer),
    }


def scope_counts(visible):
    tasks, pairs = Counter(), Counter()
    comparable = total_pairs = comparable_pairs = 0
    for item in visible:
        result = item['result']
        p, q = result['total_pairs'], result['comparable_pairs']
        comparable += bool(q)
        total_pairs += p
        comparable_pairs += q
        if not q:
            tasks[result['reason']] += 1
        pairs[result['reason'] if not q else 'no_values'] += p - q
    return {
        'tasks': len(visible),
        'comparable_tasks': comparable,
        'unavailable_tasks': len(visible) - comparable,
        'unavailable_reasons': reasons(tasks),
        'total_pairs': total_pairs,
        'comparable_pairs': comparable_pairs,
        'unavailable_pairs': total_pairs - comparable_pairs,
        'unavailable_pair_reasons': reasons(pairs),
    }


def reduce(visible, reports):
    """Average defined peers at each task, then tasks; never average people."""
    people, total_raw, total_normalized, used = {}, Fraction(), Fraction(), 0
    work = {'aggregation_pair_visits': 0, 'annotator_pair_updates': 0}
    for item, report in zip(visible, reports):
        cohort, logical = item['cohort'], report['_prepared']['logical']
        by_source = {e['source']['submission_id']: e['assignee_id'] for e in cohort if e['source']}
        actors = [e['assignee_id'] for e in cohort]
        raw, normalized = {a: Fraction() for a in actors}, {a: Fraction() for a in actors}
        counts, empty = Counter(), {a: Counter() for a in actors}
        task_raw, task_normalized, task_count = Fraction(), Fraction(), 0
        for pair in logical['pairs'] or []:
            work['aggregation_pair_visits'] += 1
            if work['aggregation_pair_visits'] > MAX_PAIR_VISITS:
                reject('TOO_LARGE', 422)
            left, right = by_source[pair['left']], by_source[pair['right']]
            if not pair['denominator']:
                for actor in (left, right):
                    empty[actor]['no_values'] += 1
                continue
            d, r, n = pair['denominator'], pair['raw_matches'], pair['normalized_matches']
            if not 0 <= r <= n <= d:
                reject('INVALID_RESULT')
            r, n = Fraction(r, d), Fraction(n, d)
            task_raw += r
            task_normalized += n
            task_count += 1
            for actor in (left, right):
                raw[actor] += r
                normalized[actor] += n
                counts[actor] += 1
                work['annotator_pair_updates'] += 1
            if work['annotator_pair_updates'] > MAX_PEER_UPDATES:
                reject('TOO_LARGE', 422)
        if task_count != item['result']['comparable_pairs']:
            reject('INVALID_RESULT')
        if task_count:
            total_raw += task_raw / task_count
            total_normalized += task_normalized / task_count
        for actor in actors:
            row = people.setdefault(
                actor,
                {
                    'actor_id': actor,
                    'tasks': 0,
                    'comparable_tasks': 0,
                    'peer_pairs': 0,
                    'comparable_peer_pairs': 0,
                    'task_ids': [],
                    '_raw': Fraction(),
                    '_normalized': Fraction(),
                    '_tasks': Counter(),
                    '_pairs': Counter(),
                },
            )
            row['tasks'] += 1
            row['task_ids'].append(item['task_id'])
            row['peer_pairs'] += len(cohort) - 1
            row['comparable_peer_pairs'] += counts[actor]
            if counts[actor]:
                row['comparable_tasks'] += 1
                row['_raw'] += raw[actor] / counts[actor]
                row['_normalized'] += normalized[actor] / counts[actor]
            else:
                row['_tasks'][logical['reason']] += 1
            if logical['pairs'] is None:
                row['_pairs'][logical['reason']] += len(cohort) - 1
            else:
                row['_pairs'].update(empty[actor])
    rows = []
    for actor in sorted(people):
        row, n = people[actor], people[actor]['comparable_tasks']
        result = {key: value for key, value in row.items() if not key.startswith('_')}
        result.update(
            unavailable_tasks=row['tasks'] - n,
            unavailable_peer_pairs=row['peer_pairs'] - row['comparable_peer_pairs'],
            unavailable_reasons=reasons(row['_tasks']),
            unavailable_peer_pair_reasons=reasons(row['_pairs']),
            task_coverage=ratio(n, row['tasks']),
            pair_coverage=ratio(row['comparable_peer_pairs'], row['peer_pairs']),
            raw_agreement=ratio(row['_raw'], n),
            normalized_agreement=ratio(row['_normalized'], n),
        )
        used += bounded(result)
        if used > MAX_BYTES:
            reject('TOO_LARGE', 422)
        rows.append(result)
    return total_raw, total_normalized, rows, work


def recalculate(prepared):
    """Second counter pass uses bounded original inputs, before final source replay."""
    visible, inspected, missing = prepared['items'], prepared['inspected'], prepared['missing']
    scored, cost = batches.score_all(inspected)
    reports = batches.project_scored(visible, scored, missing)
    counts = scope_counts(visible)
    issues = sum(r['source_status'] != 'verified' for r in reports)
    base = {
        'schema': SCHEMA,
        'rules': dict(RULES),
        **prepared['reference'],
        'scope_counts': counts,
        'batch_calculated_at': prepared['calculated_at'],
        'source_status': 'unavailable' if issues else 'verified',
        'current_source_issues': {
            'task_count': issues,
            'reasons': [{'reason': 'source_unavailable', 'count': issues}] if issues else [],
        },
    }
    work = {'aggregation_pair_visits': 0, 'annotator_pair_updates': 0}
    if issues:
        data = {
            **base,
            'availability': 'unavailable',
            'reason': 'source_unavailable',
            'summary': None,
            'annotators': None,
        }
    else:
        raw, normalized, rows, work = reduce(visible, reports)
        n = counts['comparable_tasks']
        data = {
            **base,
            'availability': 'available' if n else 'unavailable',
            'reason': None if n else 'no_comparable_tasks',
            'summary': {
                'task_coverage': ratio(n, counts['tasks']),
                'pair_coverage': ratio(counts['comparable_pairs'], counts['total_pairs']),
                'raw_agreement': ratio(raw, n),
                'normalized_agreement': ratio(normalized, n),
                'agreement_tasks': sum(i['result']['outcome'] == 'agreement' for i in visible),
                'disagreement_tasks': sum(i['result']['outcome'] == 'disagreement' for i in visible),
            },
            'annotators': rows,
        }
    actual = {**cost, **work}
    data['processing_cost'] = {
        'validation_passes': 2,
        'per_pass': actual,
        'total': {key: 2 * value for key, value in actual.items()},
        'scorer_calls': 0,
    }
    bounded(data)
    return data


def project(batch, context, *, expected_viewer=None):
    try:
        reference = origin(batch, context, expected_viewer=expected_viewer)
        visible = batches.visible_items(batch, context[1], context[3])
        if (
            len(visible) > batches.MAX_TASKS
            or any(len(i['cohort']) > batches.MAX_PARTICIPANTS for i in visible)
            or sum(len(i['cohort']) for i in visible) > batches.MAX_INPUTS
        ):
            reject('TOO_LARGE', 422)
        items, inspected, missing = batches.inspect_items(batch, context)
        if items != visible:
            reject('READ_CHANGED')
        prepared = {
            'reference': reference,
            'items': items,
            'inspected': inspected,
            'missing': missing,
            'calculated_at': batch.manifest['calculated_at'],
        }
        return recalculate(prepared), prepared
    except (KeyError, TypeError, ValueError, AttributeError, ZeroDivisionError):
        reject('MANIFEST_INVALID')


def verify_sources(batch, context, prepared):
    """Hydrate once more AFTER the final counter; do not score a third time."""
    if origin(batch, context, expected_viewer=prepared['reference']['viewer']) != prepared['reference']:
        reject('CONTEXT_CHANGED')
    items, inspected, missing = batches.inspect_items(batch, context)
    batches.budget(inspected)
    if (
        items != prepared['items']
        or inspected != prepared['inspected']
        or missing != prepared['missing']
        or batch.manifest['calculated_at'] != prepared['calculated_at']
    ):
        reject('READ_CHANGED')


def decode(token):
    if not isinstance(token, str) or not token or token.startswith('.') or len(token.encode()) > MAX_TOKEN:
        reject('INVALID_RECEIPT', 400)
    try:
        value = signing.loads(token, salt=SCHEMA)
        if (
            not isinstance(value, dict)
            or set(value) != {'schema', 'rules', 'origin', 'summarized_at', 'result_hash'}
            or value['schema'] != SCHEMA
            or value['rules'] != RULES
            or not isinstance(value['origin'], dict)
            or not isinstance(value['summarized_at'], str)
            or not isinstance(value['result_hash'], str)
            or not field_api.HEX.fullmatch(value['result_hash'])
        ):
            reject('INVALID_RECEIPT', 400)
        when = timezone.datetime.fromisoformat(value['summarized_at'])
        if timezone.is_naive(when):
            reject('INVALID_RECEIPT', 400)
        origin_value = value['origin']
        scope = origin_value.get('scope')
        if (
            set(origin_value) != {'scope', 'source_set_hash', 'viewer'}
            or not isinstance(scope, dict)
            or set(scope)
            != {'kind', 'organization_id', 'project_id', 'batch_id', 'visibility', 'reference_kind', 'reference_hash'}
            or scope['kind'] != 'batch'
            or any(not field_api.positive(scope[k]) for k in ('organization_id', 'project_id', 'batch_id'))
            or scope['visibility'] not in ('project', 'visible_subset')
            or scope['reference_kind'] != ('manifest_hash' if scope['visibility'] == 'project' else 'projection_hash')
            or any(
                not isinstance(v, str) or not field_api.HEX.fullmatch(v)
                for v in (scope['reference_hash'], origin_value['source_set_hash'], origin_value['viewer'])
            )
        ):
            reject('INVALID_RECEIPT', 400)
        return value
    except (signing.BadSignature, ValueError, TypeError, UnicodeError):
        reject('INVALID_RECEIPT', 400)


def seal(data, time):
    payload = {
        'schema': SCHEMA,
        'rules': dict(RULES),
        'origin': {key: data[key] for key in ('scope', 'source_set_hash', 'viewer')},
        'summarized_at': time,
        'result_hash': fields.digest(data),
    }
    token = signing.dumps(payload, salt=SCHEMA, compress=False)
    if len(token.encode()) > MAX_TOKEN:
        reject('TOO_LARGE', 422)
    return payload, token
