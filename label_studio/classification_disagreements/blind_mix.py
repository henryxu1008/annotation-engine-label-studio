"""Internal, default-off sampling primitive; not a participant-facing API.

This persists the coin flip under the command key. It does not itself make
quality and ordinary task metadata or timing indistinguishable.
"""

import re
from secrets import randbelow
from uuid import UUID

from django.db import transaction
from rest_framework.exceptions import ValidationError
from rest_framework.generics import get_object_or_404
from review_batches.service import digest
from tasks.models import Task, TaskAssignment

from .blind_reassignment import _lock_admission, _locked_project, create_quality_task
from .gold_api import checked
from .models import (BlindAssignmentExposure, BlindCaptureEpoch, BlindMixCompletion, BlindMixDecision, BlindMixEvidence,
                     BlindQualityAssignment, ClassificationGoldEvidence)


def deferred_source_ids(account_id):
    """Never expose a selected sample's ordinary source to the same actor."""
    return BlindMixEvidence.objects.filter(source_assignee_id_frozen=account_id).values('source_id_frozen')


def release_source_after_submission(*, submission, assignment, project):
    """Release the replaced ordinary work, atomically with the quality revision."""
    decision = BlindMixDecision.objects.select_for_update().filter(
        project=project, selected=True, chosen_assignment_id=assignment.pk, chosen_id_frozen=assignment.pk,
    ).first()
    if decision is None:
        return
    if BlindMixCompletion.objects.filter(decision_id_frozen=decision.pk).exists():
        return  # Later quality revisions retain the original release evidence.
    if decision.source_assignment_id != decision.source_id_frozen:
        unavailable()
    source_scope = TaskAssignment.objects.filter(pk=decision.source_id_frozen).values('task_id').first()
    if source_scope is None:
        unavailable()
    get_object_or_404(Task.objects.select_for_update(), pk=source_scope['task_id'], project=project)
    source = get_object_or_404(TaskAssignment.objects.select_for_update(), pk=decision.source_id_frozen,
                               project=project, task_id=source_scope['task_id'])
    from task_packages.models import TaskPackageItem

    if (source.task_id != decision.source_task_id_frozen or source.assignee_id != decision.source_assignee_id_frozen
            or source.assignee_id != assignment.assignee_id or source.status not in TaskAssignment.ACTIVE_STATUSES
            or source.annotation_id is not None or source.submissions.exists() or source.drafts.exists()
            or TaskPackageItem.objects.filter(package__project=project, task_id=source.task_id,
                                              original_task_id=source.task_id).exists()
            or submission.assignment_id != assignment.pk or submission.submitted_by_id != assignment.assignee_id):
        unavailable()
    BlindMixCompletion.objects.create(
        decision_id_frozen=decision.pk, project_id_frozen=project.pk, source_id_frozen=source.pk,
        source_version_after=source.version + 1, chosen_id_frozen=assignment.pk, submission_id_frozen=submission.pk,
    )
    source.cancel()


def unavailable():
    raise ValidationError({'detail': 'Quality sampling is unavailable.'})


