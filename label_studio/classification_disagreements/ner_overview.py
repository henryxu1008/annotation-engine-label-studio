"""Read-only task-macro reduction of verified, frozen NER pair evidence."""
from collections import Counter
from fractions import Fraction

from django.core import signing
from django.utils import timezone

from . import ner, ner_api
from . import ner_batches as batches

SCHEMA = 'ner-quality-overview/v1'
RULE = 'ner-batch-quality-task-macro/v1'
RULES = {'aggregation': RULE, 'strict': ner.RULE, 'explanation': ner.EXPLANATION,
         'engine': ner.ENGINE, 'selection': batches.SELECTION}
MAX_BYTES = 1024 * 1024
MAX_TOKEN = 16384


def reject(reason, status=409):
    raise ner.Error('NER_OVERVIEW_' + reason, status)


def bounded(value):
    return ner.bounded(value, MAX_BYTES)


def ratio(numerator, denominator):
    return ner.fraction(numerator, denominator) if denominator else None


def reasons(counter):
    return [{'reason': key, 'count': n} for key, n in sorted(counter.items()) if n]


def origin(batch, context, *, expected_viewer=None):
    _, actor, project, role = context
    batches.checked(batch, project)
    visible = batches.visible_items(batch, actor, role)
    meta = batches.metadata(batch, context)
    kind = 'manifest_hash' if role == 'manager' else 'projection_hash'
    return {'scope': {'kind': 'batch', 'organization_id': project.organization_id,
                     'project_id': project.pk, 'batch_id': batch.pk, 'visibility': meta['visibility'],
                     'reference_kind': kind, 'reference_hash': meta[kind]},
            'source_set_hash': ner.digest(visible), 'viewer': ner_api.viewer(context, expected_viewer)}


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
        pairs[result['reason'] if not q else 'no_entities'] += p - q
    return {'tasks': len(visible), 'comparable_tasks': comparable,
            'unavailable_tasks': len(visible) - comparable, 'unavailable_reasons': reasons(tasks),
            'total_pairs': total_pairs, 'comparable_pairs': comparable_pairs,
            'unavailable_pairs': total_pairs - comparable_pairs, 'unavailable_pair_reasons': reasons(pairs)}


def reduce(visible, reports):
    """Pair macro within tasks, task macro across tasks; never average people."""
    people, total, used = {}, Fraction(), 0
    for item, report in zip(visible, reports):
        cohort, logical = item['cohort'], report['_prepared']['logical']
        peer_total = len(cohort) - 1
        by_source = {e['source']['submission_id']: e['assignee_id'] for e in cohort if e['source']}
        actors = [e['assignee_id'] for e in cohort]
        sums, counts, empty = {a: Fraction() for a in actors}, Counter(), {a: Counter() for a in actors}
        task_sum, task_count = Fraction(), 0
        for pair in logical['pairs'] or []:
            left, right = by_source[pair['left']], by_source[pair['right']]
            if pair['f1'] is None:
                for actor in (left, right):
                    empty[actor][pair['reason']] += 1
                continue
            score = Fraction(int(pair['f1']['numerator']), int(pair['f1']['denominator']))
            if not 0 <= score <= 1:
                reject('INVALID_RESULT')
            task_sum += score
            task_count += 1
            for actor in (left, right):
                sums[actor] += score
                counts[actor] += 1
        if task_count != item['result']['comparable_pairs']:
            reject('INVALID_RESULT')
        if task_count:
            total += task_sum / task_count
        for actor in actors:
            row = people.setdefault(actor, {'actor_id': actor, 'tasks': 0, 'comparable_tasks': 0,
                'peer_pairs': 0, 'comparable_peer_pairs': 0, 'task_ids': [],
                '_sum': Fraction(), '_tasks': Counter(), '_pairs': Counter()})
            row['tasks'] += 1
            row['task_ids'].append(item['task_id'])
            row['peer_pairs'] += peer_total
            row['comparable_peer_pairs'] += counts[actor]
            if counts[actor]:
                row['comparable_tasks'] += 1
                row['_sum'] += sums[actor] / counts[actor]
            else:
                row['_tasks'][logical['reason']] += 1
            if logical['pairs'] is None:
                row['_pairs'][logical['reason']] += peer_total
            else:
                row['_pairs'].update(empty[actor])
    rows = []
    for actor in sorted(people):
        row = people[actor]
        n = row['comparable_tasks']
        result = {key: value for key, value in row.items() if not key.startswith('_')}
        result.update(unavailable_tasks=row['tasks'] - n,
                      unavailable_peer_pairs=row['peer_pairs'] - row['comparable_peer_pairs'],
                      unavailable_reasons=reasons(row['_tasks']), unavailable_peer_pair_reasons=reasons(row['_pairs']),
                      task_coverage=ratio(n, row['tasks']),
                      pair_coverage=ratio(row['comparable_peer_pairs'], row['peer_pairs']),
                      strict_f1=ratio(row['_sum'], n))
        used += bounded(result)
        if used > MAX_BYTES:
            reject('TOO_LARGE', 422)
        rows.append(result)
    return total, rows


