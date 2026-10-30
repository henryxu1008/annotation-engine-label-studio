"""Durable field cohorts; validate the whole request before comparing answers."""

from django.db import connection, models
from django.db.models import Case, F, JSONField, OuterRef, Subquery, Value, When
from django.db.models.expressions import RawSQL
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.fields import CharField
from review_batches.service import valid_source
from tasks.models import Submission, Task, TaskAssignment

from . import field_api, fields
from .models import FieldBatch

SCHEMA = 'field-batches/v1'
SELECTION = 'active-assignment-latest/v1'
PREVIEW_SALT = 'field-batch-preview/v1'
MAX_TASKS, MAX_PARTICIPANTS, MAX_INPUTS = 100, 20, 2000
MAX_NORMALIZATIONS, MAX_EQUALITIES = 400, 7600
QUEUE_RULE = 'field-raw-difference-queue/v1'
MAX_MANIFEST = 1024 * 1024
MAX_MANIFEST_STORED = 4 * MAX_MANIFEST
PREVIEW_AGE = 15 * 60
# Frozen v1 envelope validation is distinct from current processing budgets.
# Metadata recovery must not re-run work or apply a later processing ceiling.
V1_TASKS, V1_PARTICIPANTS, V1_INPUTS = 100, 20, 2000
V1_MANIFEST, V1_NORMALIZATIONS, V1_EQUALITIES = 1024 * 1024, 400, 7600
V1_RULE, V1_EXPLANATION, V1_ENGINE = (
    'field-lf-ascii-trim/v1',
    'field-value-differences/v1',
    'python-string-equality/v1',
)
ZERO = {'normalized_values': 0, 'field_equalities': 0}
REASONS = {
    None,
    'insufficient_inputs',
    'missing_submission',
    'latest_revision_unusable',
    'unsupported_config',
    'cancelled_or_missing',
    'missing_or_malformed',
    'missing_field',
    'config_mismatch',
    'task_data_mismatch',
    'empty_or_missing_text',
    'no_values',
}


def fail(code, status=409):
    raise fields.Error('FIELD_BATCH_' + code, status)


def validated_text(value, maximum, *, trim=False, code='INVALID_INPUT'):
    # JSON permits escaped NUL/unpaired surrogates. Reuse DRF's validators
    # before signing, UTF-8 encoding, scoring or writing PostgreSQL text/JSONB.
    if not isinstance(value, str):
        fail(code, 400)
    try:
        return CharField(max_length=maximum, trim_whitespace=trim).run_validation(value)
    except ValidationError:
        fail(code, 400)


def positive_ids(value):
    if not isinstance(value, list) or not value or any(not field_api.positive(v) for v in value):
        fail('INVALID_INPUT', 400)
    if len(value) > MAX_TASKS:
        fail('TOO_LARGE', 422)
    if len(set(value)) != len(value):
        fail('INVALID_INPUT', 400)
    return sorted(value)


def preview_args(data, project):
    if not isinstance(data, dict) or set(data) != {'name', 'scope'}:
        fail('INVALID_INPUT', 400)
    name, scope = data['name'], data['scope']
    if not isinstance(scope, dict):
        fail('INVALID_INPUT', 400)
    name = validated_text(name, 160, trim=True)
    if scope == {'kind': 'project'}:
        ids = list(Task.objects.filter(project=project).order_by('pk').values_list('pk', flat=True)[: MAX_TASKS + 1])
    elif set(scope) == {'kind', 'task_ids'} and scope['kind'] == 'tasks':
        ids = scope['task_ids']
    else:
        fail('INVALID_INPUT', 400)
    return name, positive_ids(ids)


def unavailable(count, reason, configured_fields=None):
    return {
        'input_count': count,
        'total_pairs': count * (count - 1) // 2,
        'configured_fields': configured_fields,
        'availability': 'unavailable',
        'reason': reason,
        'pairs': None,
        'sources': None,
        'details': None,
    }


