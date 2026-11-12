from core.feature_flags import flag_set
from core.utils.db import SQCount
from django.db.models import Count, OuterRef, Q
from tasks.models import Annotation, Prediction, Task


def quality_task_ids():
    # Retained purpose markers remain authoritative even after a quality
    # assignment is cancelled or deleted.
    from classification_disagreements.blind_quality_boundary import quality_task_ids as retained_quality_task_ids

    return retained_quality_task_ids()


def annotate_task_number(queryset):
    tasks = Task.objects.filter(project=OuterRef('id')).exclude(pk__in=quality_task_ids()).values_list('id')
    return queryset.annotate(task_number=SQCount(tasks))


def annotate_finished_task_number(queryset):
    if flag_set('fflag_fix_back_plt_811_finished_task_number_01072025_short', user='auto'):
        # Membership joins can multiply task rows for project owners/managers.
        return queryset.annotate(finished_task_number=Count(
            'tasks', filter=Q(tasks__is_labeled=True) & ~Q(tasks__pk__in=quality_task_ids()), distinct=True
        ))
    else:
        tasks = Task.objects.filter(project=OuterRef('id'), is_labeled=True).exclude(pk__in=quality_task_ids()).values_list('id')
        return queryset.annotate(finished_task_number=SQCount(tasks))


def annotate_total_predictions_number(queryset):
    predictions = Prediction.objects.filter(project=OuterRef('id')).exclude(task_id__in=quality_task_ids()).values('id')
    return queryset.annotate(total_predictions_number=SQCount(predictions))


def annotate_total_annotations_number(queryset):
    subquery = Annotation.objects.filter(Q(project=OuterRef('pk')) & Q(was_cancelled=False)).exclude(
        task_id__in=quality_task_ids()
    ).values('id')
    return queryset.annotate(total_annotations_number=SQCount(subquery))


def annotate_num_tasks_with_annotations(queryset):
    # @todo: check do we really need this counter?
    # this function is very slow because of tasks__id and distinct
    subquery = (
        Annotation.objects.filter(
            Q(project=OuterRef('pk')) & Q(ground_truth=False) & Q(was_cancelled=False) & Q(result__isnull=False)
        )
        .exclude(task_id__in=quality_task_ids())
        .values('task__id')
        .distinct()
    )
    return queryset.annotate(num_tasks_with_annotations=SQCount(subquery))


def annotate_useful_annotation_number(queryset):
    subquery = Annotation.objects.filter(
        Q(project=OuterRef('pk')) & Q(was_cancelled=False) & Q(ground_truth=False) & Q(result__isnull=False)
    ).exclude(task_id__in=quality_task_ids()).values('id')
    return queryset.annotate(useful_annotation_number=SQCount(subquery))


def annotate_ground_truth_number(queryset):
    subquery = Annotation.objects.filter(Q(project=OuterRef('pk')) & Q(ground_truth=True)).exclude(
        task_id__in=quality_task_ids()
    ).values('id')
    return queryset.annotate(ground_truth_number=SQCount(subquery))


def annotate_skipped_annotations_number(queryset):
    subquery = Annotation.objects.filter(Q(project=OuterRef('pk')) & Q(was_cancelled=True)).exclude(
        task_id__in=quality_task_ids()
    ).values('id')
    return queryset.annotate(skipped_annotations_number=SQCount(subquery))
