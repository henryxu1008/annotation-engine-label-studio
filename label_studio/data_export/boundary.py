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


def is_private_local_path(path, *, storage_root=False):
    """Fail closed on private paths and on storage roots that contain them.

    BASE_DATA_DIR and APPROVED_EXPORT_ROOT are always protected. Deployments
    must add other private roots (for example separate database and log roots)
    to LOCAL_FILES_PROTECTED_ROOTS. Resolving also catches existing symlinks.
    """
    from pathlib import Path

    from django.conf import settings

    try:
        extra = getattr(settings, 'LOCAL_FILES_PROTECTED_ROOTS', [])
        if not isinstance(extra, (list, tuple)):
            return True
        roots = [getattr(settings, 'BASE_DATA_DIR', None), getattr(settings, 'APPROVED_EXPORT_ROOT', None), *extra]
        if not path or not roots[0] or any(not isinstance(v, (str, Path)) or not v for v in roots):
            return True
        candidate = Path(path)
        if not candidate.is_absolute():
            return True
        candidate = candidate.resolve()
        for value in roots:
            root = Path(value)
            if not root.is_absolute():
                return True
            root = root.resolve()
            if candidate == root or root in candidate.parents:
                return True
            if storage_root and candidate in root.parents:
                return True
        return False
    except (OSError, RuntimeError, ValueError, TypeError):
        return True


def has_immutable_release_boundary(project_id):
    from tasks.models import Submission

    from .approved_models import ApprovedDatasetRelease

    return (
        Submission.objects.filter(assignment__project_id=project_id).exists()
        or ApprovedDatasetRelease.objects.filter(project_id=project_id).exists()
    )
