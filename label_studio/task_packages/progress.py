"""Batched read projections over the existing assignment/review facts."""

from django.db.models import Count, Exists, F, OuterRef, Q
from organizations.models import OrganizationMember
from projects.models import ProjectMember
from rest_framework.exceptions import ValidationError
from tasks.models import TaskAssignment
from tasks.my_work import WORK_STATES, annotate_assignment_work, work_projects
from users.models import User

from .access import fail, live_items, review_work
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
        # The API checks package visibility before/after materialization. Every
        # projection still repeats live project authority in its own SQL, while
        # personal assignment/review predicates below narrow the returned facts.
        # Re-nesting visible_packages inside each membership intersection can
        # make an empty pending count scan the same thousand-item scope repeatedly.
        scope = {
            'package__project': project,
            'package_id__in': package_ids,
            'package__project_id__in': work_projects(actor, [role]),
        }
        self.items = TaskPackageItem.objects.filter(**scope)
        self.live = live_items(project).filter(**scope)
        self._assignments = TaskAssignment.objects.filter(project=project, status__in=TaskAssignment.ACTIVE_STATUSES)
        self._pending = review_work(project, actor, role)
        if role == 'annotator':
            self._assignments = self._assignments.filter(assignee=actor)
            self._pending = self._pending.filter(
                assignment__assignee=actor, assignment__status__in=TaskAssignment.ACTIVE_STATUSES
            )
        self.assignments = self._assignments.filter(task_id__in=self.live.values('task_id'))
        self.pending = self._pending.filter(assignment__task_id__in=self.live.values('task_id'))
        self.work = annotate_assignment_work(self.assignments)
        self.blocked = (
            self._assignments.exclude(assignee_id__in=eligible_assignees(project)) if role == 'manager' else None
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

        # Intersect membership once per aggregate, not once via task IDs and
        # again via the package join. Distinct counts keep overlapping packages
        # independent without duplicating assignments or submissions.
        pending = self._pending.filter(assignment__task__package_items__in=self.live.values('pk'))
        collect(pending, 'assignment__task__package_items__package_id', pending_review=Count('pk', distinct=True))
        if self.role != 'reviewer':
            work = annotate_assignment_work(self._assignments.filter(task__package_items__in=self.live.values('pk')))
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
                self.live.exclude(task_id__in=self._assignments.values('task_id')),
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
            return self.live.exclude(task_id__in=self._assignments.values('task_id'))
        if state == 'pending_review':
            return self.items.filter(task_id__in=self.pending.values('assignment__task_id'))
        if state:
            return self.items.filter(task_id__in=self.work.filter(work_state=state).values('task_id'))
        return self.items
