"""Immutable mixed-work bindings; never a substitute for live authorization."""

import re

from django.db import connection, transaction
from django.db.models import BinaryField, Exists, Func, IntegerField, OuterRef, Subquery, TextField
from django.db.models.functions import Cast, Length
from rest_framework.exceptions import NotFound, ValidationError
from review_batches.service import digest

from .models import (BlindEligibilityProof, BlindMixDecision, BlindQualityAssignment, BlindReadAttestation,
                     BlindReadBinding, BlindReadIdentityState)


FIELDS = ('assignment_id_frozen', 'task_id_frozen', 'project_id_frozen', 'actor_id_frozen', 'decision_id_frozen',
          'source_id_frozen', 'source_task_id_frozen', 'decision_request_hash', 'purpose', 'reference_id_frozen',
          'reference_hash', 'proof_hash')


def _unavailable():
    raise NotFound('Work is unavailable.')


def _seal(values):
    value_hash = digest({'schema': 'blind-mixed-read-binding/v1', **values})
    previous = BlindReadBinding.objects.filter(assignment_id_frozen=values['assignment_id_frozen']).first()
    if previous:
        if previous.binding_hash != value_hash or any(getattr(previous, key) != value for key, value in values.items()):
            _unavailable()
        return previous
    return BlindReadBinding.objects.create(**values, binding_hash=value_hash)


def seal_ordinary_read_binding(*, assignment):
    """Bind an ordinary assignment without freezing its editable task data."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('Ordinary read binding requires an atomic transaction.')
    if (BlindQualityAssignment.objects.filter(task_id=assignment.task_id).exists()
            or BlindEligibilityProof.objects.filter(assignment_id=assignment.pk).exists()
            or BlindMixDecision.objects.filter(chosen_id_frozen=assignment.pk).exists()):
        _unavailable()
    binding = _seal({
        'assignment_id_frozen': assignment.pk, 'task_id_frozen': assignment.task_id,
        'project_id_frozen': assignment.project_id, 'actor_id_frozen': assignment.assignee_id,
        'decision_id_frozen': None, 'source_id_frozen': None, 'source_task_id_frozen': None,
        'decision_request_hash': None, 'purpose': 'ordinary', 'reference_id_frozen': None,
        'reference_hash': None, 'proof_hash': None,
    })
    from .blind_read_attestation import seal_attestation

    seal_attestation(binding=binding, assignment=assignment)
    return binding


def seal_unmixed_quality_read_binding(*, assignment, marker):
    """Bind an existing quality assignment after validating its full proof."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('Quality read binding requires an atomic transaction.')
    if (marker is None or marker.assignment_id != assignment.pk or marker.task_id != assignment.task_id
            or BlindMixDecision.objects.filter(chosen_id_frozen=assignment.pk).exists()):
        _unavailable()
    from .blind_provenance import checked_quality_proof

    try:
        proof = checked_quality_proof(assignment=assignment, marker=marker, lock_epoch=False)
    except ValidationError:
        _unavailable()
    binding = _seal({
        'assignment_id_frozen': assignment.pk, 'task_id_frozen': assignment.task_id,
        'project_id_frozen': assignment.project_id, 'actor_id_frozen': assignment.assignee_id,
        'decision_id_frozen': None, 'source_id_frozen': None, 'source_task_id_frozen': None,
        'decision_request_hash': None, 'purpose': 'quality', 'reference_id_frozen': marker.reference_id,
        'reference_hash': marker.reference_hash, 'proof_hash': proof.manifest_hash,
    })
    from .blind_read_attestation import seal_attestation

    seal_attestation(binding=binding, assignment=assignment, marker=marker, proof=proof)
    return binding