def project(batch, context, *, expected_viewer=None):
    try:
        reference = origin(batch, context, expected_viewer=expected_viewer)
        visible = batches.visible_items(batch, context[1], context[3])
        if (len(visible) > batches.MAX_TASKS or any(len(i['cohort']) > batches.MAX_PARTICIPANTS for i in visible)
                or sum(len(i['cohort']) for i in visible) > batches.MAX_INPUTS):
            reject('TOO_LARGE', 422)
        counts = scope_counts(visible)
        if 2 * counts['comparable_pairs'] > batches.MAX_CALLS:
            reject('TOO_LARGE', 422)
        reports, prepared = batches.project_items(batch, context)
        if [r['task_id'] for r in reports] != [i['task_id'] for i in visible]:
            reject('INVALID_RESULT')
        issues = sum(r['source_status'] != 'verified' for r in reports)
        base = {'schema': SCHEMA, 'rules': dict(RULES), **reference, 'scope_counts': counts,
                'batch_calculated_at': batch.manifest['calculated_at'],
                'source_status': 'unavailable' if issues else 'verified',
                'current_source_issues': {'task_count': issues,
                    'reasons': [{'reason': 'source_unavailable', 'count': issues}] if issues else []}}
        if issues:
            data = {**base, 'availability': 'unavailable', 'reason': 'source_unavailable',
                    'summary': None, 'annotators': None}
        else:
            score, rows = reduce(visible, reports)
            n = counts['comparable_tasks']
            data = {**base, 'availability': 'available' if n else 'unavailable',
                    'reason': None if n else 'no_comparable_tasks',
                    'summary': {'task_coverage': ratio(n, counts['tasks']),
                        'pair_coverage': ratio(counts['comparable_pairs'], counts['total_pairs']),
                        'strict_f1': ratio(score, n),
                        'agreement_tasks': sum(i['result']['outcome'] == 'agreement' for i in visible),
                        'disagreement_tasks': sum(i['result']['outcome'] == 'disagreement' for i in visible)},
                    'annotators': rows}
        bounded(data)
        return data, prepared
    except (KeyError, TypeError, ValueError, AttributeError, ZeroDivisionError):
        reject('MANIFEST_INVALID')


def decode(token):
    if not isinstance(token, str) or not token or token.startswith('.'):
        reject('INVALID_RECEIPT', 400)
    try:
        if len(token.encode()) > MAX_TOKEN:
            reject('INVALID_RECEIPT', 400)
        value = signing.loads(token, salt=SCHEMA)
        if (not isinstance(value, dict) or set(value) != {'schema', 'rules', 'origin', 'summarized_at', 'result_hash'}
                or value['schema'] != SCHEMA or value['rules'] != RULES or not isinstance(value['origin'], dict)
                or not isinstance(value['summarized_at'], str) or not isinstance(value['result_hash'], str)
                or not ner_api.HEX.fullmatch(value['result_hash'])):
            reject('INVALID_RECEIPT', 400)
        when = timezone.datetime.fromisoformat(value['summarized_at'])
        if timezone.is_naive(when):
            reject('INVALID_RECEIPT', 400)
        return value
    except (signing.BadSignature, ValueError, TypeError, UnicodeError):
        reject('INVALID_RECEIPT', 400)


def seal(data, time):
    payload = {'schema': SCHEMA, 'rules': dict(RULES),
               'origin': {key: data[key] for key in ('scope', 'source_set_hash', 'viewer')},
               'summarized_at': time, 'result_hash': ner.digest(data)}
    token = signing.dumps(payload, salt=SCHEMA, compress=False)
    if len(token.encode()) > MAX_TOKEN:
        reject('TOO_LARGE', 422)
    return payload, token
