"""Default-off internal reassignment primitive; no HTTP route is exposed.

This is not a dispatch or rollout switch. Trigger continuity, resource access,
epoch revocation, uniform delivery and score scheduling remain prerequisites.
"""

from access_control.authorization import authorization
from access_control.identity import LOCAL_IDENTITY_SOURCE, LocalIdentityProvider, get_identity_provider
from access_control.project_operations import ASSIGN, ORGANIZE_CLASSIFICATION_GOLD, lock_project_operation, require_project_operation
from django.conf import settings
from django.db import connection, transaction
from rest_framework.exceptions import ValidationError
from rest_framework.generics import get_object_or_404
from tasks.assignments import validate_assignment_target
from tasks.models import Task, TaskAssignment

from .blind_provenance import (
    account_born_after_reference, account_has_untainted_privilege_history, project_file_scope_supported,
)
from .blind_reference import checked_blind_reference
from .models import (
    BlindCaptureEpoch,
    BlindAssignmentExposure,
    BlindDispatchReceipt,
    BlindEligibilityProof,
    BlindPrivilegeHistory,
    BlindLabelExposure,
    BlindProjectFileExposure,
    BlindReferenceTextLength,
    BlindTextLengthExposure,
    BlindProvenanceEvent,
    BlindQualityAssignment,
    ClassificationGoldEvidence,
)


def _locked_project(project_id, assignee, principal):
    project, actor = lock_project_operation(project_id, principal, ASSIGN, related_user_ids=[assignee.pk])
    authorization.require(authorization.can_manage_project(principal, project), 'Project manager role is required.')
    opt_in = settings.ANNOTATION_CLASSIFICATION_BLIND_PROJECTS
    gold_projects = settings.ANNOTATION_CLASSIFICATION_GOLD_PROJECTS
    if (connection.vendor not in ('postgresql', 'sqlite') or not settings.ANNOTATION_CLASSIFICATION_BLIND_ENABLED
            or not isinstance(opt_in, dict) or opt_in.get(f'{project.organization_id}:{project.pk}') is not True
            or not settings.ANNOTATION_CLASSIFICATION_GOLD_ENABLED or not isinstance(gold_projects, dict)
            or gold_projects.get(f'{project.organization_id}:{project.pk}') is not True
            or type(get_identity_provider()) is not LocalIdentityProvider or principal.source != LOCAL_IDENTITY_SOURCE):
        raise ValidationError({'detail': 'Quality reassignment is unavailable.'})
    require_project_operation(project, ORGANIZE_CLASSIFICATION_GOLD)
    return project, actor


def _lock_admission(assignee, reference):
    from .blind_text_lengths import reference_text_lengths

    epoch = BlindCaptureEpoch.objects.select_for_update().filter(pk=1, enabled=True).first()
    history = BlindPrivilegeHistory.objects.select_for_update().filter(account_id=assignee.pk).first()
    assignee.refresh_from_db()
    source_task_id = reference.manifest['contents']['source']['task_id']
    text_lengths = reference_text_lengths(reference)
    source_references = ClassificationGoldEvidence.objects.filter(
        project_id=reference.project_id, kind='reference',
        manifest__contents__source__task_id=source_task_id,
    ).values('pk')
    if (epoch is None or history is None or not project_file_scope_supported(reference.project_id)
            or not account_has_untainted_privilege_history(account_id=assignee.pk)
            or not account_born_after_reference(account_id=assignee.pk, reference_id=reference.pk)
            or BlindAssignmentExposure.objects.filter(account_id=assignee.pk, task_id=source_task_id,
                                                       generation=epoch.generation).exists()
            or BlindProjectFileExposure.objects.filter(account_id=assignee.pk, generation=epoch.generation).exists()
            or BlindLabelExposure.objects.filter(account_id=assignee.pk, generation=epoch.generation).exists()
            or BlindTextLengthExposure.objects.filter(account_id=assignee.pk, generation=epoch.generation,
                                                      byte_length__in=[-1, *text_lengths]).exists()
            or BlindQualityAssignment.objects.filter(assignee_id=assignee.pk,
                                                      reference_id__in=source_references).exists()
            or reference.manifest['contents']['source']['submitter_id'] == assignee.pk):
        raise ValidationError({'detail': 'Quality reassignment is unavailable.'})
    return epoch, history, source_task_id


