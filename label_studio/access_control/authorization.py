"""Authorization service boundary.

Project/task/review policy is intentionally not implemented in the identity
foundation milestone. Protected resource checks default deny until later
milestones provide concrete policy.
"""

from rest_framework.exceptions import PermissionDenied

from .identity import Principal


class AuthorizationService:
    """Central entry point for annotation-engine authorization policy."""

    @staticmethod
    def require_authenticated(principal: Principal) -> Principal:
        return principal

    @staticmethod
    def project_role(principal: Principal, project):
        """Resolve live organization membership before project ownership/role.

        UI organization selection is not the authority for background jobs.
        """
        if principal.local_user_id is None or getattr(project, 'organization_id', None) is None:
            return None

        from organizations.models import OrganizationMember
        from projects.models import ProjectMember

        # Project ownership/role rows can outlive organization soft deletion.
        # A cached principal or residual project role is not organization access.
        if not OrganizationMember.objects.filter(
            organization_id=project.organization_id,
            user_id=principal.local_user_id,
            user__is_active=True,
            deleted_at__isnull=True,
        ).exists():
            return None

        if project.created_by_id == principal.local_user_id:
            return ProjectMember.Role.MANAGER

        membership = (
            ProjectMember.objects.filter(
                project=project,
                user_id=principal.local_user_id,
                enabled=True,
            )
            .only('role')
            .first()
        )
        return membership.role if membership else None

    @classmethod
    def can_view_project(cls, principal: Principal, project) -> bool:
        return cls.project_role(principal, project) is not None

    @classmethod
    def can_manage_project(cls, principal: Principal, project) -> bool:
        from projects.models import ProjectMember

        return cls.project_role(principal, project) == ProjectMember.Role.MANAGER

    @classmethod
    def can_review_project(cls, principal: Principal, project) -> bool:
        from projects.models import ProjectMember

        return cls.project_role(principal, project) == ProjectMember.Role.REVIEWER

    @classmethod
    def active_task_assignment(cls, principal: Principal, task):
        """Return the principal's active assignment for a task, if any."""
        if principal.local_user_id is None:
            return None

        from projects.models import ProjectMember
        from tasks.models import TaskAssignment

        role = cls.project_role(principal, task.project)
        if role not in (ProjectMember.Role.ANNOTATOR, ProjectMember.Role.MANAGER):
            return None

        return (
            TaskAssignment.objects.filter(
                task=task,
                assignee_id=principal.local_user_id,
                status__in=TaskAssignment.ACTIVE_STATUSES,
            )
            .order_by('-assigned_at', '-id')
            .first()
        )

    @classmethod
    def can_view_task(cls, principal: Principal, task) -> bool:
        if cls.can_manage_project(principal, task.project):
            return True
        return cls.active_task_assignment(principal, task) is not None

    @classmethod
    def can_manage_task(cls, principal: Principal, task) -> bool:
        return cls.can_manage_project(principal, task.project)

    @classmethod
    def can_label_task(cls, principal: Principal, task) -> bool:
        from projects.models import ProjectMember

        from .project_operations import EDIT, project_operation_policy

        role = cls.project_role(principal, task.project)
        if role not in (ProjectMember.Role.ANNOTATOR, ProjectMember.Role.MANAGER):
            return False
        if not project_operation_policy(task.project).allows(EDIT):
            return False
        return cls.active_task_assignment(principal, task) is not None

    @classmethod
    def can_view_annotation(cls, principal: Principal, annotation) -> bool:
        if annotation.task_id is None:
            return False
        if cls.can_manage_project(principal, annotation.project):
            return True
        assignment = cls.active_task_assignment(principal, annotation.task)
        return assignment is not None and assignment.annotation_id == annotation.id

    @classmethod
    def can_update_annotation(cls, principal: Principal, annotation) -> bool:
        from .project_operations import EDIT, project_operation_policy

        if annotation.task_id is None:
            return False
        if not project_operation_policy(annotation.project).allows(EDIT):
            return False
        assignment = cls.active_task_assignment(principal, annotation.task)
        return assignment is not None and assignment.annotation_id == annotation.id

    @classmethod
    def can_review_submission(cls, principal: Principal, submission) -> bool:
        from projects.models import ProjectMember

        project = submission.assignment.project
        return cls.project_role(principal, project) == ProjectMember.Role.REVIEWER

    @classmethod
    def can_release_submission(cls, principal: Principal, submission) -> bool:
        return cls.can_manage_project(principal, submission.assignment.project) and submission.is_releasable

    @staticmethod
    def require(allowed: bool, message: str = "You do not have permission to perform this action.") -> None:
        if not allowed:
            raise PermissionDenied(message)


authorization = AuthorizationService()
