"""Project-scoped authorization helpers for API endpoints."""

from access_control.authorization import authorization
from access_control.identity import resolve_principal
from projects.models import Project
from rest_framework import generics


def visible_projects_for_actor(actor):
    """Use the mapped actor's live project and organization membership in SQL."""
    return Project.objects.for_user(actor).filter(
        organization__organizationmember__user_id=actor.pk,
        organization__organizationmember__deleted_at__isnull=True,
    )


def require_project_manager(request, project):
    principal = resolve_principal(request)
    authorization.require(
        authorization.can_manage_project(principal, project),
        'Project manager role is required for this operation.',
    )
    return project


def get_visible_project_or_404(request, project_id):
    return generics.get_object_or_404(Project.objects.for_user(request.user), pk=project_id)


def get_managed_project_or_404(request, project_id):
    project = get_visible_project_or_404(request, project_id)
    return require_project_manager(request, project)


def managed_projects_for_user(request):
    visible = Project.objects.for_user(request.user)
    principal = resolve_principal(request)
    manager_ids = [
        project.id
        for project in visible.only('id', 'created_by_id')
        if authorization.can_manage_project(principal, project)
    ]
    return Project.objects.filter(id__in=manager_ids)
