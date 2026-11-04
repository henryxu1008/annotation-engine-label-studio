"""Minimal Collector-before fences; independent of per-process feature flags.

Direct schema/SQL maintenance remains a trusted, non-concurrent operation. This
adapter coordinates the public Project/Task/User/Organization deletion roots,
including related managers and Data Manager's project unlink before its queue.
"""
from contextlib import contextmanager

from django.apps import apps
from django.db import connections, transaction
from django.db.models import Q
from rest_framework.exceptions import APIException


def available(using):
    # No cache: a historical migration can create the audit tables later on the
    # same connection. Never query future audit columns in a pre-audit schema.
    return 'data_export_auditactorbinding' in connections[using].introspection.table_names()


def project_ids(query, kind):
    from projects.models import Project
    from tasks.models import TaskAssignment

    using = query.db
    ids = query.order_by().values('pk')
    if kind == 'projects':
        return set(query.order_by().values_list('pk', flat=True))
    if kind == 'organizations':
        return set(Project.all_objects.using(using).filter(organization_id__in=ids).values_list('pk', flat=True))
    if kind == 'tasks':
        direct = set(query.order_by().values_list('project_id', flat=True))
        # DM async deletion may already have unlinked Task.project. Its trusted
        # assignment still pins the original scope; never infer a current ID.
        direct.update(TaskAssignment.objects.using(using).filter(task_id__in=ids).values_list('project_id', flat=True))
        return direct - {None}
    if kind != 'users':
        raise ValueError('Unknown audit deletion root')
    from organizations.models import OrganizationMember

    user_table = query.model._meta.db_table
    found = set()
    orgs = OrganizationMember.objects.using(using).filter(user_id__in=ids).values('organization_id')
    found.update(Project.all_objects.using(using).filter(organization_id__in=orgs).values_list('pk', flat=True))
    for model in apps.get_models():
        fields = {f.name: f for f in model._meta.fields}
        relations = [f for f in fields.values() if f.is_relation and f.related_model._meta.db_table == user_table]
        if not relations:
            continue
        if model is Project:
            column = 'pk'
        elif 'project' in fields and fields['project'].is_relation and fields['project'].related_model is Project:
            column = 'project_id'
        else:
            continue
        predicate = Q()
        for field in relations:
            predicate |= Q(**{field.attname + '__in': ids})
        found.update(model._base_manager.using(using).filter(predicate).values_list(column, flat=True))
    # First-review reviewer and submission submitter have indirect projects.
    from tasks.models import ReviewDecision, Submission

    found.update(
        Submission.objects.using(using)
        .filter(submitted_by_id__in=ids)
        .values_list('assignment__project_id', flat=True)
    )
    found.update(
        ReviewDecision.objects.using(using)
        .filter(reviewer_id__in=ids)
        .values_list('submission__assignment__project_id', flat=True)
    )
    return found - {None}


@contextmanager
def coordinated(query, kind, *, extra_projects=()):
    using = query.db
    if not available(using):
        yield query
        return
    from organizations.models import OrganizationMember
    from projects.models import Project

    with transaction.atomic(using=using):
        ids = list(query.order_by('pk').values_list('pk', flat=True))
        fixed = query.filter(pk__in=ids)
        before = project_ids(fixed, kind) | set(extra_projects)
        list(
            Project.all_objects.using(using)
            .filter(pk__in=before)
            .order_by('pk')
            .select_for_update()
            .values_list('pk', flat=True)
        )
        if kind == 'users':
            list(
                OrganizationMember.objects.using(using)
                .filter(user_id__in=ids)
                .order_by('pk')
                .select_for_update()
                .values_list('pk', flat=True)
            )
            list(
                query.model._base_manager.using(using)
                .filter(pk__in=ids)
                .order_by('pk')
                .select_for_update()
                .values_list('pk', flat=True)
            )
        if (
            set(query.order_by().values_list('pk', flat=True)) != set(ids)
            or project_ids(fixed, kind) | set(extra_projects) != before
        ):
            error = APIException('AUDIT_DELETION_SCOPE_CHANGED')
            error.status_code = 409
            raise error
        # The caller must mutate this concrete PK set. Re-evaluating the
        # original predicate after this boundary admits unlocked phantoms.
        yield fixed
