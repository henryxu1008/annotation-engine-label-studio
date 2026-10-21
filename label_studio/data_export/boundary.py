"""Durable evidence that mutable project delivery must remain closed."""


def is_approved_export_path(path):
    """Reserve private artifacts from generic filesystem media/import routes.

    This reservation outlives the project's release policy. Resolve symlinks so
    an allowed import directory cannot alias private files into another project.
    """
    from pathlib import Path
    from django.conf import settings

    root = getattr(settings, 'APPROVED_EXPORT_ROOT', None)
    if not root or not path:
        return False
    try:
        root = Path(root).resolve()
        candidate = Path(path).resolve()
        return candidate == root or root in candidate.parents
    except (OSError, RuntimeError, ValueError, TypeError):
        return True  # Scope cannot be established safely.


def has_immutable_release_boundary(project_id):
    from tasks.models import Submission
    from .approved_models import ApprovedDatasetRelease

    return (
        Submission.objects.filter(assignment__project_id=project_id).exists()
        or ApprovedDatasetRelease.objects.filter(project_id=project_id).exists()
    )