def summary(logical):
    pairs = [p for p in (logical['pairs'] or []) if p['denominator']]
    outcome = (
        'unavailable'
        if not pairs
        else 'disagreement'
        if any(p['raw_matches'] < p['denominator'] for p in pairs)
        else 'agreement'
    )
    return {
        'outcome': outcome,
        'reason': logical['reason'],
        'input_count': logical['input_count'],
        'configured_fields': logical['configured_fields'],
        'total_pairs': logical['total_pairs'],
        'comparable_pairs': len(pairs),
        'unavailable_pairs': logical['total_pairs'] - len(pairs),
        'has_value_difference': any(p['normalized_matches'] < p['denominator'] for p in pairs),
        'has_format_difference': any(
            e['kind'] == 'format_only' for p in (logical.get('details') or []) for e in p['fields']
        ),
        'cost_per_pass': logical.get('cost_per_pass', dict(ZERO)),
        'calculation_hash': fields.digest(logical),
    }


def prepare(cohort, rows, reason):
    return (
        {'logical': unavailable(len(cohort), reason)}
        if reason
        else fields.inspect([r.pk for r in rows], [r.result_snapshot for r in rows])
    )


def budget(inspected):
    total = {key: sum(fields.planned_cost(value)[key] for value in inspected.values()) for key in ZERO}
    if total['normalized_values'] > MAX_NORMALIZATIONS or total['field_equalities'] > MAX_EQUALITIES:
        fail('TOO_LARGE', 422)
    return total


def score_all(inspected):
    expected, actual = budget(inspected), dict(ZERO)
    scored = {key: fields.calculate(value, actual) for key, value in inspected.items()}
    if actual != expected:
        fail('COST_CHANGED')
    return scored, actual


def scoped_sources(project, ids):
    return Submission.objects.filter(pk__in=ids, assignment__project=project, assignment__task__project=project)


def build_plan(project, ids, *, lock=False):
    ids = positive_ids(ids)
    tasks = Task.objects.filter(pk__in=ids).only('pk', 'project_id').order_by('pk')
    if lock:
        tasks = tasks.select_for_update()
    tasks = list(tasks)
    if len(tasks) != len(ids) or any(t.project_id != project.pk for t in tasks):
        fail('SCOPE_CHANGED')
    query = TaskAssignment.objects.filter(task_id__in=ids, status__in=TaskAssignment.ACTIVE_STATUSES).order_by('pk')
    if lock:
        query = query.select_for_update()
    assignments = list(query[: MAX_INPUTS + 1])
    if len(assignments) > MAX_INPUTS:
        fail('TOO_LARGE', 422)
    grouped = {t: [] for t in ids}
    for row in assignments:
        if row.project_id != project.pk or not field_api.positive(row.assignee_id):
            fail('SOURCE_INVALID')
        grouped[row.task_id].append(row)
        if len(grouped[row.task_id]) > MAX_PARTICIPANTS:
            fail('TOO_LARGE', 422)
    newest = Submission.objects.filter(assignment_id=OuterRef('assignment_id')).order_by('-revision', '-pk')
    chosen = Submission.objects.filter(
        assignment_id__in=[a.pk for a in assignments], pk=Subquery(newest.values('pk')[:1])
    )
    selected = list(chosen.order_by('pk').values_list('pk', flat=True))
    if lock:
        list(
            Submission.objects.filter(pk__in=selected).select_for_update().order_by('pk').values_list('pk', flat=True)
        )
    sources = field_api.load(scoped_sources(project, selected))
    if {s.pk for s in sources} != set(selected):
        fail('SOURCE_CHANGED')
    by_assignment = {s.assignment_id: s for s in sources}
    items, prepared = [], {}
    for task_id in ids:
        cohort, rows, reason = [], [], None
        for assignment in grouped[task_id]:
            row = by_assignment.get(assignment.pk)
            if row is not None:
                if (
                    row.submitted_by_id != assignment.assignee_id
                    or row.assignment.assignee_id != assignment.assignee_id
                    or row.assignment.task_id != assignment.task_id
                ):
                    fail('SOURCE_INVALID')
                rows.append(row)
                if row.status == Submission.Status.SUPERSEDED:
                    reason = 'latest_revision_unusable'
            cohort.append(
                {
                    'assignment_id': assignment.pk,
                    'assignee_id': assignment.assignee_id,
                    'source': field_api.pin(row) if row else None,
                }
            )
        if len(cohort) < 2:
            reason = 'insufficient_inputs'
        elif len({e['assignee_id'] for e in cohort}) != len(cohort):
            fail('SOURCE_INVALID')
        elif any(e['source'] is None for e in cohort):
            reason = 'missing_submission'
        rows.sort(key=lambda r: r.pk)
        value = prepare(cohort, rows, reason)
        prepared[task_id] = value
        budget(prepared)
        items.append({'task_id': task_id, 'cohort': cohort, 'selection_reason': reason, 'result': None})
    scored, cost = score_all(prepared)
    for item in items:
        item['result'] = summary(scored[item['task_id']]['logical'])
    plan = {
        'selection': SELECTION,
        'rule': fields.RULE,
        'explanation': fields.EXPLANATION,
        'engine': fields.ENGINE,
        'queue_rule': QUEUE_RULE,
        'task_ids': ids,
        'tasks': items,
        'cost_per_pass': cost,
    }
    fields.bounded(plan, MAX_MANIFEST)
    return plan, cost


