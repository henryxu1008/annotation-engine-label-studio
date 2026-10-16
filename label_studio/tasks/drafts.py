"""Optimistic draft concurrency, serialized with the existing assignment/summary locks."""
from projects.models import ProjectSummary
from tasks.exceptions import DraftConflictError
from tasks.models import AnnotationDraft


def lock_drafts(task):
    # AnnotationDraft.save/delete and annotation review take summary before draft.
    ProjectSummary.objects.select_for_update().filter(project_id=task.project_id).first()
    return AnnotationDraft.objects.select_for_update().filter(task=task)


def require_draft_revision(request, draft):
    revision = request.data.get('draft_revision')
    if isinstance(revision, bool) or str(revision) != str(draft.revision):
        raise DraftConflictError()


def check_submission_draft(request, assignment, actor, annotation=None):
    drafts = lock_drafts(assignment.task).filter(assignment=assignment, user=actor, annotation=annotation)
    draft = drafts.order_by('-id').first()
    draft_id = request.data.get('draft_id')
    if draft is None:
        if draft_id:
            raise DraftConflictError()
        return None
    if str(draft_id) != str(draft.id):
        raise DraftConflictError()
    require_draft_revision(request, draft)
    return draft
