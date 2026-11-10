"""Bounded bbox cohort planning and frozen-source replay."""

from django.db import connection, models
from django.db.models.expressions import RawSQL
from tasks.models import Submission, Task

from . import bbox, bbox_compare, ner, ner_api, ner_batches
from .models import BboxSubmissionProof

MAX_TASKS = 5
MAX_INPUTS = 100
MAX_TASK_DATA_BYTES = 1024 * 1024
MAX_PLAN_BYTES = 5 * 1024 * 1024


def counts(items):
    result = {'agreement': 0, 'disagreement': 0, 'unavailable': 0}
    for item in items:
        comparison = item['result']
        pairs = comparison.get('pairs') if comparison.get('availability') == 'available' else None
        outcome = ('unavailable' if not pairs else 'disagreement' if any(
            pair['f1']['numerator'] != pair['f1']['denominator'] for pair in pairs
        ) else 'agreement')
        result[outcome] += 1
    return result


def bounded_tasks(project, ids):
    table = connection.ops.quote_name(Task._meta.db_table)
    column = table + '.' + connection.ops.quote_name('data')
    length = (f'OCTET_LENGTH(CAST({column} AS TEXT))' if connection.vendor == 'postgresql'
              else f'LENGTH(CAST({column} AS BLOB))')
    tasks = Task.objects.filter(pk__in=ids, project=project).annotate(
        _bbox_data_size=RawSQL(length, [], output_field=models.BigIntegerField())
    ).filter(_bbox_data_size__lte=MAX_TASK_DATA_BYTES).only('pk', 'project_id', 'data')
    by_task = {task.pk: task for task in tasks}
    if set(by_task) != set(ids):
        raise bbox.Error('BBOX_BATCH_SOURCE_UNAVAILABLE', 409)
    return by_task


def build_plan(project, task_ids, *, lock=False):
    ids = ner_batches.positive_ids(task_ids)
    if len(ids) > MAX_TASKS:
        raise bbox.Error('BBOX_BATCH_TOO_LARGE', 422)
    ids, grouped, by_assignment = ner_batches.collect_inputs(project, ids, lock=lock)
    if sum(len(values) for values in grouped.values()) > MAX_INPUTS:
        raise bbox.Error('BBOX_BATCH_TOO_LARGE', 422)
    by_task = bounded_tasks(project, ids)
    selected_ids = [row.pk for row in by_assignment.values()]
    proofs = {p.submission_id: p for p in BboxSubmissionProof.objects.filter(
        submission_id__in=selected_ids).select_related('media')}
    items = []
    for task_id in ids:
        cohort, rows = [], []
        for assignment in grouped[task_id]:
            row = by_assignment.get(assignment.pk)
            if row and (row.submitted_by_id != assignment.assignee_id
                        or row.assignment.task_id != task_id or row.assignment.assignee_id != assignment.assignee_id):
                raise bbox.Error('BBOX_BATCH_SOURCE_INVALID', 409)
            proof = proofs.get(row.pk) if row else None
            source = None
            if row:
                rows.append(row)
                source = {**ner_api.pin(row),
                          'media': ({'token': str(proof.media.token), 'sha256': proof.media.sha256,
                                     'proof_hash': proof.proof_hash} if proof else None)}
            cohort.append({'assignment_id': assignment.pk, 'assignee_id': assignment.assignee_id, 'source': source})
        if len({member['assignee_id'] for member in cohort}) != len(cohort):
            raise bbox.Error('BBOX_BATCH_SOURCE_INVALID', 409)
        if len(cohort) < 2:
            result = {'availability': 'unavailable', 'reason': 'insufficient_inputs', 'pairs': None}
        elif any(member['source'] is None for member in cohort):
            result = {'availability': 'unavailable', 'reason': 'missing_submission', 'pairs': None}
        elif any(row.status == Submission.Status.SUPERSEDED for row in rows):
            result = {'availability': 'unavailable', 'reason': 'latest_revision_unusable', 'pairs': None}
        else:
            rows.sort(key=lambda row: row.pk)
            try:
                result = bbox_compare.prepare(rows, project=project, task=by_task[task_id])
            except bbox.Error as exc:
                if exc.code not in ('BBOX_UNVERIFIED_MEDIA', 'BBOX_SOURCE_UNAVAILABLE'):
                    raise
                result = {'availability': 'unavailable', 'reason': 'unverified_media', 'pairs': None}
        items.append({'task_id': task_id, 'cohort': cohort, 'result': result})
    plan = {'schema': 'bbox-quality-plan/v1', 'rule': bbox.RULE, 'engine': bbox.ENGINE,
            'selection': ner_batches.SELECTION, 'task_ids': ids, 'items': items}
    bbox.bounded(plan, MAX_PLAN_BYTES)
    return plan


