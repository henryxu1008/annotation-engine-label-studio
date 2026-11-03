"""Narrow existing-source adapter: every selected payload/scalar shares one gate.

No live task data, user profiles, whole cohorts or deferred relation hydration.
The old supervision loader and its callers keep their original projection.
"""

from tasks.models import ReviewDecision, Task, TaskAssignment

from . import second_review_contract as c
from . import service


def one(rows):
    if len(rows) != 1:
        c.fail('SOURCE_UNAVAILABLE')
    return rows[0]


def load(query, budget):
    row = one(
        budget.rows(
            query.only(
                'id', 'assignment_id', 'revision', 'result_hash', 'submitted_by_id', 'status', 'result_snapshot'
            ),
            {'result_snapshot': (256 * 1024, 2 * c.MIB)},
        )
    )
    assignment = one(
        budget.rows(
            TaskAssignment.objects.filter(pk=row.assignment_id).only(
                'id', 'project_id', 'task_id', 'assignee_id', 'status'
            ),
            {},
        )
    )
    task = one(budget.rows(Task.objects.filter(pk=assignment.task_id).only('id', 'project_id'), {}))
    reviews = budget.rows(
        ReviewDecision.objects.filter(submission_id=row.pk).only(
            'id', 'submission_id', 'reviewer_id', 'decision', 'reason', 'created_at'
        ),
        {'reason': (8192, 32768)},
    )
    if len(reviews) > 1:
        c.fail()
    review = reviews[0] if reviews else None
    assignment.task = task
    row.assignment = assignment
    row.review = review
    row.ner_row_size = row._sr_size_result_snapshot
    row.ner_reason_total = review._sr_size_reason if review else 0
    row.ner_review_timestamp_size = review._sr_size_created_at if review else 0
    try:
        if not service.valid_source(row, assignment.project_id):
            c.fail()
    except (ValueError, TypeError, KeyError, AttributeError):
        c.fail()
    return [row]
