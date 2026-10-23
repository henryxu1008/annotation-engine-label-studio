"""Shared fixed-package visibility predicates; context never grants authority."""

from access_control.project_operations import READ_WORK, require_project_operation
from django.db.models import Exists, F, OuterRef
from projects.models import Project, ProjectMember
from rest_framework.exceptions import PermissionDenied
from tasks.models import Submission, TaskAssignment
from tasks.my_work import work_projects

from .models import TaskPackage, TaskPackageItem


def fail(exception, reason, detail):
    error = exception({'detail': detail})
    error.display_context = {'reason': reason}
    raise error


def live_items(project):
    return TaskPackageItem.objects.filter(
        package__project=project,
        task__project=project,
        task_id=F('original_task_id'),
        task__created_at=F('original_created_at'),
    )


def review_work(project, actor, role):
    rows = Submission.objects.filter(assignment__project=project, status=Submission.Status.PENDING)
    return rows.exclude(submitted_by=actor) if role == ProjectMember.Role.REVIEWER else rows


def visible_packages(project, actor, role):
    rows = TaskPackage.objects.filter(project=project)
    if role == ProjectMember.Role.MANAGER:
        return rows.filter(project_id__in=work_projects(actor, ['manager']))
    live = live_items(project).filter(package_id=OuterRef('pk'))
    if role == ProjectMember.Role.ANNOTATOR:
        tasks = TaskAssignment.objects.filter(
            project=project,
            project_id__in=work_projects(actor, ['annotator']),
            assignee=actor,
            status__in=TaskAssignment.ACTIVE_STATUSES,
        ).values('task_id')
    else:
        tasks = (
            review_work(project, actor, role)
            .filter(
                assignment__project_id__in=work_projects(actor, ['reviewer']),
            )
            .values('assignment__task_id')
        )
    return rows.annotate(visible=Exists(live.filter(task_id__in=tasks))).filter(visible=True)


def current_read_policy(project, actor, role, *, package_ids=()):
    """Do not serialize cached role privileges after a concurrent revocation.

    Queries retain their own live scope; this final check also guards metadata
    already materialized before revocation. Avoid taking a project write lock
    for progress reads. #86 receives a freshly loaded project, not cached state.
    """
    current = Project.objects.filter(pk=project.pk, pk__in=work_projects(actor, [role])).first()
    if current is None:
        fail(PermissionDenied, 'PACKAGE_ACCESS_CHANGED', 'Current project access has changed.')
    policy = require_project_operation(current, READ_WORK)
    if package_ids:
        # One query for the entire page, including the last-assignment and last
        # pending-review cases. Metadata materialized earlier is not authority.
        visible = set(visible_packages(current, actor, role).filter(pk__in=package_ids).values_list('pk', flat=True))
        if visible != set(package_ids):
            fail(PermissionDenied, 'PACKAGE_ACCESS_CHANGED', 'Current package access has changed.')
    return policy