def visible_items(plan, actor, role):
    """Reuse the existing whole-cohort self-exclusion rule."""
    return [item for item in plan['items'] if ner_batches.can_view_item(item, actor, role)]


def replay(project, plan):
    """Verify exact frozen sources, without replacing them with newer work."""
    try:
        bbox.bounded(plan, MAX_PLAN_BYTES)
        if (not isinstance(plan, dict) or set(plan) != {'schema', 'rule', 'engine', 'selection', 'task_ids', 'items'}
                or plan['schema'] != 'bbox-quality-plan/v1' or plan['rule'] != bbox.RULE
                or plan['engine'] != bbox.ENGINE or plan['selection'] != ner_batches.SELECTION):
            raise ValueError('invalid plan')
        ids = ner_batches.positive_ids(plan['task_ids'])
        if (len(ids) > MAX_TASKS or ids != plan['task_ids'] or not isinstance(plan['items'], list)
                or [item['task_id'] for item in plan['items']] != ids):
            raise ValueError('invalid scope')
        source_ids, assignment_ids = [], []
        for item in plan['items']:
            if (set(item) != {'task_id', 'cohort', 'result'} or not isinstance(item['cohort'], list)
                    or len(item['cohort']) > bbox.MAX_INPUTS):
                raise ValueError('invalid cohort')
            actors = []
            for entry in item['cohort']:
                if (set(entry) != {'assignment_id', 'assignee_id', 'source'}
                        or not ner_api.positive(entry['assignment_id']) or not ner_api.positive(entry['assignee_id'])):
                    raise ValueError('invalid member')
                assignment_ids.append(entry['assignment_id'])
                actors.append(entry['assignee_id'])
                source = entry['source']
                if source is not None:
                    if (not isinstance(source, dict) or set(source) != set(ner_api.PIN_IDS + ner_api.PIN_HASHES + ('media',))
                            or any(not ner_api.positive(source[key]) for key in ner_api.PIN_IDS)
                            or any(not isinstance(source[key], str) or not ner_api.HEX.fullmatch(source[key])
                                   for key in ner_api.PIN_HASHES)
                            or source['task_id'] != item['task_id'] or source['assignment_id'] != entry['assignment_id']
                            or source['assignee_id'] != entry['assignee_id'] or source['submitter_id'] != entry['assignee_id']):
                        raise ValueError('invalid source')
                    source_ids.append(source['submission_id'])
            if len(set(actors)) != len(actors):
                raise ValueError('duplicate actor')
        if (len(assignment_ids) > MAX_INPUTS or len(set(assignment_ids)) != len(assignment_ids)
                or len(set(source_ids)) != len(source_ids)):
            raise ValueError('duplicate input')
        tasks = bounded_tasks(project, ids)
        rows = ner_api.load(ner_batches.scoped_sources(project, source_ids))
        by_id = {row.pk: row for row in rows}
        if set(by_id) != set(source_ids):
            raise ValueError('missing source')
        proofs = {p.submission_id: p for p in BboxSubmissionProof.objects.filter(
            submission_id__in=source_ids).select_related('media')}
        for item in plan['items']:
            selected = []
            for entry in item['cohort']:
                source = entry['source']
                if source is None:
                    continue
                row = by_id[source['submission_id']]
                if ner_api.pin(row) != {key: source[key] for key in ner_api.PIN_IDS + ner_api.PIN_HASHES}:
                    raise ValueError('source changed')
                proof = proofs.get(row.pk)
                media = ({'token': str(proof.media.token), 'sha256': proof.media.sha256,
                          'proof_hash': proof.proof_hash} if proof else None)
                if source['media'] != media:
                    raise ValueError('media changed')
                selected.append(row)
            saved = item['result']
            if not isinstance(saved, dict) or saved.get('availability') not in ('available', 'unavailable'):
                raise ValueError('invalid result')
            if saved['availability'] == 'available':
                current = bbox_compare.prepare(sorted(selected, key=lambda row: row.pk),
                                               project=project, task=tasks[item['task_id']])
                if bbox.digest(current) != bbox.digest(saved):
                    raise ValueError('result changed')
        return plan
    except (KeyError, TypeError, ValueError, AttributeError, bbox.Error, ner.Error) as exc:
        raise bbox.Error('BBOX_BATCH_SOURCE_UNAVAILABLE', 409) from exc
