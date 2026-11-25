"""Private participant progress projection for opted-in classification projects.

This does not make task identities or the rest of the participant API blind.
It is applied uniformly to ordinary and quality work before any sample exists.
"""

from access_control.authorization import authorization
from access_control.identity import resolve_actor
from django.conf import settings
from django.db.models import Count, Q

from .models import BlindQualityAssignment


def sensitive_project(project):
    configured = settings.ANNOTATION_CLASSIFICATION_BLIND_PROJECTS
    opted_in = (settings.ANNOTATION_CLASSIFICATION_BLIND_ENABLED and isinstance(configured, dict)
                and configured.get(f'{project.organization_id}:{project.pk}') is True)
    return opted_in or BlindQualityAssignment.objects.filter(project_id=project.pk).exists()


def sensitive_project_filter(project_field):
    """SQL scope matching sensitive_project for project-linked collections."""
    sensitive = Q(**{f'{project_field}_id__in': BlindQualityAssignment.objects.values('project_id')})
    configured = settings.ANNOTATION_CLASSIFICATION_BLIND_PROJECTS
    if settings.ANNOTATION_CLASSIFICATION_BLIND_ENABLED and isinstance(configured, dict):
        for scope, enabled in configured.items():
            if enabled is not True or not isinstance(scope, str):
                continue
            parts = scope.split(':')
            if len(parts) == 2 and all(part.isascii() and part.isdecimal() for part in parts):
                sensitive |= Q(**{f'{project_field}_id': int(parts[1]),
                                  f'{project_field}__organization_id': int(parts[0])})
    return sensitive


def sensitive_nonmanager(project, principal):
    return sensitive_project(project) and not authorization.can_manage_project(principal, project)


def participant_for_project(request, project):
    if request is None or not getattr(getattr(request, 'user', None), 'is_authenticated', False):
        return None
    if not sensitive_project(project):
        return None
    principal, actor = resolve_actor(request)
    return actor if not authorization.can_manage_project(principal, project) else None


def participant_counts(project, actor):
    from tasks.my_work import annotation_work

    assignments = annotation_work(project, actor).order_by()
    annotated = Q(annotation_id__isnull=False)
    non_skipped = annotated & Q(annotation__was_cancelled=False)
    skipped = annotated & Q(annotation__was_cancelled=True)
    useful = non_skipped & Q(annotation__ground_truth=False) & Q(annotation__result__isnull=False)
    counts = assignments.aggregate(
        total=Count('pk', distinct=True), done=Count('pk', filter=non_skipped, distinct=True),
        skipped=Count('pk', filter=skipped, distinct=True), useful=Count('pk', filter=useful, distinct=True),
        ground_truth=Count('pk', filter=annotated & Q(annotation__ground_truth=True), distinct=True),
        with_annotations=Count('pk', filter=useful, distinct=True),
        queue_all=Count('pk', filter=annotated, distinct=True),
    )
    queue_done = counts['done'] if project.skip_queue == project.SkipQueue.REQUEUE_FOR_ME else counts['queue_all']
    return {
        'task_number': counts['total'], 'finished_task_number': counts['done'],
        'total_annotations_number': counts['done'], 'total_predictions_number': 0,
        'num_tasks_with_annotations': counts['with_annotations'], 'useful_annotation_number': counts['useful'],
        'ground_truth_number': counts['ground_truth'], 'skipped_annotations_number': counts['skipped'],
        'queue_total': counts['total'], 'queue_done': queue_done,
    }


def participant_summary(project):
    # Configuration is stable across both outcomes and does not disclose task
    # or answer frequencies. Managers retain the full ordinary summary.
    fields = sorted((project.data_types or {}).keys())
    return {
        'all_data_columns': {field: None for field in fields},
        'common_data_columns': fields,
        'created_annotations': {}, 'created_labels': {}, 'created_labels_drafts': {},
    }


def participant_task_metadata(result, request, project):
    """Use one conservative metadata shape for ordinary and quality work."""
    if project is None or participant_for_project(request, project) is None:
        return result
    for field in ('created_at', 'updated_at', 'completed_at', 'inner_id', 'file_upload', 'storage_filename',
                  'predictions_score'):
        if field in result:
            result[field] = None
    if 'predictions' in result:
        result['predictions'] = []
    for field in ('predictions_results', 'predictions_ids', 'predictions_model_versions'):
        if field in result:
            result[field] = ''
    if 'total_predictions' in result:
        result['total_predictions'] = 0
    if 'meta' in result:
        result['meta'] = {}
    result.pop('state', None)
    return result


def restrict_prediction_queryset(queryset, request):
    """Hide offline predictions uniformly from non-managers in private projects."""
    from access_control.project_access import visible_projects_for_actor

    _, actor = resolve_actor(request)
    projects = visible_projects_for_actor(actor)
    managed = projects.filter(Q(created_by_id=actor.pk) | Q(
        members__user_id=actor.pk, members__enabled=True, members__role='manager',
    )).values('pk')
    sensitive = Q(project_id__in=BlindQualityAssignment.objects.values('project_id'))
    configured = settings.ANNOTATION_CLASSIFICATION_BLIND_PROJECTS
    if settings.ANNOTATION_CLASSIFICATION_BLIND_ENABLED and isinstance(configured, dict):
        for scope, enabled in configured.items():
            if enabled is not True or not isinstance(scope, str):
                continue
            parts = scope.split(':')
            if len(parts) == 2 and all(part.isdigit() for part in parts):
                sensitive |= Q(project_id=int(parts[1]), project__organization_id=int(parts[0]))
    return queryset.exclude(sensitive & ~Q(project_id__in=managed))
