"""Optional navigation context intersects existing live resource authorization."""

import re

from access_control.authorization import authorization
from access_control.identity import resolve_actor
from django.conf import settings
from projects.models import Project
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.generics import get_object_or_404
from tasks.models import Task
from tasks.my_work import work_projects

from .access import current_read_policy, fail, live_items, visible_packages


def requested_package(request):
    values = request.query_params.getlist('task_package')
    if not values:
        return None
    if len(values) != 1 or not re.fullmatch(r'[1-9][0-9]{0,18}', values[0]) or int(values[0]) > 2**63 - 1:
        fail(ValidationError, 'PACKAGE_INVALID_INPUT', 'A single positive task_package ID is required.')
    return int(values[0])


class PackageScope:
    def __init__(self, project, principal, actor, package_id):
        if not settings.ANNOTATION_FIXED_PACKAGES_ENABLED:
            raise NotFound()
        self.project = get_object_or_404(
            Project.objects.filter(pk__in=work_projects(actor, ['manager', 'annotator', 'reviewer'])),
            pk=project.pk,
        )
        self.principal, self.actor = principal, actor
        self.role = authorization.project_role(principal, self.project)
        self.package = get_object_or_404(visible_packages(self.project, actor, self.role), pk=package_id)
        current_read_policy(self.project, actor, self.role, package_ids=[self.package.pk])

    @classmethod
    def from_request(cls, request, project):
        package_id = requested_package(request)
        return None if package_id is None else cls(project, *resolve_actor(request), package_id)

    def task_ids(self):
        return live_items(self.project).filter(package=self.package).values('task_id')

    def tasks(self, queryset):
        # Membership and assignment are independent predicates. This can only
        # narrow an already-authorized queue, including saved views/selections.
        allowed = Task.objects.for_user(self.actor).filter(project=self.project, pk__in=self.task_ids())
        return queryset.filter(project=self.project, pk__in=allowed.values('pk'))

    def require_task(self, task):
        get_object_or_404(self.tasks(Task.objects.all()), pk=task.pk)
        authorization.require(authorization.can_view_task(self.principal, task))

    def submissions(self, queryset):
        return queryset.filter(assignment__project=self.project, assignment__task_id__in=self.task_ids())
