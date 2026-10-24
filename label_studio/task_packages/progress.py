"""Batched read projections over the existing assignment/review facts."""

from django.db.models import Count, Exists, F, OuterRef, Q
from organizations.models import OrganizationMember
from projects.models import ProjectMember
from rest_framework.exceptions import ValidationError
from tasks.models import TaskAssignment
from tasks.my_work import WORK_STATES, annotate_assignment_work, work_projects
from users.models import User

from .access import fail, live_items, review_work, visible_packages
from .models import TaskPackageItem


def filter_states(role):
    if role == 'reviewer':
        return ['pending_review']
    states = list(WORK_STATES)
    return states + ['unassigned', 'unavailable', 'pending_review'] if role == 'manager' else states


def requested_state(request, role):
    values = request.query_params.getlist('state')
    if not values:
        return ''
    if len(values) != 1 or values[0] not in filter_states(role):
        fail(ValidationError, 'PACKAGE_INVALID_INPUT', 'A single supported work state is required.')
    return values[0]


def eligible_assignees(project):
    member = ProjectMember.objects.filter(
        project=project, user_id=OuterRef('pk'), enabled=True, role__in=['manager', 'annotator']
    )
    organization_users = OrganizationMember.objects.filter(
        organization=project.organization, deleted_at__isnull=True
    ).values('user_id')
    return (
        User.objects.filter(is_active=True, active_organization_id=project.organization_id, pk__in=organization_users)
        .annotate(project_member=Exists(member))
        .filter(Q(pk=project.created_by_id) | Q(project_member=True))
    )


def grouped_counts(rows, path, **counts):
    return rows.order_by().values(package_key=F(path)).annotate(**counts)


class PackageWork:
    """A role-scoped query adapter, never a new assignment/status authority."""

    def __init__(self, project, actor, role, package_ids):
        self.role = role
        packages = visible_packages(project, actor, role).filter(pk__in=package_ids)
        self.items = TaskPackageItem.objects.filter(package__in=packages)
        self.live = live_items(project).filter(pk__in=self.items.values('pk'))
        self.assignments = TaskAssignment.objects.filter(
            project=project, task_id__in=self.live.values('task_id'), status__in=TaskAssignment.ACTIVE_STATUSES
        )
        self.pending = review_work(project, actor, role).filter(assignment__task_id__in=self.live.values('task_id'))
        if role == 'annotator':
            self.assignments = self.assignments.filter(
                assignee=actor, project_id__in=work_projects(actor, ['annotator'])
            )
            self.pending = self.pending.filter(assignment__in=self.assignments)
        elif role == 'reviewer':
            self.pending = self.pending.filter(assignment__project_id__in=work_projects(actor, ['reviewer']))
        else:
            self.assignments = self.assignments.filter(project_id__in=work_projects(actor, ['manager']))
        self.work = annotate_assignment_work(self.assignments)
        self.blocked = (
            self.assignments.exclude(assignee_id__in=eligible_assignees(project)) if role == 'manager' else None
        )

    def summaries(self, package_ids):
        result = {}
        for package_id in package_ids:
            counts = {'pending_review': 0}
            if self.role != 'reviewer':
                counts.update({state: 0 for state in WORK_STATES}, assignments=0)
            if self.role == 'manager':
                counts.update(
                    fixed_tasks=0, available_tasks=0, unavailable_tasks=0, unassigned_tasks=0, blocked_assignments=0
                )
            result[package_id] = counts

        def collect(rows, path, **counts):
            for row in grouped_counts(rows, path, **counts):
                result[row.pop('package_key')].update(row)

        pending = self.pending.filter(assignment__task__package_items__in=self.live.values('pk'))
        collect(pending, 'assignment__task__package_items__package_id', pending_review=Count('pk', distinct=True))
        if self.role != 'reviewer':
            work = self.work.filter(task__package_items__in=self.live.values('pk'))
            collect(
                work,
                'task__package_items__package_id',
                **{state: Count('pk', filter=Q(work_state=state), distinct=True) for state in WORK_STATES},
            )
            for counts in result.values():
                counts['assignments'] = sum(counts[state] for state in WORK_STATES)
        if self.role == 'manager':
            collect(self.items, 'package_id', fixed_tasks=Count('pk'))
            collect(self.live, 'package_id', available_tasks=Count('pk'))
            collect(
                self.live.exclude(task_id__in=self.assignments.values('task_id')),
                'package_id',
                unassigned_tasks=Count('pk'),
            )
            collect(
                self.blocked.filter(task__package_items__in=self.live.values('pk')),
                'task__package_items__package_id',
                blocked_assignments=Count('pk', distinct=True),
            )
            for counts in result.values():
                counts['unavailable_tasks'] = counts['fixed_tasks'] - counts['available_tasks']
        return result

    def manager_items(self, state):
        if state == 'unavailable':
            return self.items.exclude(pk__in=self.live.values('pk'))
        if state == 'unassigned':
            return self.live.exclude(task_id__in=self.assignments.values('task_id'))
        if state == 'pending_review':
            return self.items.filter(task_id__in=self.pending.values('assignment__task_id'))
        if state:
            return self.items.filter(task_id__in=self.work.filter(work_state=state).values('task_id'))
        return self.items
