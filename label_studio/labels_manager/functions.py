from django.db import transaction
from tasks.models import Annotation


def bulk_update_label(old_label, new_label, organization, project=None, allowed_project_ids=None):
    from classification_disagreements.blind_quality_boundary import quality_task_ids

    # Quality-only annotations never participate in ordinary bulk operations.
    annotations = Annotation.objects.filter(project__organization=organization).exclude(task_id__in=quality_task_ids())
    if allowed_project_ids is not None:
        annotations = annotations.filter(project_id__in=allowed_project_ids)
    if project is not None:
        annotations = annotations.filter(project=project)

    updated_count = 0
    with transaction.atomic():
        update_annotations = []
        for annotation in annotations.only('result').all():
            result = annotation.result

            updated_result = []
            need_update = False
            for region in result:
                result_type = region.get('type')
                if result_type is not None:
                    label = region['value'].get(result_type)
                    if label is not None and label == old_label:
                        region['value'][result_type] = new_label
                        updated_count += 1
                        need_update = True
                updated_result.append(region)

            if need_update:
                annotation.result = updated_result
                update_annotations.append(annotation)

        if update_annotations:
            Annotation.objects.bulk_update(update_annotations, ['result'])
    return updated_count
