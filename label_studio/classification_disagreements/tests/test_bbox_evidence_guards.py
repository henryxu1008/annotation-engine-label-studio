"""Database scope and immutability constraints for prospective bbox evidence."""

from datetime import timedelta
from uuid import uuid4

import pytest
from classification_disagreements.models import BboxMediaAttempt, BboxMediaPin, BboxSubmissionProof
from django.db import IntegrityError, transaction
from django.utils import timezone
from projects.tests.factories import ProjectFactory
from tasks.models import Submission, TaskAssignment
from tasks.tests.factories import TaskFactory


@pytest.mark.django_db
def test_evidence_is_scope_checked_and_immutable():
    project = ProjectFactory()
    other = ProjectFactory()
    task = TaskFactory(project=project)
    values = dict(task=task, project=project, token=uuid4(), actor_id_frozen=project.created_by_id,
                  field='image', source={'kind': 'upload', 'id': 1}, task_data_hash='a' * 64,
                  config_hash='b' * 64, artifact_key='pin/file', storage_binding='c' * 64,
                  sha256='d' * 64, size_bytes=1, width=1, height=1)
    BboxMediaAttempt.objects.create(token=values['token'], task=task, project=project, task_id_frozen=task.pk,
                                    project_id_frozen=project.pk, actor_id_frozen=project.created_by_id,
                                    storage_binding=values['storage_binding'], expires_at=timezone.now() + timedelta(minutes=5))
    with pytest.raises(IntegrityError), transaction.atomic():
        BboxMediaPin.objects.create(**{**values, 'project': other})
    pin = BboxMediaPin.objects.create(**values)
    with pytest.raises(IntegrityError), transaction.atomic():
        BboxMediaPin.objects.filter(pk=pin.pk).update(sha256='e' * 64)

    assignment = TaskAssignment.objects.create(task=task, project=project, assignee=project.created_by)
    submission = Submission.objects.create(assignment=assignment, revision=1, result_snapshot={}, result_hash='f' * 64)
    proof = dict(submission=submission, media=pin, assignment_id_frozen=assignment.pk, revision=1,
                 result_hash=submission.result_hash, proof_hash='1' * 64)
    with pytest.raises(IntegrityError), transaction.atomic():
        BboxSubmissionProof.objects.create(**{**proof, 'revision': 2})
    record = BboxSubmissionProof.objects.create(**proof)
    with pytest.raises(IntegrityError), transaction.atomic():
        BboxSubmissionProof.objects.filter(pk=record.pk).update(result_hash='2' * 64)
    task.delete()
    assert not BboxMediaPin.objects.filter(pk=pin.pk).exists()
    assert not BboxSubmissionProof.objects.filter(pk=record.pk).exists()
