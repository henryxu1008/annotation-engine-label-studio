"""Internal exact-revision classification scoring; no worker or public API."""

from django.db import connection, transaction
from rest_framework.exceptions import ValidationError
from rest_framework.generics import get_object_or_404
from review_batches.service import digest
from tasks.models import Submission, Task, TaskAssignment
from tasks.submissions import _snapshot_hash

from . import gold_scoring
from .gold_api import MAX_BYTES, bounded_query, checked
from .models import BlindGoldScore, BlindQualityAssignment, ClassificationGoldEvidence

SCHEMA = 'classification-blind-score/v1'


def unavailable():
    raise ValidationError({'detail': 'The quality revision cannot be scored.'})


@transaction.atomic
def score_quality_revision(submission_id):
    """Idempotently score one locked, hash-checked quality submission snapshot."""
    if connection.vendor not in ('postgresql', 'sqlite') or type(submission_id) is not int or submission_id <= 0:
        unavailable()
    scope = Submission.objects.filter(pk=submission_id).values_list(
        'assignment__project_id', 'assignment__task_id', 'assignment_id',
    ).first()
    if not scope:
        unavailable()
    project_id, task_id, assignment_id = scope
    from projects.models import Project

    project = get_object_or_404(Project.objects.select_for_update(), pk=project_id)
    task = get_object_or_404(Task.objects.select_for_update(), pk=task_id, project=project)
    assignment = get_object_or_404(
        TaskAssignment.objects.select_for_update(), pk=assignment_id, project=project, task=task,
    )
    row = get_object_or_404(bounded_query(Submission.objects.select_for_update(), 'result_snapshot'),
                            pk=submission_id, assignment=assignment)
    marker = BlindQualityAssignment.objects.filter(assignment_id=assignment.pk).first()
    if (marker is None or marker.project_id != project.pk or marker.task_id != task.pk
            or marker.assignee_id != assignment.assignee_id or row.submitted_by_id != marker.assignee_id
            or row.status not in (Submission.Status.PENDING, Submission.Status.SUPERSEDED)
            or marker.reference_id is None or not marker.reference_hash
            or type(row._gold_bytes) is not int or not 0 <= row._gold_bytes <= MAX_BYTES):
        unavailable()
    from .blind_provenance import checked_quality_proof

    # Scoring uses the immutable submission and reference snapshots below,
    # even if the live editor config or task changes after submission.
    checked_quality_proof(assignment=assignment, marker=marker, require_current_content=False)
    get_object_or_404(ClassificationGoldEvidence.objects.select_for_update(), pk=marker.reference_id, project=project)
    reference = checked(project, marker.reference_id, 'reference')
    if reference.manifest_hash != marker.reference_hash:
        unavailable()
    contents = reference.manifest['contents']
    snapshot = row.result_snapshot
    try:
        source = contents['source']
        annotation = snapshot['annotation']
        if (set(contents) != {'source', 'config', 'task_data', 'answer'} or not isinstance(source, dict)
                or type(source.get('task_id')) is not int or source['task_id'] <= 0 or source['task_id'] == task.pk
                or type(source.get('submitter_id')) is not int or source['submitter_id'] <= 0
                or source['submitter_id'] == marker.assignee_id or not isinstance(contents['config'], str)
                or not isinstance(contents['task_data'], dict) or not isinstance(contents['answer'], list)
                or not isinstance(snapshot, dict) or not isinstance(annotation, dict)
                or snapshot['project']['id'] != project.pk or snapshot['task']['id'] != task.pk
                or snapshot['project']['label_config'] != contents['config']
                or digest(snapshot['task']['data']) != digest(contents['task_data'])
                or annotation.get('was_cancelled') is not False or not isinstance(annotation.get('result'), list)
                or _snapshot_hash(snapshot) != row.result_hash):
            unavailable()
        result = gold_scoring.score(contents['config'], contents['task_data'], contents['answer'], annotation['result'])
    except (KeyError, TypeError, ValueError, AttributeError):
        unavailable()
    if result['status'] != 'scored':
        unavailable()
    manifest = {
        'schema': SCHEMA, 'project_id': project.pk, 'task_id': task.pk, 'assignment_id': assignment.pk,
        'assignee_id': marker.assignee_id, 'submission_id': row.pk, 'revision': row.revision,
        'submission_hash': row.result_hash, 'reference_id': reference.pk, 'reference_hash': reference.manifest_hash,
        'rule': gold_scoring.RULE, 'result': result,
    }
    manifest_hash = digest(manifest)
    existing = BlindGoldScore.objects.filter(submission=row).first()
    if existing:
        if existing.manifest_hash != manifest_hash or existing.manifest != manifest:
            unavailable()
        return existing
    return BlindGoldScore.objects.create(submission=row, manifest=manifest, manifest_hash=manifest_hash)
