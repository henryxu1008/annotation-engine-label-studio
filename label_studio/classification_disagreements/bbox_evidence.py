"""Submission-time checks for a previously pinned bbox image."""

import re

from core.utils.private_file import PrivateFileError
from django.conf import settings
from django.db import connection
from rest_framework.exceptions import APIException, ValidationError

from . import bbox, bbox_storage
from .models import BboxMediaPin, BboxSubmissionProof

HEX = re.compile(r'^[0-9a-f]{64}$')


def proof_body(submission, pin):
    return {'schema': 'bbox-submission-proof/v1', 'submission_id': submission.pk,
            'assignment_id': submission.assignment_id, 'revision': submission.revision,
            'result_hash': submission.result_hash, 'media_token': str(pin.token), 'media_sha256': pin.sha256}


def bind_submission(submission, *, task, project):
    """Bind a pin to a new revision in its existing project/task transaction."""
    if not connection.in_atomic_block:
        raise RuntimeError('bbox evidence requires the submission transaction')
    pin = BboxMediaPin.objects.filter(task=task).first()
    if pin is None:
        return None
    try:
        snapshot = submission.result_snapshot
        modes = settings.ANNOTATION_BBOX_MEDIA_PROJECTS
        valid = (
            settings.ANNOTATION_BBOX_MEDIA_ENABLED and isinstance(modes, dict)
            and modes.get(f'{project.organization_id}:{project.pk}') == 'managed'
            and pin.project_id == project.pk and task.project_id == project.pk
            and submission.assignment.task_id == task.pk and submission.assignment.project_id == project.pk
            and snapshot['task']['id'] == task.pk and snapshot['project']['id'] == project.pk
            and bbox.digest(snapshot) == submission.result_hash
            and bbox.digest(snapshot['task']['data']) == pin.task_data_hash == bbox.digest(task.data)
            and bbox.digest(snapshot['project']['label_config']) == pin.config_hash == bbox.digest(project.label_config)
            and pin.artifact_key == str(pin.token)
            and bool(HEX.fullmatch(pin.sha256)) and bool(HEX.fullmatch(pin.storage_binding))
            and 0 < pin.size_bytes <= bbox.MAX_IMAGE_BYTES and 0 < pin.width * pin.height <= bbox.MAX_IMAGE_PIXELS
        )
        if not valid:
            raise ValueError('stale media binding')
        with bbox_storage.open_verified(pin.token, pin.storage_binding, pin.sha256, pin.size_bytes) as reader:
            identity = bbox.image_identity(reader.read(pin.size_bytes + 1))
        if identity != {'sha256': pin.sha256, 'width': pin.width, 'height': pin.height}:
            raise ValueError('invalid pinned media')
        return BboxSubmissionProof.objects.create(
            submission=submission, media=pin, assignment_id_frozen=submission.assignment_id,
            revision=submission.revision, result_hash=submission.result_hash,
            proof_hash=bbox.digest(proof_body(submission, pin)),
        )
    except (KeyError, TypeError, ValueError, OSError, PrivateFileError, APIException, bbox.Incomparable, bbox.Error) as exc:
        raise ValidationError({'detail': 'Pinned bbox media is unavailable or has changed.'}) from exc
