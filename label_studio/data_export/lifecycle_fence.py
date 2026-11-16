"""Fail closed for export effects not enrolled in lifecycle admission."""
from access_control.project_operations import lifecycle_background_fence_enabled
from rest_framework.exceptions import PermissionDenied


def require_enrolled_export_effect():
    if lifecycle_background_fence_enabled():
        raise PermissionDenied(
            'Export generation, conversion and deletion are unavailable during lifecycle admission.'
        )
