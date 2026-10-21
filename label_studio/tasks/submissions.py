"""Immutable annotation submission and review services."""

import hashlib
import json

from django.db import transaction
from django.db.models import Max
from projects.models import Project
from rest_framework.exceptions import ValidationError

from tasks.models import ReviewDecision, Submission, TaskAssignment


def _canonical_snapshot(annotation, assignment):
    task = assignment.task
    project = assignment.project

    return {
        'annotation': {
            'id': annotation.id,
            'result': annotation.result or [],
            'was_cancelled': annotation.was_cancelled,
            'ground_truth': annotation.ground_truth,
            'lead_time': annotation.lead_time,
        },
        'task': {
            'id': task.id if task else None,
            'data': task.data if task else None,
            'meta': task.meta if task else None,
        },
        'project': {
            'id': project.id if project else None,
            'label_config': project.label_config if project else None,
            'label_config_hash': project.label_config_hash if project else None,
        },
    }


def _snapshot_hash(snapshot):
    canonical = json.dumps(snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


@transaction.atomic
def create_submission(*, assignment, annotation, actor):
    """Create the next immutable revision for an assignment.

    Pending older revisions become superseded. Previously approved/rejected
    revisions remain immutable historical evidence.
    """

    Project.objects.select_for_update().get(pk=assignment.project_id)

    assignment = (
        TaskAssignment.objects.select_for_update()
        # The nullable annotation join cannot participate in PostgreSQL's
        # FOR UPDATE. Writes are serialized by the locked assignment, and the
        # snapshot below uses the annotation supplied by the write service.
        .select_related('task', 'project', 'assignee')
        .get(pk=assignment.pk)
    )

    if assignment.assignee_id != actor.id:
        raise ValidationError({'detail': 'Only the assignment owner can submit this annotation.'})
    if assignment.annotation_id != annotation.id:
        raise ValidationError({'detail': 'Annotation does not belong to this assignment.'})
    if annotation.task_id != assignment.task_id or annotation.project_id != assignment.project_id:
        raise ValidationError({'detail': 'Annotation task/project must match the locked assignment.'})

    last_revision = (
        Submission.objects.filter(assignment=assignment).aggregate(max_revision=Max('revision'))['max_revision'] or 0
    )
    revision = last_revision + 1

    Submission.objects.filter(
        assignment=assignment,
        status=Submission.Status.PENDING,
    ).update(status=Submission.Status.SUPERSEDED)

    snapshot = _canonical_snapshot(annotation, assignment)
    submission = Submission.objects.create(
        assignment=assignment,
        annotation=annotation,
        revision=revision,
        result_snapshot=snapshot,
        result_hash=_snapshot_hash(snapshot),
        submitted_by=actor,
    )

    # Bump the optimistic-lock token so another tab holding the pre-submit
    # assignment version cannot overwrite this newly submitted revision.
    assignment.version += 1
    assignment.save(update_fields=['version', 'updated_at'])

    return submission


@transaction.atomic
def review_submission(*, submission, reviewer, decision, reason=''):
    # submitted_by is nullable; its ID is sufficient for the self-review check.
    submission = Submission.objects.select_for_update().select_related('assignment').get(
        pk=submission.pk
    )

    if submission.status != Submission.Status.PENDING:
        raise ValidationError({'detail': 'Only pending submissions can be reviewed.'})
    if submission.submitted_by_id == reviewer.id:
        raise ValidationError({'detail': 'A user cannot review their own submission.'})
    if decision == ReviewDecision.Decision.REJECTED and not reason.strip():
        raise ValidationError({'reason': 'A rejection reason is required.'})

    review = ReviewDecision.objects.create(
        submission=submission,
        reviewer=reviewer,
        decision=decision,
        reason=reason.strip(),
    )
    submission.status = (
        Submission.Status.APPROVED
        if decision == ReviewDecision.Decision.APPROVED
        else Submission.Status.REJECTED
    )
    submission.save(update_fields=['status'])
    return review
