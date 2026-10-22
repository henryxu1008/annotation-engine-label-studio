"""Optional navigation context intersects existing live resource authorization."""

import re

from access_control.authorization import authorization
from access_control.identity import resolve_actor
from access_control.project_operations import EDIT
from django.conf import settings
from projects.models import Project
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.generics import get_object_or_404
from tasks.models import Annotation, AnnotationDraft, Task, TaskAssignment
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
        self.policy = current_read_policy(self.project, actor, self.role, package_ids=[self.package.pk])

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

    def require_task(self, task, data=None):
        current = get_object_or_404(self.tasks(Task.objects.select_related('project')), pk=task.pk)
        authorization.require(authorization.can_view_task(self.principal, current))
        if data is None:
            return
        # A replacement assignment to the same person still permits task reads,
        # but cannot authorize the previous assignment's materialized payload.
        assignment = authorization.active_task_assignment(self.principal, current)
        expected = (assignment.pk, assignment.version) if assignment else (None, None)
        if (data.get('assignment_id'), data.get('assignment_version')) != expected:
            fail(PermissionDenied, 'PACKAGE_ACCESS_CHANGED', 'Current task assignment has changed.')
        if not authorization.can_manage_project(self.principal, current.project):
            annotation_ids = {assignment.annotation_id} if assignment and assignment.annotation_id else set()
            draft_ids = (
                set(
                    AnnotationDraft.objects.filter(task=current, user=self.actor, assignment=assignment).values_list(
                        'pk', flat=True
                    )
                )
                if assignment
                else set()
            )
            if any(row['id'] not in annotation_ids for row in data.get('annotations', [])) or any(
                row['id'] not in draft_ids for row in data.get('drafts', [])
            ):
                fail(PermissionDenied, 'PACKAGE_ACCESS_CHANGED', 'Current annotation access has changed.')
        # Read-only #86 policy may retain the task while removing edit actions.
        data['can_label'] = bool(
            data.get('can_label')
            and self.policy.allows(EDIT)
            and authorization.can_label_task(self.principal, current)
        )

    def history(self, rows):
        # Navigation references obey the same current annotation ownership as
        # direct reads. Copy rows: package filtering must not prune global history.
        annotations = Annotation.objects.filter(
            pk__in=[row['annotationId'] for row in rows],
            task__project=self.project,
        )
        if not authorization.can_manage_project(self.principal, self.project):
            annotations = annotations.filter(
                task_assignment__project=self.project,
                task_assignment__assignee=self.actor,
                task_assignment__status__in=TaskAssignment.ACTIVE_STATUSES,
            )
        allowed = set(annotations.values_list('task_id', 'pk'))
        return [
            {**row, 'annotationId': row['annotationId'] if (row['taskId'], row['annotationId']) in allowed else None}
            for row in rows
        ]

    def submissions(self, queryset):
        return queryset.filter(assignment__project=self.project, assignment__task_id__in=self.task_ids())