def query_batches(project):
    table = connection.ops.quote_name(FieldBatch._meta.db_table)
    column = table + '.' + connection.ops.quote_name('manifest')
    size = (
        f'OCTET_LENGTH(CAST({column} AS TEXT))'
        if connection.vendor == 'postgresql'
        else f'LENGTH(CAST({column} AS BLOB))'
    )
    return (
        FieldBatch.objects.filter(project=project)
        .annotate(_size=RawSQL(size, [], output_field=models.BigIntegerField()))
        .annotate(
            _frozen=Case(
                When(_size__lte=MAX_MANIFEST_STORED, then=F('manifest')), default=Value(None), output_field=JSONField()
            )
        )
        .defer('manifest')
    )


def checked(batch, project):
    if hasattr(batch, '_frozen'):
        if type(batch._size) is not int or not 0 <= batch._size <= MAX_MANIFEST_STORED:
            fail('MANIFEST_INVALID')
        batch.manifest = batch._frozen
    m = batch.manifest
    try:
        fields.bounded(m, V1_MANIFEST)
        keys = {
            'selection',
            'rule',
            'explanation',
            'engine',
            'queue_rule',
            'task_ids',
            'tasks',
            'cost_per_pass',
            'schema',
            'project_id',
            'organization_id',
            'creator_principal',
            'creator_source',
            'actor_id_frozen',
            'name',
            'scope_enumerated_at',
            'calculated_at',
        }
        valid = (
            set(m) == keys
            and fields.digest(m) == batch.manifest_hash
            and m['schema'] == SCHEMA
            and field_api.positive(m['project_id'])
            and field_api.positive(m['organization_id'])
            and m['project_id'] == project.pk
            and m['organization_id'] == project.organization_id
            and m['creator_principal'] == batch.creator_principal
            and m['actor_id_frozen'] == batch.actor_id_frozen
            and field_api.positive(m['actor_id_frozen'])
            and isinstance(m['creator_source'], str)
            and bool(m['creator_source'])
            and validated_text(m['name'], 160, trim=True) == batch.name == m['name']
            and m['selection'] == SELECTION
            and m['queue_rule'] == QUEUE_RULE
            and m['rule'] == V1_RULE
            and m['explanation'] == V1_EXPLANATION
            and m['engine'] == V1_ENGINE
            and isinstance(m['task_ids'], list)
            and 1 <= len(m['task_ids']) <= V1_TASKS
            and all(field_api.positive(i) for i in m['task_ids'])
            and sorted(set(m['task_ids'])) == m['task_ids']
            and m['task_ids'] == [i['task_id'] for i in m['tasks']]
        )
        from django.utils import timezone

        for key in ('scope_enumerated_at', 'calculated_at'):
            valid = (
                valid and isinstance(m[key], str) and not timezone.is_naive(timezone.datetime.fromisoformat(m[key]))
            )
        assignment_ids, source_ids = [], []
        total = dict(ZERO)
        for item in m['tasks']:
            cohort, result = item['cohort'], item['result']
            valid = valid and set(item) == {'task_id', 'cohort', 'selection_reason', 'result'}
            valid = valid and field_api.positive(item['task_id'])
            valid = valid and item['selection_reason'] in (
                None,
                'insufficient_inputs',
                'missing_submission',
                'latest_revision_unusable',
            )
            valid = valid and len(cohort) <= V1_PARTICIPANTS and len({e['assignee_id'] for e in cohort}) == len(cohort)
            valid = valid and [e['assignment_id'] for e in cohort] == sorted(e['assignment_id'] for e in cohort)
            if len(cohort) < 2:
                valid = valid and item['selection_reason'] == 'insufficient_inputs'
            elif any(e['source'] is None for e in cohort):
                valid = valid and item['selection_reason'] == 'missing_submission'
            else:
                valid = valid and item['selection_reason'] in (None, 'latest_revision_unusable')
            for entry in cohort:
                valid = valid and set(entry) == {'assignment_id', 'assignee_id', 'source'}
                valid = (
                    valid and field_api.positive(entry['assignment_id']) and field_api.positive(entry['assignee_id'])
                )
                assignment_ids.append(entry['assignment_id'])
                pin = entry['source']
                if pin is not None:
                    source_ids.append(pin['submission_id'])
                    valid = valid and (
                        set(pin) == set(field_api.PIN_IDS + field_api.PIN_HASHES)
                        and all(field_api.positive(pin[k]) for k in field_api.PIN_IDS)
                        and all(
                            isinstance(pin[k], str) and field_api.HEX.fullmatch(pin[k]) for k in field_api.PIN_HASHES
                        )
                        and pin['task_id'] == item['task_id']
                        and pin['assignment_id'] == entry['assignment_id']
                        and pin['submitter_id'] == pin['assignee_id'] == entry['assignee_id']
                    )
            valid = valid and set(result) == {
                'outcome',
                'reason',
                'input_count',
                'configured_fields',
                'total_pairs',
                'comparable_pairs',
                'unavailable_pairs',
                'has_value_difference',
                'has_format_difference',
                'cost_per_pass',
                'calculation_hash',
            }
            count = len(cohort)
            valid = valid and type(result['input_count']) is int and result['input_count'] == count
            valid = valid and type(result['total_pairs']) is int and result['total_pairs'] == count * (count - 1) // 2
            valid = valid and all(
                type(result[k]) is int and 0 <= result[k] <= result['total_pairs']
                for k in ('comparable_pairs', 'unavailable_pairs')
            )
            valid = valid and result['comparable_pairs'] + result['unavailable_pairs'] == result['total_pairs']
            valid = valid and result['reason'] in REASONS
            if item['selection_reason'] is not None:
                valid = valid and result['reason'] == item['selection_reason']
            else:
                valid = valid and result['reason'] not in (
                    'insufficient_inputs',
                    'missing_submission',
                    'latest_revision_unusable',
                )
            valid = valid and (
                result['configured_fields'] is None
                or type(result['configured_fields']) is int
                and 1 <= result['configured_fields'] <= 20
            )
            valid = valid and all(type(result[k]) is bool for k in ('has_value_difference', 'has_format_difference'))
            valid = valid and result['outcome'] in ('agreement', 'disagreement', 'unavailable')
            if result['outcome'] == 'unavailable':
                valid = valid and result['comparable_pairs'] == 0 and result['reason'] is not None
                valid = valid and not result['has_value_difference'] and not result['has_format_difference']
            else:
                valid = valid and result['comparable_pairs'] > 0 and result['reason'] is None
                valid = valid and (result['outcome'] == 'disagreement') == bool(
                    result['has_value_difference'] or result['has_format_difference']
                )
            cost = result['cost_per_pass']
            valid = valid and set(cost) == set(ZERO) and all(type(cost[k]) is int and cost[k] >= 0 for k in ZERO)
            if cost['normalized_values']:
                size = result['configured_fields']
                valid = valid and count >= 2 and isinstance(size, int)
                valid = valid and cost == {
                    'normalized_values': count * size,
                    'field_equalities': count * (count - 1) * size,
                }
                valid = valid and result['reason'] in (None, 'no_values')
            else:
                valid = (
                    valid and cost == ZERO and result['outcome'] == 'unavailable' and result['reason'] != 'no_values'
                )
            for key in ZERO:
                total[key] += cost[key]
            valid = (
                valid
                and isinstance(result['calculation_hash'], str)
                and field_api.HEX.fullmatch(result['calculation_hash']) is not None
            )
        valid = valid and sum(len(i['cohort']) for i in m['tasks']) <= V1_INPUTS
        valid = valid and len(assignment_ids) == len(set(assignment_ids)) and len(source_ids) == len(set(source_ids))
        valid = valid and m['cost_per_pass'] == total and all(type(m['cost_per_pass'][k]) is int for k in ZERO)
        valid = (
            valid and total['normalized_values'] <= V1_NORMALIZATIONS and total['field_equalities'] <= V1_EQUALITIES
        )
    except (TypeError, ValueError, KeyError, AttributeError, fields.Error):
        valid = False
    if not valid:
        fail('MANIFEST_INVALID')
    return batch


