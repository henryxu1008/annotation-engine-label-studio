"""Private resource identities; they do not themselves grant access.

Each concrete draft gets a new handle, so deleting and recreating a draft
cannot make an old URL address the replacement. This module is not routed.
"""

from django.db import IntegrityError, connection, transaction
from rest_framework.exceptions import NotFound
from tasks.models import Annotation, AnnotationDraft

from .blind_task_handles import HANDLE_BASE, HANDLE_LIMIT, _token, issue_handle, resolve_handle
from .models import BlindResourceHandle


def _unavailable():
    raise NotFound('Work is unavailable.')


def _object(assignment, kind, object_id, *, lock=False):
    from .gold_api import MAX_BYTES, bounded_query

    if kind == 'annotation':
        query = Annotation.objects.filter(pk=object_id, pk__exact=assignment.annotation_id,
                                          task_id=assignment.task_id, project_id=assignment.project_id,
                                          completed_by_id=assignment.assignee_id)
    elif kind == 'draft':
        query = AnnotationDraft.objects.filter(pk=object_id, assignment_id=assignment.pk,
                                               task_id=assignment.task_id, user_id=assignment.assignee_id)
    else:
        _unavailable()
    if lock:
        query = query.select_for_update()
    obj = bounded_query(query, 'result').first()
    if obj is None or type(obj._gold_bytes) is not int or not 0 <= obj._gold_bytes <= MAX_BYTES:
        _unavailable()
    if kind == 'draft' and obj.annotation_id not in (None, assignment.annotation_id):
        _unavailable()
    return obj


@transaction.atomic
def issue_resource_handle(*, principal, assignment_id, kind, object_id):
    if kind not in ('annotation', 'draft') or type(object_id) is not int or object_id <= 0:
        _unavailable()
    # issue_handle holds project, task and assignment locks to transaction end.
    task_record = issue_handle(principal=principal, assignment_id=assignment_id)
    assignment = resolve_handle(principal=principal, task_handle=task_record.task_handle)
    return _issue_for_locked_assignment(principal=principal, assignment=assignment, kind=kind, object_id=object_id)


def _issue_for_locked_assignment(*, principal, assignment, kind, object_id):
    """Bind a resource after the caller locked and fully authorized its assignment."""
    if not connection.in_atomic_block:
        raise RuntimeError('Resource issuance requires the caller\'s locked transaction.')
    if (kind not in ('annotation', 'draft') or type(object_id) is not int or object_id <= 0
            or assignment.assignee_id != principal.local_user_id):
        _unavailable()
    _object(assignment, kind, object_id, lock=True)
    binding = dict(assignment_id_frozen=assignment.pk, task_id_frozen=assignment.task_id,
                   project_id_frozen=assignment.project_id, actor_id_frozen=principal.local_user_id,
                   kind=kind, object_id_frozen=object_id)
    existing = BlindResourceHandle.objects.filter(kind=kind, object_id_frozen=object_id).first()
    if existing is not None:
        if any(getattr(existing, key) != value for key, value in binding.items()):
            _unavailable()
        return existing
    for _ in range(16):
        try:
            with transaction.atomic():
                return BlindResourceHandle.objects.create(**binding, handle=_token())
        except IntegrityError:
            existing = BlindResourceHandle.objects.filter(kind=kind, object_id_frozen=object_id).first()
            if existing is not None:
                if any(getattr(existing, key) != value for key, value in binding.items()):
                    _unavailable()
                return existing
    _unavailable()


def resolve_resource_handle(*, principal, kind, handle):
    """Resolve for reads; writes must lock and revalidate the returned object."""
    if (kind not in ('annotation', 'draft') or type(handle) is not int
            or not HANDLE_BASE <= handle < HANDLE_LIMIT or type(principal.local_user_id) is not int):
        _unavailable()
    record = BlindResourceHandle.objects.filter(kind=kind, handle=handle,
                                                 actor_id_frozen=principal.local_user_id).first()
    if record is None:
        _unavailable()
    from .models import BlindTaskHandle

    task_record = BlindTaskHandle.objects.filter(assignment_id_frozen=record.assignment_id_frozen,
                                                 actor_id_frozen=record.actor_id_frozen).first()
    if task_record is None:
        _unavailable()
    assignment = resolve_handle(principal=principal, task_handle=task_record.task_handle)
    if (assignment.pk != record.assignment_id_frozen or assignment.task_id != record.task_id_frozen
            or assignment.project_id != record.project_id_frozen):
        _unavailable()
    return _object(assignment, kind, record.object_id_frozen)
