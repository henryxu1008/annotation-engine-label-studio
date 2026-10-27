"""Durable NER cohorts with bounded whole-batch work and immutable provenance."""
import itertools

from django.db import connection, models
from django.db.models import Case, F, JSONField, OuterRef, Subquery, Value, When
from django.db.models.expressions import RawSQL
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.fields import CharField
from review_batches.service import valid_source
from tasks.models import Submission, Task, TaskAssignment

from . import ner, ner_api
from .models import NerBatch

SCHEMA = 'ner-batches/v1'
SELECTION = 'active-assignment-latest/v1'
PREVIEW_SALT = 'ner-batch-preview/v1'
MAX_TASKS, MAX_PARTICIPANTS, MAX_INPUTS = 100, 20, 2000
MAX_CALLS = 380
MAX_MANIFEST = 1024 * 1024
MAX_MANIFEST_STORED = 4 * MAX_MANIFEST
PREVIEW_AGE = 15 * 60
# Frozen v1 envelope validation is distinct from current processing budgets.
# Metadata recovery must not re-run work or apply a later processing ceiling.
V1_TASKS, V1_PARTICIPANTS, V1_INPUTS = 100, 20, 2000
V1_MANIFEST, V1_WORK, V1_CALLS = 1024 * 1024, 5_000_000, 380


def fail(code, status=409):
    raise ner.Error('NER_BATCH_' + code, status)


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
    if not isinstance(value, list) or not value or any(not ner_api.positive(v) for v in value):
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
        ids = list(Task.objects.filter(project=project).order_by('pk').values_list('pk', flat=True)[:MAX_TASKS + 1])
    elif set(scope) == {'kind', 'task_ids'} and scope['kind'] == 'tasks':
        ids = scope['task_ids']
    else:
        fail('INVALID_INPUT', 400)
    return name, positive_ids(ids)


def unavailable(count, reason):
    return {'input_count': count, 'total_pairs': count * (count - 1) // 2,
            'availability': 'unavailable', 'reason': reason, 'pairs': None,
            'comparable_pairs': 0, 'unavailable_pairs': count * (count - 1) // 2,
            'selected_pair': None}


def summary(logical):
    pairs = logical['pairs']
    comparable = [p for p in (pairs or []) if p['f1'] is not None]
    outcome = ('unavailable' if not comparable else 'disagreement' if any(
        p['f1']['numerator'] != p['f1']['denominator'] for p in comparable) else 'agreement')
    return {'outcome': outcome, 'reason': logical['reason'], 'input_count': logical['input_count'],
            'total_pairs': logical['total_pairs'], 'comparable_pairs': len(comparable),
            'calculation_hash': ner.digest(logical)}


def prepare(cohort, rows, reason):
    return ({'logical': unavailable(len(cohort), reason), 'engine_inputs': []} if reason else
            ner.prepare([r.pk for r in rows], [r.result_snapshot for r in rows]))


def budget(prepared):
    # This is one request budget, not a per-task multiplier. Both response
    # validation passes are included in the scorer call ceiling.
    work, calls = 0, 0
    for value in prepared.values():
        work += value.get('work_units', 0)
        rows = value['engine_inputs']
        calls += sum(bool(a or b) for a, b in itertools.combinations(rows, 2))
    if work > ner.MAX_WORK or 2 * calls > MAX_CALLS:
        fail('TOO_LARGE', 422)
    return work, calls


