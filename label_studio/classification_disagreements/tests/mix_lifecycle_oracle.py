"""Pre-view mixed quality predicate, retained for migration differential tests."""

from classification_disagreements.models import (
    BlindCompletionReadState,
    BlindMixCompletion,
    BlindMixDecision,
    BlindMixEvidence,
)
from django.db.models import Exists, F, OuterRef
from tasks.models import Submission, TaskAssignment


def legacy_valid_mix(*, account_id, assignment_id, project_id, reference_id):
    mixes = BlindMixDecision.objects.filter(chosen_id_frozen=assignment_id, selected=True)
    history = BlindMixEvidence.objects.filter(
        decision_id_frozen=OuterRef('pk'), project_id_frozen=OuterRef('project_id'),
        source_id_frozen=OuterRef('source_id_frozen'), source_task_id_frozen=OuterRef('source_task_id_frozen'),
        source_assignee_id_frozen=OuterRef('source_assignee_id_frozen'), chosen_id_frozen=OuterRef('chosen_id_frozen'),
    )
    active = TaskAssignment.objects.filter(pk=OuterRef('source_assignment_id'), project_id=OuterRef('project_id'),
        assignee_id=account_id, status__in=TaskAssignment.ACTIVE_STATUSES,
        task__project_id=OuterRef('project_id'), task_id=OuterRef('source_task_id_frozen')
    ).filter(pk=OuterRef('source_id_frozen'))
    released = TaskAssignment.objects.filter(pk=OuterRef('source_id_frozen'), project_id=OuterRef('project_id_frozen'),
        assignee_id=account_id, status=TaskAssignment.Status.CANCELLED, version=OuterRef('source_version_after'),
        task__project_id=OuterRef('project_id_frozen'), task_id=OuterRef(OuterRef('source_task_id_frozen')))
    submission = Submission.objects.filter(pk=OuterRef('submission_id_frozen'),
        assignment_id=OuterRef('chosen_id_frozen'), submitted_by_id=account_id)
    state = BlindCompletionReadState.objects.filter(submission_id=OuterRef('submission_id_frozen'), revoked=False)
    completion = BlindMixCompletion.objects.filter(decision_id_frozen=OuterRef('pk'),
        project_id_frozen=OuterRef('project_id'), source_id_frozen=OuterRef('source_id_frozen'),
        chosen_id_frozen=OuterRef('chosen_id_frozen')
    ).filter(Exists(released), Exists(submission), Exists(state))
    return mixes.filter(project_id=project_id, chosen_assignment_id=assignment_id, reference_id_frozen=reference_id,
        source_assignee_id_frozen=account_id, source_assignment_id=F('source_id_frozen')
    ).filter(Exists(history)).filter(Exists(active) | Exists(completion))