def visible_items(batch, actor, role):
    return [
        item
        for item in batch.manifest['tasks']
        if role == 'manager'
        or not any(
            e['assignee_id'] == actor.pk or (e['source'] or {}).get('submitter_id') == actor.pk for e in item['cohort']
        )
    ]


def visible_batches(project, actor, role):
    rows = query_batches(project)
    if role == 'reviewer':
        if connection.vendor == 'postgresql':
            expression = """EXISTS (SELECT 1 FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(manifest->'tasks') = 'array' THEN manifest->'tasks' ELSE '[]'::jsonb END) item
                WHERE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(item->'cohort') = 'array' THEN item->'cohort' ELSE '[]'::jsonb END) member
                WHERE member->>'assignee_id' = %s OR member->'source'->>'submitter_id' = %s))"""
            parameters = (str(actor.pk), str(actor.pk))
        else:
            expression = """EXISTS (SELECT 1 FROM json_each(
                CASE WHEN json_type(manifest, '$.tasks') = 'array' THEN json_extract(manifest, '$.tasks') ELSE '[]' END) item
                WHERE NOT EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(item.value) THEN
                CASE WHEN json_type(item.value, '$.cohort') = 'array' THEN json_extract(item.value, '$.cohort') ELSE '[]' END
                ELSE '[]' END) member
                WHERE CASE WHEN json_valid(member.value) THEN json_extract(member.value, '$.assignee_id') END = %s
                OR CASE WHEN json_valid(member.value) THEN json_extract(member.value, '$.source.submitter_id') END = %s))"""
            parameters = (actor.pk, actor.pk)
        rows = rows.annotate(_visible=RawSQL(expression, parameters, output_field=models.BooleanField())).filter(
            _visible=True
        )
    return rows.order_by('-pk')


