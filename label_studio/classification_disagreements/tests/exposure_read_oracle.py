"""Pre-projection live exposure checks for differential regression tests."""

from classification_disagreements.blind_provenance import _matching_text_exposures
from classification_disagreements.models import (
    BlindAccountExposureState,
    BlindAssignmentExposure,
    BlindCaptureEpoch,
    BlindEligibilityProof,
    BlindQualityAssignment,
)


def legacy_exposure(*, assignment, binding, marker=None, signed=None):
    epoch = BlindCaptureEpoch.objects.get(pk=1)
    result = {
        'exposure_clean': BlindAccountExposureState.objects.filter(account_id=assignment.assignee_id,
            generation=epoch.generation, file_exposed=False, label_exposed=False).exists(),
        'marker_present': BlindQualityAssignment.objects.filter(task_id=assignment.task_id).exists(),
        'proof_present': BlindEligibilityProof.objects.filter(assignment_id=assignment.pk).exists(),
    }
    if binding.purpose == 'quality':
        proof, lengths = signed['quality']['proof'], signed['quality']['text_lengths']
        result.update(
            matching_marker_id=marker.pk if BlindQualityAssignment.objects.filter(pk=marker.pk,
                assignment_id=assignment.pk, task_id=assignment.task_id, project_id=assignment.project_id,
                assignee_id=assignment.assignee_id, reference_id=marker.reference_id,
                reference_hash=marker.reference_hash).exists() else None,
            proof_matches=BlindEligibilityProof.objects.filter(assignment_id=assignment.pk,
                                                               manifest_hash=binding.proof_hash).exists(),
            source_exposed=BlindAssignmentExposure.objects.filter(account_id=assignment.assignee_id,
                task_id=proof.get('source_task_id'), generation=epoch.generation).exists(),
            text_exposed=_matching_text_exposures(account_id=assignment.assignee_id, generation=epoch.generation,
                lengths=lengths, assignment_id=assignment.pk).exists(),
        )
    return result