def _issue_proof(assignment, marker, reference, epoch, history, source_task_id, actor, principal):
    from .blind_text_lengths import reference_text_lengths

    history.refresh_from_db()
    birth = BlindProvenanceEvent.objects.filter(kind='account', subject_id=assignment.assignee_id).values_list(
        'sequence', flat=True).first()
    publication = BlindProvenanceEvent.objects.filter(kind='reference', subject_id=reference.pk).values_list(
        'sequence', flat=True).first()
    if (marker is None or marker.reference_id != reference.pk or marker.reference_hash != reference.manifest_hash
            or marker.assignee_id != assignment.assignee_id or marker.task_id != assignment.task_id
            or marker.project_id != assignment.project_id or history.elevated or not history.had_assignment
            or type(birth) is not int or type(publication) is not int or birth <= publication):
        raise ValidationError({'detail': 'Quality reassignment is unavailable.'})
    from review_batches.service import digest

    for length in sorted(reference_text_lengths(reference)):
        BlindReferenceTextLength.objects.get_or_create(reference_id=reference.pk, byte_length=length)

    manifest = {
        'schema': 'classification-blind-eligibility/v1', 'rule': 'local-unseen-source/v2',
        'project_id': assignment.project_id, 'task_id': assignment.task_id, 'assignment_id': assignment.pk,
        'assignee_id': assignment.assignee_id, 'reference_id': reference.pk, 'reference_hash': reference.manifest_hash,
        'capture_generation': epoch.generation, 'account_birth_sequence': birth, 'reference_sequence': publication,
        'source_task_id': source_task_id,
        'issued_by': actor.pk, 'issuer_principal': principal.principal_id, 'issuer_source': principal.source,
    }
    BlindEligibilityProof.objects.create(assignment_id=assignment.pk, manifest=manifest, manifest_hash=digest(manifest))


@transaction.atomic
def reassign_quality_task(*, task, assignee, principal):
    """Bind a fresh, certified local account to an already-cancelled quality task."""
    project, actor = _locked_project(task.project_id, assignee, principal)
    task = get_object_or_404(Task.objects.select_for_update(), pk=task.pk, project=project)
    task.project = project
    # A mixed task is bound to its original ordinary assignment. Reassigning it
    # directly would omit that binding and expose a recognisable quality task.
    # A future mixed reassignment must create a fresh, atomic mix decision.
    from .models import BlindMixDecision

    if BlindMixDecision.objects.filter(
        selected=True, chosen_id_frozen__in=BlindQualityAssignment.objects.filter(task_id=task.pk).values('assignment_id')
    ).exists():
        raise ValidationError({'detail': 'Quality reassignment is unavailable.'})
    assignments = TaskAssignment.objects.select_for_update().filter(task=task).order_by('pk')
    list(assignments.values_list('pk', flat=True))
    if assignments.filter(status__in=TaskAssignment.ACTIVE_STATUSES).exists():
        raise ValidationError({'detail': 'Quality reassignment is unavailable.'})
    marker = BlindQualityAssignment.objects.filter(task_id=task.pk, project_id=project.pk).order_by('pk').first()
    if marker is None or marker.reference_id is None:
        raise ValidationError({'detail': 'Quality reassignment is unavailable.'})
    get_object_or_404(ClassificationGoldEvidence.objects.select_for_update(), pk=marker.reference_id, project=project)
    reference = checked_blind_reference(marker=marker, project=project, task=task)
    # The rows are locked through insertion, so a concurrent role grant or epoch
    # invalidation cannot commit between this check and the assignment binding.
    epoch, history, source_task_id = _lock_admission(assignee, reference)
    validate_assignment_target(task, assignee)
    assignment = TaskAssignment.objects.create(task=task, project=project, assignee=assignee, assigned_by=actor)
    marker = BlindQualityAssignment.objects.filter(assignment_id=assignment.pk).first()
    _issue_proof(assignment, marker, reference, epoch, history, source_task_id, actor, principal)
    return assignment