def metadata(batch, context):
    _, actor, project, role = context
    items = visible_items(batch, actor, role)
    if not items:
        raise NotFound()
    reference = (
        batch.manifest_hash
        if role == 'manager'
        else fields.digest({'schema': SCHEMA, 'batch': batch.pk, 'rule': batch.manifest['rule'], 'tasks': items})
    )
    return {
        'id': batch.pk,
        'project_id': project.pk,
        'name': batch.name,
        'schema': SCHEMA,
        'rule': batch.manifest['rule'],
        'explanation': batch.manifest['explanation'],
        'engine': batch.manifest['engine'],
        'selection': SELECTION,
        'queue_rule': QUEUE_RULE,
        'created_at': batch.created_at.isoformat(),
        'calculated_at': batch.manifest['calculated_at'],
        'scope_enumerated_at': batch.manifest['scope_enumerated_at'],
        'visible_task_count': len(items),
        'visibility': 'project' if role == 'manager' else 'visible_subset',
        'viewer': field_api.viewer(context),
        'plan_cost_per_pass': {key: sum(i['result']['cost_per_pass'][key] for i in items) for key in ZERO},
        'manifest_hash' if role == 'manager' else 'projection_hash': reference,
    }


def plan_hash(batch):
    return fields.digest(
        {
            k: batch.manifest[k]
            for k in ('selection', 'rule', 'explanation', 'engine', 'queue_rule', 'task_ids', 'tasks', 'cost_per_pass')
        }
    )


