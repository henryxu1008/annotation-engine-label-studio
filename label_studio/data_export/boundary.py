"""Durable evidence that mutable project delivery must remain closed."""


def has_immutable_release_boundary(project_id):
    from tasks.models import Submission
    from .approved_models import ApprovedDatasetRelease

    return (
        Submission.objects.filter(assignment__project_id=project_id).exists()
        or ApprovedDatasetRelease.objects.filter(project_id=project_id).exists()
    )