def check_engine(prepared):
    budget(prepared)
    for value in prepared.values():
        ner.check_engine(value)


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
    assignments = list(query[:MAX_INPUTS + 1])
    if len(assignments) > MAX_INPUTS:
        fail('TOO_LARGE', 422)
    grouped = {t: [] for t in ids}
    for row in assignments:
        if row.project_id != project.pk or not ner_api.positive(row.assignee_id):
            fail('SOURCE_INVALID')
        grouped[row.task_id].append(row)
        if len(grouped[row.task_id]) > MAX_PARTICIPANTS:
            fail('TOO_LARGE', 422)
    newest = Submission.objects.filter(assignment_id=OuterRef('assignment_id')).order_by('-revision', '-pk')
    chosen = Submission.objects.filter(assignment_id__in=[a.pk for a in assignments],
                                       pk=Subquery(newest.values('pk')[:1]))
    selected = list(chosen.order_by('pk').values_list('pk', flat=True))
    if lock:
        list(Submission.objects.filter(pk__in=selected).select_for_update().order_by('pk').values_list('pk', flat=True))
    sources = ner_api.load(scoped_sources(project, selected))
    if {s.pk for s in sources} != set(selected):
        fail('SOURCE_CHANGED')
    by_assignment = {s.assignment_id: s for s in sources}
    items, prepared = [], {}
    for task_id in ids:
        cohort, rows, reason = [], [], None
        for assignment in grouped[task_id]:
            row = by_assignment.get(assignment.pk)
            if row is not None:
                if (row.submitted_by_id != assignment.assignee_id
                        or row.assignment.assignee_id != assignment.assignee_id
                        or row.assignment.task_id != assignment.task_id):
                    fail('SOURCE_INVALID')
                rows.append(row)
                if row.status == Submission.Status.SUPERSEDED:
                    reason = 'latest_revision_unusable'
            cohort.append({'assignment_id': assignment.pk, 'assignee_id': assignment.assignee_id,
                           'source': ner_api.pin(row) if row else None})
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
        items.append({'task_id': task_id, 'cohort': cohort, 'selection_reason': reason,
                      'result': summary(value['logical'])})
    work, calls = budget(prepared)
    plan = {'selection': SELECTION, 'rule': ner.RULE, 'explanation': ner.EXPLANATION, 'engine': ner.ENGINE,
            'task_ids': ids, 'tasks': items, 'work_units': work, 'scorer_calls_per_pass': calls}
    ner.bounded(plan, MAX_MANIFEST)
    return plan, prepared


def query_batches(project):
    table = connection.ops.quote_name(NerBatch._meta.db_table)
    column = table + '.' + connection.ops.quote_name('manifest')
    size = f'OCTET_LENGTH(CAST({column} AS TEXT))' if connection.vendor == 'postgresql' else f'LENGTH(CAST({column} AS BLOB))'
    return NerBatch.objects.filter(project=project).annotate(
        _size=RawSQL(size, [], output_field=models.BigIntegerField())).annotate(
        _frozen=Case(When(_size__lte=MAX_MANIFEST_STORED, then=F('manifest')),
                     default=Value(None), output_field=JSONField())).defer('manifest')