def inspect_items(batch, context):
    items = visible_items(batch, context[1], context[3])
    task_ids = [i['task_id'] for i in items]
    tasks = dict(Task.objects.filter(pk__in=task_ids).values_list('pk', 'project_id'))
    cohort = [e for i in items for e in i['cohort']]
    assignments = {a.pk: a for a in TaskAssignment.objects.filter(pk__in=[e['assignment_id'] for e in cohort])}
    source_ids = [e['source']['submission_id'] for e in cohort if e['source']]
    sources = {r.pk: r for r in field_api.load(scoped_sources(context[2], source_ids), validate_source=False)}
    inspected, missing = {}, set()
    for item in items:
        current_rows, reason = [], None
        if tasks.get(item['task_id']) != batch.project_id:
            reason = 'source_unavailable'
        for entry in item['cohort']:
            assignment = assignments.get(entry['assignment_id'])
            if (
                assignment is None
                or assignment.project_id != batch.project_id
                or assignment.task_id != item['task_id']
                or assignment.assignee_id != entry['assignee_id']
            ):
                reason = 'source_unavailable'
            pin = entry['source']
            if pin:
                row = sources.get(pin['submission_id'])
                try:
                    valid = row is not None and valid_source(row, batch.project_id) and field_api.pin(row) == pin
                except (KeyError, TypeError, ValueError, AttributeError):
                    valid = False
                if not valid:
                    reason = 'source_unavailable'
                else:
                    current_rows.append(row)
        current_rows.sort(key=lambda r: r.pk)
        if reason:
            missing.add(item['task_id'])
            inspected[item['task_id']] = {
                'logical': unavailable(len(item['cohort']), reason, item['result']['configured_fields'])
            }
        else:
            inspected[item['task_id']] = prepare(item['cohort'], current_rows, item['selection_reason'])
    return items, inspected, missing


def project_scored(items, scored, missing):
    projected = []
    for item in items:
        task_id, frozen = item['task_id'], item['result']
        lost = task_id in missing
        value = scored[task_id]
        if not lost and summary(value['logical']) != frozen:
            fail('RESULT_CHANGED')
        projected.append(
            {
                'task_id': task_id,
                'outcome': 'unavailable' if lost else frozen['outcome'],
                'reason': 'source_unavailable' if lost else frozen['reason'],
                'original_outcome': frozen['outcome'],
                'input_count': frozen['input_count'],
                'total_pairs': frozen['total_pairs'],
                'configured_fields': frozen['configured_fields'],
                'frozen_comparable_pairs': frozen['comparable_pairs'],
                'comparable_pairs': None if lost else frozen['comparable_pairs'],
                'has_value_difference': None if lost else frozen['has_value_difference'],
                'has_format_difference': None if lost else frozen['has_format_difference'],
                'calculation_hash': None if lost else frozen['calculation_hash'],
                'source_status': 'unavailable' if lost else 'verified',
                'inputs': []
                if lost
                else sorted([e['source'] for e in item['cohort'] if e['source']], key=lambda x: x['submission_id']),
                '_prepared': value,
                '_frozen': item,
            }
        )
    return projected


def project_items(batch, context):
    items, inspected, missing = inspect_items(batch, context)
    scored, cost = score_all(inspected)
    return project_scored(items, scored, missing), cost


def public_row(row):
    return {k: v for k, v in row.items() if k not in ('_prepared', '_frozen', 'inputs')}


def counts(items):
    return {state: sum(i['outcome'] == state for i in items) for state in ('agreement', 'disagreement', 'unavailable')}


def item_evidence(batch, context, row):
    if row['source_status'] != 'verified':
        return None
    meta = metadata(batch, context)
    return {
        'schema': 'field-batch-item-evidence/v1',
        'project_id': batch.project_id,
        'batch_id': batch.pk,
        'task_id': row['task_id'],
        'viewer': meta['viewer'],
        'reference': meta.get('manifest_hash', meta.get('projection_hash')),
        'rule': V1_RULE,
        'explanation': V1_EXPLANATION,
        'engine': V1_ENGINE,
        'queue_rule': QUEUE_RULE,
        'calculated_at': meta['calculated_at'],
        'cohort': row['_frozen']['cohort'],
        'result': row['_frozen']['result'],
    }
