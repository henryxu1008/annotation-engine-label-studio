"""Shared single-assignment mutations; no batch dispatch endpoint is enabled."""

from access_control.authorization import authorization
from access_control.project_operations import ASSIGN, SECURITY_CANCEL, lock_project_operation
from django.db import transaction
from organizations.models import OrganizationMember
from projects.models import ProjectMember
from rest_framework.exceptions import ValidationError
from rest_framework.generics import get_object_or_404
from tasks.models import Task, TaskAssignment


def validate_assignment_target(task, assignee):
    if task.project_id is None:
        raise ValidationError({'task': 'Task must belong to a project.'})
    has_membership = (
        assignee.is_active
        and OrganizationMember.objects.filter(
            organization_id=task.project.organization_id,
            user=assignee,
            deleted_at__isnull=True,
        ).exists()
    )
    has_role = (
        task.project.created_by_id == assignee.pk
        or ProjectMember.objects.filter(
            project=task.project,
            user=assignee,
            enabled=True,
            role__in=[ProjectMember.Role.ANNOTATOR, ProjectMember.Role.MANAGER],
        ).exists()
    )
    if not has_membership or not has_role:
        raise ValidationError({'assignee': 'Assignee must be an active annotator or manager in this project.'})
    if TaskAssignment.objects.filter(
        task=task,
        assignee=assignee,
        status__in=TaskAssignment.ACTIVE_STATUSES,
    ).exists():
        raise ValidationError({'assignee': 'This user already has an active assignment for the task.'})


@transaction.atomic
def create_assignment(*, task, assignee, principal):
    project, actor = lock_project_operation(
        task.project_id,
        principal,
        ASSIGN,
        related_user_ids=[assignee.pk],
    )
    authorization.require(authorization.can_manage_project(principal, project), 'Project manager role is required.')
    task = get_object_or_404(Task.objects.select_for_update(), pk=task.pk, project=project)
    task.project = project
    # The serializer's preflight is not authoritative: revalidate the locked
    # current target after concurrent membership/account changes commit.
    assignee.refresh_from_db()
    validate_assignment_target(task, assignee)
    return TaskAssignment.objects.create(task=task, project=project, assignee=assignee, assigned_by=actor)


@transaction.atomic
def cancel_assignment(*, assignment, principal):
    project, _ = lock_project_operation(assignment.project_id, principal, SECURITY_CANCEL)
    authorization.require(authorization.can_manage_project(principal, project), 'Project manager role is required.')
    get_object_or_404(Task.objects.select_for_update(), pk=assignment.task_id, project=project)
    assignment = get_object_or_404(TaskAssignment.objects.select_for_update(), pk=assignment.pk, project=project)
    assignment.cancel()
    return assignment
