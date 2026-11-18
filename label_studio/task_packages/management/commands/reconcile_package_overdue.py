"""Bounded, resumable project-local generation of overdue in-app notices."""

from access_control.authorization import authorization
from access_control.project_operations import READ_WORK, local_principal, lock_project_operation
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from task_packages.api import overdue_counts, overdue_source
from task_packages.models import TaskPackageOverdueNotice, TaskPackagePool
from task_packages.pools import postgres_required


class Command(BaseCommand):
    help = 'Reconcile a bounded page of overdue package notices; repeat using next_after until exhausted.'

    def add_arguments(self, parser):
        parser.add_argument('--after', type=int, default=0)
        parser.add_argument('--limit', type=int, default=100)

    def handle(self, *args, **options):
        if not settings.ANNOTATION_FIXED_PACKAGES_ENABLED or not settings.ANNOTATION_PACKAGE_CLAIM_ENABLED:
            raise CommandError('Package claim features must be enabled.')
        after, limit = options['after'], options['limit']
        if after < 0 or not 1 <= limit <= 500:
            raise CommandError('Expected a nonnegative cursor and a limit from 1 to 500.')
        postgres_required()
        rows = list(
            TaskPackagePool.objects.filter(
                Q(claim__isnull=False) | Q(allocation__isnull=False), pk__gt=after, due_at__lte=timezone.now()
            )
            .values_list('pk', 'package__project_id', 'claim__actor_id', 'allocation__assignee_id')
            .order_by('pk')[: limit + 1]
        )
        for pool_id, project_id, claim_actor_id, allocation_actor_id in rows[:limit]:
            actor_id = claim_actor_id or allocation_actor_id
            if actor_id is None:
                continue
            from users.models import User

            actor = User.objects.filter(pk=actor_id, is_active=True).first()
            if actor is None:
                continue
            principal = local_principal(actor)
            try:
                with transaction.atomic():
                    project, actor = lock_project_operation(project_id, principal, READ_WORK)
                    if authorization.project_role(principal, project) not in ('annotator', 'manager'):
                        continue
                    pool = (
                        TaskPackagePool.objects.filter(
                            Q(claim__actor=actor) | Q(claim__isnull=True, allocation__assignee=actor),
                            pk=pool_id, package__project=project, due_at__lte=timezone.now()
                        )
                        .select_related('claim', 'allocation', 'package')
                        .first()
                    )
                    if pool is None:
                        continue
                    count = overdue_counts([pool], project).get(pool.pk, 0)
                    source = overdue_source(pool)
                    if count and source and source[0] == actor.pk:
                        TaskPackageOverdueNotice.objects.get_or_create(
                            pool=pool,
                            **source[2],
                            due_at=pool.due_at,
                            defaults={'recipient': actor, 'overdue_count': count},
                        )
            except (PermissionDenied, Http404):
                continue
        self.stdout.write(f'next_after={rows[limit - 1][0] if len(rows) > limit else ""}')
