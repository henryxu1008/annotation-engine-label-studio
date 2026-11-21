"""Internal quality-purpose checks. Creating and exposing markers is not yet supported."""

from django.db import connections
from django.db.models import Exists, OuterRef

from .models import BlindQualityAssignment


def is_quality_only_assignment(assignment_id):
    from tasks.models import TaskAssignment

    task = TaskAssignment.objects.filter(pk=assignment_id).values('task_id')
    return BlindQualityAssignment.objects.filter(task_id__in=task).exists()


def is_quality_only_annotation(annotation_id):
    from tasks.models import Annotation

    task = Annotation.objects.filter(pk=annotation_id).values('task_id')
    return BlindQualityAssignment.objects.filter(task_id__in=task).exists()


def quality_task_ids(project_id=None):
    # IDs are global and retained even if a task is moved or its project is deleted.
    return BlindQualityAssignment.objects.values('task_id')


def is_quality_only_submission(submission):
    # The supported-database marker is immutable and cannot be added once a
    # submission exists. A marker joined in the same SQL read therefore stays
    # valid without issuing a query for every row in a bounded batch.
    using = submission._state.db or 'default'
    if (connections[using].vendor in ('postgresql', 'sqlite')
            and type(getattr(submission, '_blind_quality_snapshot', None)) is bool):
        return submission._blind_quality_snapshot
    from tasks.models import TaskAssignment

    task = TaskAssignment.objects.using(using).filter(pk=submission.assignment_id).values('task_id')
    return BlindQualityAssignment.objects.using(using).filter(task_id__in=task).exists()


def with_quality_snapshot(queryset):
    markers = BlindQualityAssignment.objects.filter(task_id=OuterRef('assignment__task_id'))
    return queryset.annotate(_blind_quality_snapshot=Exists(markers))