def checked(batch, project):
    if hasattr(batch, '_frozen'):
        if type(batch._size) is not int or not 0 <= batch._size <= MAX_MANIFEST_STORED:
            fail('MANIFEST_INVALID')
        batch.manifest = batch._frozen
    m = batch.manifest
    try:
        ner.bounded(m, V1_MANIFEST)
        keys = {'selection', 'rule', 'explanation', 'engine', 'task_ids', 'tasks', 'work_units', 'scorer_calls_per_pass',
                'schema', 'project_id', 'organization_id', 'creator_principal', 'creator_source', 'actor_id_frozen', 'name',
                'scope_enumerated_at', 'calculated_at'}
        valid = (set(m) == keys and ner.digest(m) == batch.manifest_hash and m['schema'] == SCHEMA
                 and m['project_id'] == project.pk and m['organization_id'] == project.organization_id
                 and m['creator_principal'] == batch.creator_principal and m['actor_id_frozen'] == batch.actor_id_frozen
                 and isinstance(m['creator_source'], str) and bool(m['creator_source'])
                 and m['name'] == batch.name and m['selection'] == SELECTION and m['rule'] == ner.RULE
                 and m['explanation'] == ner.EXPLANATION and m['engine'] == ner.ENGINE
                 and isinstance(m['task_ids'], list) and 1 <= len(m['task_ids']) <= V1_TASKS
                 and all(ner_api.positive(i) for i in m['task_ids'])
                 and sorted(set(m['task_ids'])) == m['task_ids']
                 and m['task_ids'] == [i['task_id'] for i in m['tasks']])
        from django.utils import timezone
        for key in ('scope_enumerated_at', 'calculated_at'):
            valid = valid and isinstance(m[key], str) and not timezone.is_naive(timezone.datetime.fromisoformat(m[key]))
        assignment_ids, source_ids = [], []
        for item in m['tasks']:
            cohort = item['cohort']
            valid = valid and set(item) == {'task_id', 'cohort', 'selection_reason', 'result'}
            valid = valid and item['selection_reason'] in (None, 'insufficient_inputs', 'missing_submission', 'latest_revision_unusable')
            valid = valid and len(cohort) <= V1_PARTICIPANTS
            valid = valid and len({e['assignee_id'] for e in cohort}) == len(cohort)
            for entry in cohort:
                valid = valid and set(entry) == {'assignment_id', 'assignee_id', 'source'}
                valid = valid and ner_api.positive(entry['assignment_id']) and ner_api.positive(entry['assignee_id'])
                assignment_ids.append(entry['assignment_id'])
                pin = entry['source']
                if pin is not None:
                    source_ids.append(pin['submission_id'])
                    valid = valid and (set(pin) == set(ner_api.PIN_IDS + ner_api.PIN_HASHES)
                        and all(ner_api.positive(pin[k]) for k in ner_api.PIN_IDS)
                        and all(isinstance(pin[k], str) and ner_api.HEX.fullmatch(pin[k]) for k in ner_api.PIN_HASHES)
                        and pin['task_id'] == item['task_id'] and pin['assignment_id'] == entry['assignment_id']
                        and pin['submitter_id'] == pin['assignee_id'] == entry['assignee_id'])
            valid = valid and ner_api.HEX.fullmatch(item['result']['calculation_hash']) is not None
            result = item['result']
            valid = valid and set(result) == {'outcome', 'reason', 'input_count', 'total_pairs', 'comparable_pairs', 'calculation_hash'}
            valid = valid and result['outcome'] in ('agreement', 'disagreement', 'unavailable')
            valid = valid and type(result['input_count']) is int and result['input_count'] == len(cohort)
            valid = valid and type(result['total_pairs']) is int and result['total_pairs'] == len(cohort) * (len(cohort)-1)//2
            valid = valid and type(result['comparable_pairs']) is int and 0 <= result['comparable_pairs'] <= result['total_pairs']
            valid = valid and ((result['outcome'] == 'unavailable' and result['comparable_pairs'] == 0 and isinstance(result['reason'], str))
                              or (result['outcome'] != 'unavailable' and result['comparable_pairs'] > 0 and result['reason'] is None))
        valid = valid and sum(len(i['cohort']) for i in m['tasks']) <= V1_INPUTS
        valid = valid and len(assignment_ids) == len(set(assignment_ids)) and len(source_ids) == len(set(source_ids))
        valid = valid and type(m['work_units']) is int and 0 <= m['work_units'] <= V1_WORK
        valid = valid and type(m['scorer_calls_per_pass']) is int and 0 <= 2*m['scorer_calls_per_pass'] <= V1_CALLS
        valid = valid and m['scorer_calls_per_pass'] == sum(i['result']['comparable_pairs'] for i in m['tasks'])
    except (TypeError, ValueError, KeyError, AttributeError, ner.Error):
        valid = False
    if not valid:
        fail('MANIFEST_INVALID')
    return batch


def visible_items(batch, actor, role):
    return [item for item in batch.manifest['tasks'] if role == 'manager' or not any(
        e['assignee_id'] == actor.pk or (e['source'] or {}).get('submitter_id') == actor.pk for e in item['cohort'])]


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
        rows = rows.annotate(_visible=RawSQL(expression, parameters, output_field=models.BooleanField())).filter(_visible=True)
    return rows.order_by('-pk')