@transaction.atomic
def _choose_assignment(*, source, expected_version, references, ratio_basis_points, principal, key, predecessor_id=None):
    """Choose under a durable key; callers must create the source in the same transaction.

    This low-level primitive does not establish that the source was unseen. The
    supported entry point is ``create_mixed_assignment``.
    """
    if (type(expected_version) is not int or expected_version <= 0
            or type(ratio_basis_points) is not int or not 0 <= ratio_basis_points <= 10000
            or not isinstance(key, UUID) or not isinstance(references, list) or len(references) > 32
            or (ratio_basis_points > 0 and not references)):
        unavailable()
    normalized = []
    for item in references:
        if (not isinstance(item, dict) or set(item) != {'id', 'hash'}
                or type(item['id']) is not int or item['id'] <= 0
                or not isinstance(item['hash'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['hash'])):
            unavailable()
        normalized.append({'id': item['id'], 'hash': item['hash']})
    normalized.sort(key=lambda item: item['id'])
    if len({item['id'] for item in normalized}) != len(normalized):
        unavailable()
    project, actor = _locked_project(source.project_id, source.assignee, principal)
    request_hash = digest({'source_id': source.pk, 'version': expected_version, 'references': normalized,
                           'ratio_basis_points': ratio_basis_points, 'actor_id': actor.pk,
                           'principal': principal.principal_id, 'predecessor_id': predecessor_id})
    previous = BlindMixDecision.objects.select_for_update().filter(project=project, key=key).first()
    if previous is not None:
        if (previous.request_hash != request_hash or previous.actor_id != actor.pk
                or previous.source_assignment_id != previous.source_id_frozen
                or previous.source_task_id_frozen != source.task_id
                or previous.source_assignee_id_frozen != source.assignee_id
                or previous.chosen_assignment_id != previous.chosen_id_frozen):
            unavailable()
        if previous.selected:
            from .blind_provenance import current_quality_assignment_ids

            if not TaskAssignment.objects.filter(
                pk=previous.chosen_assignment_id, assignee_id=source.assignee_id,
                status__in=TaskAssignment.ACTIVE_STATUSES,
                pk__in=current_quality_assignment_ids(source.assignee_id),
            ).exists():
                unavailable()
        return previous.chosen_assignment
    if BlindMixDecision.objects.filter(project=project, source_id_frozen=source.pk).exists():
        unavailable()
    get_object_or_404(Task.objects.select_for_update(), pk=source.task_id, project=project)
    source = get_object_or_404(TaskAssignment.objects.select_for_update().select_related('assignee', 'task'),
                               pk=source.pk, project=project, task__project=project)
    if (source.status != TaskAssignment.Status.ASSIGNED or source.version != expected_version
            or source.annotation_id is not None or BlindQualityAssignment.objects.filter(task_id=source.task_id).exists()
            or source.drafts.exists() or source.submissions.exists()):
        unavailable()
    from task_packages.models import TaskPackageItem

    # Fixed packages cannot safely acquire a new task identity implicitly.
    packaged = TaskPackageItem.objects.filter(package__project=project, task_id=source.task_id,
                                              original_task_id=source.task_id).exists()
    eligible = []
    for item in normalized:
        get_object_or_404(ClassificationGoldEvidence.objects.select_for_update(), pk=item['id'], project=project)
        reference = checked(project, item['id'], 'reference')
        if reference.manifest_hash != item['hash']:
            unavailable()
        try:
            _lock_admission(source.assignee, reference)
        except (KeyError, TypeError, ValidationError):
            continue
        eligible.append(item)
    selected = bool(not packaged and eligible and ratio_basis_points and randbelow(10000) < ratio_basis_points)
    chosen, reference_id = source, None
    if selected:
        item = eligible[randbelow(len(eligible))]
        reference_id = item['id']
        chosen = create_quality_task(project_id=project.pk, reference_id=item['id'],
                                     expected_reference_hash=item['hash'], assignee=source.assignee,
                                     principal=principal, key=key)
    BlindMixDecision.objects.create(
        project=project, source_assignment=source, chosen_assignment=chosen,
        source_id_frozen=source.pk, source_task_id_frozen=source.task_id, source_assignee_id_frozen=source.assignee_id,
        chosen_id_frozen=chosen.pk, reference_id_frozen=reference_id,
        predecessor_id_frozen=predecessor_id,
        key=key, request_hash=request_hash, actor_id=actor.pk, ratio_basis_points=ratio_basis_points,
        selected=selected,
    )
    return chosen


@transaction.atomic
def create_mixed_assignment(*, task, assignee, references, ratio_basis_points, principal, key):
    """Create and sample an ordinary assignment before either result can be read."""
    return _create_mixed_assignment(task=task, assignee=assignee, references=references,
                                    ratio_basis_points=ratio_basis_points, principal=principal, key=key)


def _create_mixed_assignment(*, task, assignee, references, ratio_basis_points, principal, key, predecessor_id=None):
    from tasks.assignments import create_assignment

    project, _ = _locked_project(task.project_id, assignee, principal)
    existing = BlindMixDecision.objects.select_for_update().filter(project=project, key=key).first()
    if existing is not None:
        source = existing.source_assignment
        if source is None or source.task_id != task.pk or source.assignee_id != assignee.pk:
            unavailable()
    else:
        # The ordinary assignment must not have been visible to this account in
        # the current capture generation before it is replaced.
        epoch = BlindCaptureEpoch.objects.filter(pk=1, enabled=True).first()
        if epoch is not None and BlindAssignmentExposure.objects.filter(
                account_id=assignee.pk, task_id=task.pk, generation=epoch.generation).exists():
            unavailable()
        source = create_assignment(task=task, assignee=assignee, principal=principal)
    return _choose_assignment(source=source, expected_version=1, references=references,
                             ratio_basis_points=ratio_basis_points, principal=principal, key=key,
                             predecessor_id=predecessor_id)


@transaction.atomic
def reassign_mixed_assignment(*, previous_id, task, assignee, principal, key):
    """Replace one cancelled, unsubmitted sample using a new atomic mix slot.

    Only a new eligible member can receive the same frozen reference. A
    replacement has a fresh ordinary source and its own durable decision; the
    old task identity is never reassigned directly. One predecessor may have
    at most one successor, including across retries and concurrent commands.
    """
    if type(previous_id) is not int or previous_id <= 0 or not isinstance(key, UUID):
        unavailable()
    project, _ = _locked_project(task.project_id, assignee, principal)
    previous = get_object_or_404(BlindMixDecision.objects.select_for_update(), pk=previous_id, project=project)
    if (not previous.selected or previous.chosen_assignment_id != previous.chosen_id_frozen
            or previous.reference_id_frozen is None):
        unavailable()
    if not BlindMixEvidence.objects.filter(
        decision_id_frozen=previous.pk, project_id_frozen=project.pk, source_id_frozen=previous.source_id_frozen,
        source_task_id_frozen=previous.source_task_id_frozen,
        source_assignee_id_frozen=previous.source_assignee_id_frozen, chosen_id_frozen=previous.chosen_id_frozen,
    ).exists():
        unavailable()
    original = get_object_or_404(TaskAssignment.objects.select_for_update(), pk=previous.chosen_id_frozen,
                                 project=project, task__project=project)
    marker = BlindQualityAssignment.objects.filter(assignment_id=original.pk, task_id=original.task_id,
                                                    project_id=project.pk).first()
    if (original.status != TaskAssignment.Status.CANCELLED or original.submissions.exists() or marker is None
            or marker.reference_id != previous.reference_id_frozen or not marker.reference_hash):
        unavailable()
    successor = BlindMixDecision.objects.select_for_update().filter(predecessor_id_frozen=previous.pk).first()
    if successor is not None and (successor.project_id != project.pk or successor.key != key):
        unavailable()
    chosen = _create_mixed_assignment(
        task=task, assignee=assignee, references=[{'id': marker.reference_id, 'hash': marker.reference_hash}],
        ratio_basis_points=10000, principal=principal, key=key, predecessor_id=previous.pk,
    )
    decision = BlindMixDecision.objects.get(project=project, key=key)
    if not decision.selected or decision.predecessor_id_frozen != previous.pk or chosen.pk != decision.chosen_id_frozen:
        unavailable()
    if previous.source_assignment_id != previous.source_id_frozen:
        unavailable()
    source_scope = TaskAssignment.objects.filter(pk=previous.source_id_frozen).values('task_id').first()
    if source_scope is None:
        unavailable()
    get_object_or_404(Task.objects.select_for_update(), pk=source_scope['task_id'], project=project)
    old_source = get_object_or_404(TaskAssignment.objects.select_for_update(), pk=previous.source_id_frozen,
                                   project=project, task_id=source_scope['task_id'])
    from task_packages.models import TaskPackageItem

    if (old_source.task_id != previous.source_task_id_frozen
            or old_source.assignee_id != previous.source_assignee_id_frozen
            or old_source.assignee_id != original.assignee_id or old_source.annotation_id is not None
            or old_source.submissions.exists() or old_source.drafts.exists()
            or TaskPackageItem.objects.filter(package__project=project, task_id=old_source.task_id,
                                              original_task_id=old_source.task_id).exists()):
        unavailable()
    old_source.cancel()
    return chosen