def seal_mixed_read_binding(*, assignment, decision):
    """Seal validated immutable evidence before the mixed slot can commit.

    The caller holds the project and capture write fences. Quality admission
    remains fully checked here and on every live read; this record does not
    make an eligibility decision by itself.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('Mixed read binding requires an atomic transaction.')
    if (decision.chosen_id_frozen != assignment.pk or decision.chosen_assignment_id != assignment.pk
            or decision.project_id != assignment.project_id or decision.source_assignee_id_frozen != assignment.assignee_id
            or decision.source_assignment_id != decision.source_id_frozen
            or (not decision.selected and (decision.source_id_frozen != assignment.pk
                                            or decision.source_task_id_frozen != assignment.task_id))):
        _unavailable()
    marker = BlindQualityAssignment.objects.filter(task_id=assignment.task_id).first()
    proof = None
    if decision.selected:
        if marker is None or marker.assignment_id != assignment.pk:
            _unavailable()
        from .blind_provenance import checked_quality_proof

        try:
            proof = checked_quality_proof(assignment=assignment, marker=marker, lock_epoch=False)
        except ValidationError:
            _unavailable()
    elif marker is not None or BlindEligibilityProof.objects.filter(assignment_id=assignment.pk).exists():
        _unavailable()
    values = {
        'assignment_id_frozen': assignment.pk, 'task_id_frozen': assignment.task_id,
        'project_id_frozen': assignment.project_id, 'actor_id_frozen': assignment.assignee_id,
        'decision_id_frozen': decision.pk, 'source_id_frozen': decision.source_id_frozen,
        'source_task_id_frozen': decision.source_task_id_frozen, 'decision_request_hash': decision.request_hash,
        'purpose': 'quality' if decision.selected else 'ordinary',
        'reference_id_frozen': marker.reference_id if marker else None,
        'reference_hash': marker.reference_hash if marker else None,
        'proof_hash': proof.manifest_hash if proof else None,
    }
    binding = _seal(values)
    from .blind_read_attestation import seal_attestation

    seal_attestation(binding=binding, assignment=assignment, marker=marker, proof=proof)
    return binding


def load_attested_read_binding(*, assignment, fence):
    """Load bounded signed evidence; the caller must perform fenced live checks.

    Absence falls back to the legacy verifier. Oversized or malformed evidence
    fails closed and is never treated as an unsigned legacy assignment.
    """
    from .blind_read_attestation import read_attestation
    from .blind_provenance import require_capture_fence
    from .gold_api import MAX_BYTES

    require_capture_fence(fence)
    attestations = BlindReadAttestation.objects.filter(assignment_id_frozen=OuterRef('assignment_id_frozen'))
    size = (Func(Cast('signed_payload', TextField()), function='OCTET_LENGTH', output_field=IntegerField())
            if connection.vendor == 'postgresql' else Length(Cast('signed_payload', BinaryField())))
    bounded = attestations.annotate(_bytes=size).filter(_bytes__gte=0, _bytes__lte=MAX_BYTES)
    row = BlindReadBinding.objects.filter(assignment_id_frozen=assignment.pk).annotate(
        has_attestation=Exists(attestations), bounded_attestation=Subquery(bounded.values('signed_payload')[:1]),
    ).first()
    if row is None or not row.has_attestation:
        return None
    signed = read_attestation(row, payload=row.bounded_attestation, has_row=row.has_attestation)
    return row, signed


def verify_mixed_read_binding(assignment):
    """Check retained binding and current marker/proof identity in one query.

    Pre-existing non-mixed work retains its original authorization path. A
    mixed decision without a binding must be replayed by its manager before it
    is usable; a missing record must never silently become an ordinary grant.
    """
    from .gold_api import MAX_BYTES

    attestations = BlindReadAttestation.objects.filter(assignment_id_frozen=OuterRef('assignment_id_frozen'))
    size = (Func(Cast('signed_payload', TextField()), function='OCTET_LENGTH', output_field=IntegerField())
            if connection.vendor == 'postgresql' else Length(Cast('signed_payload', BinaryField())))
    bounded_attestations = attestations.annotate(_bytes=size).filter(_bytes__gte=0, _bytes__lte=MAX_BYTES)
    markers = BlindQualityAssignment.objects.filter(task_id=OuterRef('task_id_frozen'))
    matching_marker = markers.filter(
        assignment_id=OuterRef('assignment_id_frozen'), project_id=OuterRef('project_id_frozen'),
        assignee_id=OuterRef('actor_id_frozen'), reference_id=OuterRef('reference_id_frozen'),
        reference_hash=OuterRef('reference_hash'),
    )
    proofs = BlindEligibilityProof.objects.filter(assignment_id=OuterRef('assignment_id_frozen'))
    decisions = BlindMixDecision.objects.filter(
        pk=OuterRef('decision_id_frozen'), project_id=OuterRef('project_id_frozen'),
        chosen_id_frozen=OuterRef('assignment_id_frozen'), chosen_assignment_id=OuterRef('assignment_id_frozen'),
        source_id_frozen=OuterRef('source_id_frozen'), source_task_id_frozen=OuterRef('source_task_id_frozen'),
        source_assignee_id_frozen=OuterRef('actor_id_frozen'), request_hash=OuterRef('decision_request_hash'),
    )
    any_decisions = BlindMixDecision.objects.filter(chosen_id_frozen=OuterRef('assignment_id_frozen'))
    live_identity = BlindReadIdentityState.objects.filter(assignment_id=OuterRef('assignment_id_frozen'), revoked=False)
    row = BlindReadBinding.objects.filter(assignment_id_frozen=assignment.pk).annotate(
        has_marker=Exists(markers), matching_marker=Exists(matching_marker),
        has_proof=Exists(proofs), matching_proof=Exists(proofs.filter(manifest_hash=OuterRef('proof_hash'))),
        ordinary_decision=Exists(decisions.filter(selected=False, reference_id_frozen__isnull=True)),
        quality_decision=Exists(decisions.filter(selected=True, reference_id_frozen=OuterRef('reference_id_frozen'))),
        has_decision=Exists(any_decisions),
        has_attestation=Exists(attestations),
        live_identity=Exists(live_identity),
        bounded_attestation=Subquery(bounded_attestations.values('signed_payload')[:1]),
    ).first()
    if row is None:
        if BlindMixDecision.objects.filter(chosen_id_frozen=assignment.pk).exists():
            _unavailable()
        return
    values = {field: getattr(row, field) for field in FIELDS}
    if (not row.live_identity or values['assignment_id_frozen'] != assignment.pk or values['task_id_frozen'] != assignment.task_id
            or values['project_id_frozen'] != assignment.project_id or values['actor_id_frozen'] != assignment.assignee_id
            or row.binding_hash != digest({'schema': 'blind-mixed-read-binding/v1', **values})):
        _unavailable()
    mixed = row.decision_id_frozen is not None
    if mixed and not re.fullmatch(r'[0-9a-f]{64}', row.decision_request_hash or ''):
        _unavailable()
    if not mixed and (row.has_decision or row.source_id_frozen is not None
                      or row.source_task_id_frozen is not None or row.decision_request_hash is not None):
        _unavailable()
    if row.purpose == 'ordinary':
        if (row.reference_id_frozen is not None or row.reference_hash is not None or row.proof_hash is not None
                or row.has_marker or row.has_proof
                or (mixed and (not row.ordinary_decision or row.source_id_frozen != assignment.pk
                               or row.source_task_id_frozen != assignment.task_id))):
            _unavailable()
    elif row.purpose == 'quality':
        if ((mixed and not row.quality_decision) or not row.matching_marker or not row.matching_proof
                or type(row.reference_id_frozen) is not int
                or row.reference_id_frozen <= 0 or not re.fullmatch(r'[0-9a-f]{64}', row.reference_hash or '')
                or not re.fullmatch(r'[0-9a-f]{64}', row.proof_hash or '')):
            _unavailable()
    else:
        _unavailable()
    from .blind_read_attestation import read_attestation

    return row, read_attestation(row, payload=row.bounded_attestation, has_row=row.has_attestation)
