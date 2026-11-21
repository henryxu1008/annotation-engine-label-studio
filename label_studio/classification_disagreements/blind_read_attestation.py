"""Server-signed static facts; live eligibility remains authoritative."""

import re

from django.core import signing
from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError
from review_batches.service import digest

from .gold_api import MAX_BYTES, bounded_query, checked
from .models import BlindReadAttestation


SALT = 'label-studio.blind-read-attestation.v1'
_NOT_LOADED = object()


def _unavailable():
    raise NotFound('Work is unavailable.')


def _signed_row(assignment_id):
    # Bound database bytes before retrieving the signed content. Clearing defer
    # keeps the bound and the payload in the same SQL statement/snapshot.
    return (bounded_query(BlindReadAttestation.objects.all(), 'signed_payload').defer(None)
            .filter(assignment_id_frozen=assignment_id, _gold_bytes__gte=0, _gold_bytes__lte=MAX_BYTES).first())


def read_attestation(binding, *, payload=_NOT_LOADED, has_row=None):
    """Verify an attestation if present; legacy absence is not authority."""
    if payload is _NOT_LOADED:
        row = _signed_row(binding.assignment_id_frozen)
        if row is None:
            # A too-large row is also unavailable; never treat it as legacy absence.
            if BlindReadAttestation.objects.filter(assignment_id_frozen=binding.assignment_id_frozen).exists():
                _unavailable()
            return None
        payload = row.signed_payload
    elif payload is None:
        if has_row is not False:
            _unavailable()
        return None
    try:
        value = signing.Signer(salt=SALT).unsign_object(payload)
    except (signing.BadSignature, TypeError, ValueError, UnicodeError, OverflowError):
        _unavailable()
    if (not isinstance(value, dict) or set(value) != {'schema', 'assignment_id', 'binding_hash', 'purpose', 'quality'}
            or value['schema'] != 'blind-read-static/v1' or value['assignment_id'] != binding.assignment_id_frozen
            or value['binding_hash'] != binding.binding_hash or value['purpose'] != binding.purpose
            or (binding.purpose == 'ordinary' and value['quality'] is not None)
            or (binding.purpose == 'quality' and not isinstance(value['quality'], dict))):
        _unavailable()
    if binding.purpose == 'quality':
        facts = value['quality']
        keys = {'proof', 'reference_id', 'reference_hash', 'config_hash', 'task_data_hash',
                'source_task_id', 'source_submitter_id', 'text_lengths'}
        proof, lengths = facts.get('proof'), facts.get('text_lengths')
        if (set(facts) != keys or not isinstance(proof, dict)
                or facts['reference_id'] != binding.reference_id_frozen
                or facts['reference_hash'] != binding.reference_hash
                or proof.get('assignment_id') != binding.assignment_id_frozen
                or proof.get('project_id') != binding.project_id_frozen
                or proof.get('task_id') != binding.task_id_frozen
                or proof.get('assignee_id') != binding.actor_id_frozen
                or proof.get('reference_id') != binding.reference_id_frozen
                or proof.get('reference_hash') != binding.reference_hash
                or type(facts['source_task_id']) is not int or facts['source_task_id'] <= 0
                or type(facts['source_submitter_id']) is not int or facts['source_submitter_id'] <= 0
                or not isinstance(lengths, list) or not lengths
                or any(type(length) is not int or length < 0 for length in lengths)
                or lengths != sorted(set(lengths))
                or any(not isinstance(facts[name], str) or not re.fullmatch(r'[0-9a-f]{64}', facts[name])
                       for name in ('config_hash', 'task_data_hash'))):
            _unavailable()
    return value


def seal_attestation(*, binding, assignment, marker=None, proof=None):
    """Sign facts only after the caller has performed full admission checks."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('Read attestation requires an atomic transaction.')
    quality = None
    if binding.purpose == 'quality':
        from .blind_provenance import checked_quality_proof
        from .blind_text_lengths import reference_text_lengths

        if (marker is None or proof is None or marker.assignment_id != assignment.pk
                or marker.reference_id != binding.reference_id_frozen or marker.reference_hash != binding.reference_hash
                or proof.assignment_id != assignment.pk or proof.manifest_hash != binding.proof_hash):
            _unavailable()
        try:
            # Do not rely on a caller merely asserting that it checked the
            # proof; signing is the trust boundary for any future fast path.
            verified = checked_quality_proof(assignment=assignment, marker=marker, lock_epoch=False)
            if verified.pk != proof.pk or verified.manifest_hash != proof.manifest_hash:
                _unavailable()
            proof = verified
            reference = checked(assignment.project, marker.reference_id, 'reference')
            contents = reference.manifest['contents']
            source = contents['source']
            if (reference.manifest_hash != binding.reference_hash or not isinstance(source, dict)
                    or type(source.get('task_id')) is not int or type(source.get('submitter_id')) is not int):
                _unavailable()
            quality = {'proof': proof.manifest, 'reference_id': reference.pk, 'reference_hash': reference.manifest_hash,
                       'config_hash': digest(contents['config']), 'task_data_hash': digest(contents['task_data']),
                       'source_task_id': source['task_id'], 'source_submitter_id': source['submitter_id'],
                       'text_lengths': sorted(reference_text_lengths(reference))}
        except (KeyError, TypeError, ValueError, OverflowError, ValidationError):
            _unavailable()
    elif binding.purpose != 'ordinary' or marker is not None or proof is not None:
        _unavailable()
    value = {'schema': 'blind-read-static/v1', 'assignment_id': assignment.pk, 'binding_hash': binding.binding_hash,
             'purpose': binding.purpose, 'quality': quality}
    existing = read_attestation(binding)
    if existing is not None:
        if existing != value:
            _unavailable()
        return
    signed = signing.Signer(salt=SALT).sign_object(value, compress=True)
    if len(signed.encode('utf-8')) > MAX_BYTES:
        _unavailable()
    BlindReadAttestation.objects.create(assignment_id_frozen=assignment.pk, signed_payload=signed)
