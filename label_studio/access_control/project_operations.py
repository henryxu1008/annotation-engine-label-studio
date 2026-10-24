"""Project-first write fence and the versioned boundary for #86 operation policy.

Call inside an atomic transaction, before locking tasks/assignments/drafts.
Legacy mode preserves single-task workflows; it never enables batch dispatch
or claims that the project has an authoritative lifecycle state.
"""

from dataclasses import dataclass

from django.conf import settings
from django.db import connection
from django.utils.module_loading import import_string
from organizations.models import OrganizationMember
from projects.models import Project, ProjectMember
from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import get_object_or_404
from users.models import User

from .identity import Principal

READ_WORK = 'read_work'
EDIT = 'edit'
SUBMIT = 'submit'
REVIEW = 'review'
ASSIGN = 'assign'
ORGANIZE_WORK = 'organize_work'
ORGANIZE_REVIEW = 'organize_review'
ORGANIZE_CLASSIFICATION = 'organize_classification'
ORGANIZE_ARBITRATION = 'organize_arbitration'
ARBITRATE = 'arbitrate'
SECURITY_CANCEL = 'security_cancel'
LEGACY_OPERATIONS = frozenset({READ_WORK, EDIT, SUBMIT, REVIEW, ASSIGN})


@dataclass(frozen=True)
class ProjectOperationPolicy:
    version: str
    state: str
    allowed_operations: frozenset
    reason: str = ''

    def allows(self, operation):
        return operation in self.allowed_operations


class LegacyProjectOperationProvider:
    def evaluate(self, project):
        operations = LEGACY_OPERATIONS
        if getattr(settings, 'ANNOTATION_REVIEW_BATCHES_ENABLED', False):
            operations = operations | {ORGANIZE_REVIEW}
        if getattr(settings, 'ANNOTATION_CLASSIFICATION_BATCHES_ENABLED', False):
            operations = operations | {ORGANIZE_CLASSIFICATION}
        if getattr(settings, 'ANNOTATION_CLASSIFICATION_ARBITRATION_ENABLED', False):
            operations = operations | {ORGANIZE_ARBITRATION, ARBITRATE}
        if getattr(settings, 'ANNOTATION_FIXED_PACKAGES_ENABLED', False):
            return ProjectOperationPolicy('legacy-fixed-list-v1', 'legacy', operations | {ORGANIZE_WORK})
        return ProjectOperationPolicy('legacy-single-task-v1', 'legacy', operations)


def project_operation_policy(project):
    path = getattr(settings, 'ANNOTATION_PROJECT_OPERATION_PROVIDER', None)
    if not path:
        return ProjectOperationPolicy('unavailable-v1', 'unknown', frozenset(), 'POLICY_UNAVAILABLE')
    policy = import_string(path)().evaluate(project)
    if (
        not isinstance(policy, ProjectOperationPolicy)
        or not policy.version
        or policy.state not in {'legacy', 'draft', 'active', 'paused', 'completed', 'archived'}
        or not isinstance(policy.allowed_operations, frozenset)
    ):
        return ProjectOperationPolicy('invalid-v1', 'unknown', frozenset(), 'POLICY_UNAVAILABLE')
    return policy


def require_project_operation(project, operation):
    policy = project_operation_policy(project)
    if not policy.allows(operation):
        raise PermissionDenied(
            {
                'detail': 'This project does not permit this operation.',
                'reason': policy.reason or 'PROJECT_OPERATION_BLOCKED',
                'policy_version': policy.version,
            }
        )
    return policy


def local_principal(actor):
    """Internal services already receive a trusted persisted actor, not request fields."""
    return Principal(f'label_studio_local:{actor.pk}', 'label_studio_local', actor.email, actor.pk)


def lock_project_operation(project_id, principal, operation, *, related_user_ids=()):
    if not connection.in_atomic_block:
        raise RuntimeError('Project write fencing requires an atomic transaction')
    project = get_object_or_404(Project.objects.select_for_update(), pk=project_id)
    user_ids = sorted({principal.local_user_id, *related_user_ids} - {None})
    # Matches OrganizationMember.soft_delete(): membership before User. Lock
    # disabled/deleted rows too, then re-read eligibility while all locks hold.
    memberships = list(
        OrganizationMember.objects.select_for_update()
        .filter(
            organization_id=project.organization_id,
            user_id__in=user_ids,
        )
        .order_by('pk')
    )
    users = {user.pk: user for user in User.objects.select_for_update().filter(pk__in=user_ids).order_by('pk')}
    list(
        ProjectMember.objects.select_for_update()
        .filter(
            project=project,
            user_id__in=user_ids,
        )
        .order_by('pk')
    )
    actor = users.get(principal.local_user_id)
    if (
        actor is None
        or not actor.is_active
        or actor.active_organization_id != project.organization_id
        or not any(member.user_id == actor.pk and member.deleted_at is None for member in memberships)
    ):
        raise PermissionDenied('Current organization membership is required.')
    # Security revocation remains possible while a lifecycle policy blocks work.
    if operation != SECURITY_CANCEL:
        require_project_operation(project, operation)
    return project, actor
