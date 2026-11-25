"""Internal validation of a pinned classification reference for blind work.

This does not grant access, create assignments, schedule scores or prove that an
actor was unexposed. A caller must lock and recheck project/task/assignment and
admission state in the transaction that binds or reads the reference.
"""

from rest_framework.exceptions import ValidationError


def checked_blind_reference(*, marker, project, task):
    from review_batches.service import digest

    from . import gold_scoring
    from .gold_api import checked

    def unavailable():
        raise ValidationError({'detail': 'The pinned quality reference is unavailable.'})

    if (marker.project_id != project.pk or marker.task_id != task.pk or task.project_id != project.pk
            or marker.reference_id is None or not isinstance(marker.reference_hash, str)):
        unavailable()
    reference = checked(project, marker.reference_id, 'reference')
    if reference.manifest_hash != marker.reference_hash:
        unavailable()
    contents = reference.manifest['contents']
    if (set(contents) != {'source', 'config', 'task_data', 'answer'}
            or not isinstance(contents['source'], dict)
            or type(contents['source'].get('task_id')) is not int
            or contents['source']['task_id'] <= 0 or contents['source']['task_id'] == task.pk
            or type(contents['source'].get('submitter_id')) is not int
            or contents['source']['submitter_id'] <= 0
            or not isinstance(contents['config'], str)
            or not isinstance(contents['task_data'], dict)
            or not isinstance(contents['answer'], list)
            or contents['config'] != project.label_config
            or digest(contents['task_data']) != digest(task.data)):
        unavailable()
    result = gold_scoring.score(contents['config'], contents['task_data'], contents['answer'], contents['answer'])
    if result['status'] != 'scored':
        unavailable()
    return reference