def metadata(batch, context):
    _, actor, project, role = context
    items = visible_items(batch, actor, role)
    if not items:
        raise NotFound()
    reference = batch.manifest_hash if role == 'manager' else ner.digest(
        {'schema': SCHEMA, 'batch': batch.pk, 'rule': ner.RULE, 'tasks': items})
    return {'id': batch.pk, 'project_id': project.pk, 'name': batch.name, 'schema': SCHEMA, 'rule': ner.RULE,
            'explanation': ner.EXPLANATION, 'engine': ner.ENGINE, 'selection': SELECTION,
            'created_at': batch.created_at.isoformat(), 'calculated_at': batch.manifest['calculated_at'],
            'scope_enumerated_at': batch.manifest['scope_enumerated_at'], 'visible_task_count': len(items),
            'visibility': 'project' if role == 'manager' else 'visible_subset', 'viewer': ner_api.viewer(context),
            'manifest_hash' if role == 'manager' else 'projection_hash': reference}


def plan_hash(batch):
    return ner.digest({k: batch.manifest[k] for k in ('selection', 'rule', 'explanation', 'engine', 'task_ids',
                                                    'tasks', 'work_units', 'scorer_calls_per_pass')})


def project_items(batch, context):
    items = visible_items(batch, context[1], context[3])
    task_ids = [i['task_id'] for i in items]
    tasks = dict(Task.objects.filter(pk__in=task_ids).values_list('pk', 'project_id'))
    cohort = [e for i in items for e in i['cohort']]
    assignments = {a.pk: a for a in TaskAssignment.objects.filter(pk__in=[e['assignment_id'] for e in cohort])}
    source_ids = [e['source']['submission_id'] for e in cohort if e['source']]
    # The same cumulative SQL/JSON byte guard as manual NER, including growth
    # between preflight and hydration. Hidden cohorts never reach this query.
    rows = ner_api.load(scoped_sources(context[2], source_ids), validate_source=False)
    sources = {r.pk: r for r in rows}
    projected, prepared = [], {}
    for item in items:
        task_id, current_rows, reason = item['task_id'], [], None
        if tasks.get(task_id) != batch.project_id:
            reason = 'source_unavailable'
        for entry in item['cohort']:
            assignment = assignments.get(entry['assignment_id'])
            if (assignment is None or assignment.project_id != batch.project_id or assignment.task_id != task_id
                    or assignment.assignee_id != entry['assignee_id']):
                reason = 'source_unavailable'
            pin = entry['source']
            if pin:
                row = sources.get(pin['submission_id'])
                try:
                    valid = row is not None and valid_source(row, batch.project_id) and ner_api.pin(row) == pin
                except (KeyError, TypeError, ValueError, AttributeError):
                    valid = False
                if not valid:
                    reason = 'source_unavailable'
                else:
                    current_rows.append(row)
        current_rows.sort(key=lambda r: r.pk)
        if reason:
            value = {'logical': unavailable(len(item['cohort']), reason), 'engine_inputs': []}
        else:
            value = prepare(item['cohort'], current_rows, item['selection_reason'])
            if summary(value['logical']) != item['result']:
                fail('RESULT_CHANGED')
            prepared[task_id] = value
            budget(prepared)
        frozen = item['result']
        projected.append({'task_id': task_id, 'outcome': 'unavailable' if reason else frozen['outcome'],
                          'reason': reason or frozen['reason'], 'original_outcome': frozen['outcome'],
                          'input_count': frozen['input_count'], 'total_pairs': frozen['total_pairs'],
                          'comparable_pairs': 0 if reason else frozen['comparable_pairs'],
                          'calculation_hash': None if reason else frozen['calculation_hash'],
                          'source_status': 'unavailable' if reason else 'verified',
                          'inputs': [] if reason else sorted([e['source'] for e in item['cohort'] if e['source']],
                                                           key=lambda p: p['submission_id']),
                          '_prepared': value})
    budget(prepared)
    return projected, prepared


def public_row(row):
    return {k: v for k, v in row.items() if k not in ('_prepared', 'inputs')}


def counts(items):
    return {state: sum(i['outcome'] == state for i in items) for state in ('agreement', 'disagreement', 'unavailable')}
