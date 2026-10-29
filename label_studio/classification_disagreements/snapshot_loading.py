"""Shared SQL byte gate before immutable snapshot JSON decoding."""
from django.db import connection
from django.db.models import BigIntegerField, Case, F, JSONField, Sum, Value, When, Window
from django.db.models.expressions import RawSQL
from tasks.models import Submission


def load(query, *, policy, validate, validate_source=True, projection='ner'):
    if projection not in ('ner', 'review', 'submission'):
        raise ValueError('Unknown guarded source projection')
    # Scope first; count bytes in SQL before hydrating a JSONField or live task body.
    column = connection.ops.quote_name(Submission._meta.db_table) + '.' + connection.ops.quote_name('result_snapshot')
    expression = (
        f'OCTET_LENGTH(CAST({column} AS TEXT))'
        if connection.vendor == 'postgresql'
        else f'LENGTH(CAST({column} AS BLOB))'
    )
    size = RawSQL(expression, [], output_field=BigIntegerField())
    sizes = list(query.annotate(ner_size=size).values_list('ner_size', flat=True))
    if any(type(size) is not int or size < 0 for size in sizes) or sum(sizes) > policy.MAX_STORED_BYTES:
        raise policy.Error(policy.TOO_LARGE)
    rows, used = [], 0
    # The preflight and this SELECT may observe different committed rows. Gate
    # the entire selected payload inside the hydrating statement, so a growing
    # snapshot is never transferred/decoded before its budget is enforced.
    guarded = query.annotate(ner_total=Window(Sum(size))).annotate(
        ner_snapshot=Case(
            When(ner_total__lte=policy.MAX_STORED_BYTES, then=F('result_snapshot')),
            default=Value(None),
            output_field=JSONField(),
        )
    )
    fields = (
        'id',
        'assignment_id',
        'revision',
        'result_hash',
        'submitted_by_id',
        'status',
        'assignment__id',
        'assignment__task_id',
        'assignment__project_id',
        'assignment__assignee_id',
        'assignment__task__id',
        'assignment__task__project_id',
        'review__id',
        'review__decision',
    )
    related = ('assignment__task', 'review')
    if projection in ('review', 'submission'):
        fields += ('annotation_id', 'submitted_at', 'review__reason', 'review__reviewer_id', 'review__created_at')
    if projection == 'submission':
        related += ('submitted_by', 'review__reviewer')
        fields += ('submitted_by', 'review__reviewer')
    for row in guarded.select_related(*related).only(*fields).order_by('pk').iterator(chunk_size=1):
        if type(row.ner_total) is not int or row.ner_total < 0 or row.ner_total > policy.MAX_STORED_BYTES:
            raise policy.Error(policy.TOO_LARGE)
        row.result_snapshot = row.ner_snapshot
        try:
            used += policy.bounded(row.result_snapshot, policy.MAX_SNAPSHOT_BYTES)
            if used > policy.MAX_SNAPSHOT_BYTES:
                raise policy.Error(policy.TOO_LARGE)
            if validate_source and not validate(row, row.assignment.project_id):
                raise policy.Error(policy.SOURCE_INTEGRITY_FAILED, 409)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise policy.Error(policy.SOURCE_INTEGRITY_FAILED, 409)
        rows.append(row)
    return rows