@transaction.atomic
def create_quality_task(*, project_id, reference_id, expected_reference_hash, assignee, principal, key):
    """Create one frozen, private assignment atomically; not a public dispatch API."""
    import copy
    import re
    from uuid import UUID

    from . import gold_scoring
    from .gold_api import checked
    from review_batches.service import digest

    if (type(project_id) is not int or project_id <= 0 or type(reference_id) is not int or reference_id <= 0
            or not isinstance(expected_reference_hash, str)
            or not re.fullmatch(r'[0-9a-f]{64}', expected_reference_hash) or not isinstance(key, UUID)):
        raise ValidationError({'detail': 'Quality assignment is unavailable.'})
    project, actor = _locked_project(project_id, assignee, principal)
    request_hash = digest({'reference_id': reference_id, 'reference_hash': expected_reference_hash,
                           'assignee_id': assignee.pk, 'actor_id': actor.pk,
                           'principal': principal.principal_id})
    receipt = BlindDispatchReceipt.objects.select_for_update().filter(project=project, key=key).first()
    if receipt is not None:
        if (receipt.request_hash != request_hash or receipt.actor_id != actor.pk
                or receipt.assignment_id is None or receipt.assignment_id != receipt.assignment_id_frozen):
            raise ValidationError({'detail': 'Quality assignment is unavailable.'})
        return receipt.assignment
    get_object_or_404(ClassificationGoldEvidence.objects.select_for_update(), pk=reference_id, project=project)
    reference = checked(project, reference_id, 'reference')
    contents = reference.manifest['contents']
    source = contents.get('source')
    if (reference.manifest_hash != expected_reference_hash
            or set(contents) != {'source', 'config', 'task_data', 'answer'} or not isinstance(source, dict)
            or type(source.get('task_id')) is not int or source['task_id'] <= 0
            or type(source.get('submitter_id')) is not int or source['submitter_id'] <= 0
            or contents.get('config') != project.label_config or not isinstance(contents.get('task_data'), dict)
            or not isinstance(contents.get('answer'), list)
            or gold_scoring.score(contents['config'], contents['task_data'], contents['answer'],
                                  contents['answer'])['status'] != 'scored'):
        raise ValidationError({'detail': 'Quality assignment is unavailable.'})
    epoch, history, source_task_id = _lock_admission(assignee, reference)
    task = Task(project=project, data=copy.deepcopy(contents['task_data']))
    # The immutable purpose marker is inserted later in this transaction. Do
    # not let the task-created signal add it to ordinary summary counters first.
    task._blind_quality_creation = True
    task.save()
    del task._blind_quality_creation
    validate_assignment_target(task, assignee)
    assignment = TaskAssignment.objects.create(task=task, project=project, assignee=assignee, assigned_by=actor)
    marker = BlindQualityAssignment.objects.create(
        assignment_id=assignment.pk, task_id=task.pk, project_id=project.pk, assignee_id=assignee.pk,
        reference_id=reference.pk, reference_hash=reference.manifest_hash,
    )
    checked_blind_reference(marker=marker, project=project, task=task)
    _issue_proof(assignment, marker, reference, epoch, history, source_task_id, actor, principal)
    BlindDispatchReceipt.objects.create(project=project, assignment=assignment, key=key,
                                        assignment_id_frozen=assignment.pk, request_hash=request_hash, actor_id=actor.pk)
    return assignment
