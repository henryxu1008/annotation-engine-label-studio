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
    def active_task_assignment(cls, principal: Principal, task, *, _locked_task=False, _attested_binding=None,
                               _attested_fence=None):
        """Return a live assignment; callers may reuse a task they have locked.

        The private flag requires the caller to hold the project and task locks
        until the operation completes. It never skips live role/assignment or
        quality checks. Ordinary unfenced callers retain fresh related reads.
        """
        if principal.local_user_id is None:
            return None
        if _locked_task:
            from django.db import transaction

            if not transaction.get_connection().in_atomic_block:
                raise RuntimeError('A locked task requires an open transaction.')
        elif _attested_binding is not None:
            raise RuntimeError('Attested authorization requires a fenced task.')

        from projects.models import ProjectMember
        from tasks.models import TaskAssignment
        from classification_disagreements.blind_mix import deferred_source_ids
        from classification_disagreements.models import BlindQualityAssignment
        from django.db.models import Exists, OuterRef, Subquery

        role = cls.project_role(principal, task.project)
        if role not in (ProjectMember.Role.ANNOTATOR, ProjectMember.Role.MANAGER):
            return None

        markers = BlindQualityAssignment.objects.filter(task_id=task.pk)
        matching = markers.filter(assignment_id=OuterRef('pk'))
        assignment = (
            TaskAssignment.objects.filter(
                task=task,
                assignee_id=principal.local_user_id,
                status__in=TaskAssignment.ACTIVE_STATUSES,
            )
            .exclude(pk__in=deferred_source_ids(principal.local_user_id))
            .annotate(
                _quality_task=Exists(markers),
                _quality_marker_id=Subquery(matching.values('pk')[:1]),
                _quality_project_id=Subquery(matching.values('project_id')[:1]),
                _quality_assignee_id=Subquery(matching.values('assignee_id')[:1]),
                _quality_reference_id=Subquery(matching.values('reference_id')[:1]),
                _quality_reference_hash=Subquery(matching.values('reference_hash')[:1]),
            )
            .order_by('-assigned_at', '-id')
            .first()
        )
        if assignment is None:
            return None
        if _locked_task:
            if assignment.project_id != task.project_id:
                return None
            assignment.task, assignment.project = task, task.project
        from classification_disagreements.blind_provenance import checked_quality_proof
        from rest_framework.exceptions import ValidationError

        marker = None
        if assignment._quality_task:
            if assignment._quality_marker_id is None:
                return None
            marker = BlindQualityAssignment(
                pk=assignment._quality_marker_id, assignment_id=assignment.pk, task_id=task.pk,
                project_id=assignment._quality_project_id, assignee_id=assignment._quality_assignee_id,
                reference_id=assignment._quality_reference_id, reference_hash=assignment._quality_reference_hash,
            )
        try:
            if _attested_binding is not None:
                from classification_disagreements.blind_attested_live import checked_attested_work

                checked_attested_work(assignment=assignment, marker=marker, binding=_attested_binding,
                                      fence=_attested_fence)
            elif marker is not None:
                checked_quality_proof(assignment=assignment, marker=marker, lock_epoch=False)
        except ValidationError:
            return None
        return assignment

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
        if annotation.task_id is None or annotation.project_id is None or annotation.project_id != annotation.task.project_id:
            return False
        if cls.can_manage_project(principal, annotation.project):
            return True
        assignment = cls.active_task_assignment(principal, annotation.task)
        return (assignment is not None and assignment.annotation_id == annotation.id
                and assignment.project_id == annotation.project_id
                and annotation.completed_by_id == principal.local_user_id)

    @classmethod
    def can_update_annotation(cls, principal: Principal, annotation) -> bool:
        from .project_operations import EDIT, project_operation_policy

        if annotation.task_id is None or annotation.project_id is None or annotation.project_id != annotation.task.project_id:
            return False
        if not project_operation_policy(annotation.project).allows(EDIT):
            return False
        assignment = cls.active_task_assignment(principal, annotation.task)
        return (assignment is not None and assignment.annotation_id == annotation.id
                and assignment.project_id == annotation.project_id
                and annotation.completed_by_id == principal.local_user_id)

    @classmethod
    def can_review_submission(cls, principal: Principal, submission) -> bool:
        from classification_disagreements.blind_quality_boundary import is_quality_only_submission
        from projects.models import ProjectMember

        project = submission.assignment.project
        return (not is_quality_only_submission(submission)
                and cls.project_role(principal, project) == ProjectMember.Role.REVIEWER)

    @classmethod
    def can_release_submission(cls, principal: Principal, submission) -> bool:
        return cls.can_manage_project(principal, submission.assignment.project) and submission.is_releasable

    @staticmethod
    def require(allowed: bool, message: str = "You do not have permission to perform this action.") -> None:
        if not allowed:
            raise PermissionDenied(message)


authorization = AuthorizationService()
