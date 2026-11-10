"""Verify bound revisions before calling the pure bbox comparator.

Callers must authorize the full cohort, use the bounded snapshot loader and
recheck authority and source state before returning any result.
"""

from core.utils.private_file import PrivateFileError
from rest_framework.exceptions import APIException
from review_batches.service import valid_source

from . import bbox, bbox_storage
from .bbox_evidence import proof_body
from .models import BboxSubmissionProof


def prepare(rows, *, project, task):
    if not 2 <= len(rows) <= bbox.MAX_INPUTS:
        raise bbox.Error('BBOX_INVALID_INPUT', 400)
    ids = [row.pk for row in rows]
    if ids != sorted(set(ids)):
        raise bbox.Error('BBOX_INVALID_INPUT', 400)
    participants = [row.submitted_by_id for row in rows]
    if (None in participants or len(set(participants)) != len(participants)
            or any(row.submitted_by_id != row.assignment.assignee_id for row in rows)):
        raise bbox.Error('BBOX_INVALID_PARTICIPANTS', 400)
    proofs = {p.submission_id: p for p in BboxSubmissionProof.objects.filter(
        submission_id__in=ids).select_related('media')}
    if len(proofs) != len(ids) or len({proof.media_id for proof in proofs.values()}) != 1:
        raise bbox.Error('BBOX_UNVERIFIED_MEDIA', 409)
    pin = proofs[ids[0]].media
    try:
        if (pin.task_id != task.pk or pin.project_id != project.pk or task.project_id != project.pk
                or pin.artifact_key != str(pin.token) or not 0 < pin.size_bytes <= bbox.MAX_IMAGE_BYTES
                or bbox.digest(task.data) != pin.task_data_hash or bbox.digest(project.label_config) != pin.config_hash):
            raise ValueError('media binding changed')
        for row in rows:
            proof = proofs[row.pk]
            snapshot = row.result_snapshot
            if (row.assignment.task_id != task.pk or not valid_source(row, project.pk)
                    or proof.assignment_id_frozen != row.assignment_id or proof.revision != row.revision
                    or proof.result_hash != row.result_hash or proof.proof_hash != bbox.digest(proof_body(row, pin))
                    or bbox.digest(snapshot['task']['data']) != pin.task_data_hash
                    or bbox.digest(snapshot['project']['label_config']) != pin.config_hash):
                raise ValueError('invalid submission evidence')
        with bbox_storage.open_verified(pin.token, pin.storage_binding, pin.sha256, pin.size_bytes) as reader:
            content = reader.read(pin.size_bytes + 1)
        identity = bbox.image_identity(content)
        if identity != {'sha256': pin.sha256, 'width': pin.width, 'height': pin.height}:
            raise ValueError('invalid media')
        return bbox.prepare(ids, [row.result_snapshot for row in rows], content)
    except (KeyError, TypeError, ValueError, OSError, PrivateFileError, APIException, bbox.Incomparable) as exc:
        raise bbox.Error('BBOX_SOURCE_UNAVAILABLE', 409) from exc
