"""Live checks for signed work evidence on already-fenced handle reads.

The existing blind editor uses this for fenced reads; public dispatch remains
disabled. Static facts require signature verification and immutable identities.
"""

import re

from django.db import connection
from django.db.models import Exists, Subquery
from rest_framework.exceptions import ValidationError
from review_batches.service import digest

from .blind_provenance import (
    PROJECTED_STORAGE_FLAGS,
    _exact_reference_length_checks,
    _external_scope_checks,
    _local_upload_backend_supported,
    require_capture_fence,
)
from .blind_read_attestation import read_attestation
from .blind_read_binding import FIELDS
from .models import (
    BlindCaptureCertificate,
    BlindBoundLifecycle,
    BlindGlobalScopeState,
    BlindLabelExposure,
    BlindPrivilegeHistory,
    BlindPrivilegeReadState,
    BlindProjectFileExposure,
    BlindProvenanceEvent,
    BlindReadIdentityState,
    BlindWorkExposureState,
    BlindWorkReferenceLengths,
    ClassificationGoldEvidence,
)

# Keep this in sync with the bounded database view in migration 0068.
SHARED_REFERENCE_LENGTH_LIMIT = 1024
GLOBAL_SCOPE_KEYS = frozenset(('credentials', 'model', 's3', 'gcs', 'azure', 'redis'))


def _deny():
    raise ValidationError({'detail': 'The quality admission evidence is unavailable.'})


def _read_live_guard(*, assignment, binding, checks, quality_generation=None):
    """Read and require both purposes' common facts in the branch's fenced query."""
    from tasks.models import TaskAssignment
    from users.models import User

    required = {
        'active_user': Exists(User.objects.filter(pk=assignment.assignee_id, is_active=True,
                              active_organization_id=assignment.project.organization_id)),
        'active_assignment': Exists(TaskAssignment.objects.filter(pk=assignment.pk, task_id=assignment.task_id,
            project_id=assignment.project_id, assignee_id=assignment.assignee_id,
            status__in=TaskAssignment.ACTIVE_STATUSES, task__project_id=assignment.project_id)),
        'read_identity': Exists(BlindReadIdentityState.objects.filter(assignment_id=assignment.pk, revoked=False)),
    }
    required['shared_history'] = Exists(BlindBoundLifecycle.objects.filter(binding_id=binding.pk, valid=True))
    state = BlindWorkExposureState.objects.filter(assignment_id=assignment.pk, binding_id=binding.pk,
        binding_hash=binding.binding_hash, actor_id=assignment.assignee_id)
    if binding.purpose == 'quality' and quality_generation is None:
        _deny()
    shared_lengths = BlindWorkReferenceLengths.objects.filter(assignment_id=assignment.pk).values('lengths')[:1]
    shared_scope = BlindGlobalScopeState.objects.filter(pk=1).values('facts')[:1]
    guard = (state.annotate(**required, reference_lengths=Subquery(shared_lengths),
                            global_scope=Subquery(shared_scope), **checks)
             .values(*required, 'reference_lengths', 'global_scope', 'generation', 'epoch_enabled',
        'exposure_clean', 'marker_present',
        'matching_marker_id', 'proof_present', 'proof_matches', 'source_task_key', 'source_exposed',
        'text_exposed', *checks).first())
    if guard is None or not all(guard[name] for name in required):
        _deny()
    if binding.purpose == 'quality' and (not guard['epoch_enabled'] or guard['generation'] != quality_generation):
        _deny()
    scope = guard['global_scope']
    if (not isinstance(scope, dict) or set(scope) != GLOBAL_SCOPE_KEYS
            or any(type(value) not in (bool, int) or value not in (0, 1) for value in scope.values())):
        _deny()
    return guard


def checked_attested_work(*, assignment, marker, binding, fence):
    """Check signed work and live fences while caller holds project/task/epoch locks."""
    if not connection.in_atomic_block or connection.vendor not in ('postgresql', 'sqlite'):
        _deny()
    require_capture_fence(fence)
    privilege_clean = False
    if connection.vendor == 'postgresql':
        # Shared row locks permit concurrent readers and make privilege
        # invalidation wait until the fenced response is assembled. Both work
        # purposes acquire this lock; ordinary work does not require a clean
        # quality-eligibility history.
        table = connection.ops.quote_name(BlindPrivilegeReadState._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT invalidated FROM {table} WHERE account_id = %s FOR SHARE',
                           [assignment.assignee_id])
            row = cursor.fetchone()
            privilege_clean = row is not None and not row[0]
    if (binding is None or binding.purpose not in ('ordinary', 'quality')
            or binding.assignment_id_frozen != assignment.pk or binding.task_id_frozen != assignment.task_id
            or binding.project_id_frozen != assignment.project_id or binding.actor_id_frozen != assignment.assignee_id
            or (binding.decision_id_frozen is None and (binding.source_id_frozen is not None
                or binding.source_task_id_frozen is not None or binding.decision_request_hash is not None))
            or (binding.decision_id_frozen is not None
                and not re.fullmatch(r'[0-9a-f]{64}', binding.decision_request_hash or ''))
            or (binding.purpose == 'ordinary' and (marker is not None or binding.reference_id_frozen is not None
                or binding.reference_hash is not None or binding.proof_hash is not None))
            or (binding.purpose == 'quality' and (marker is None or marker.assignment_id != assignment.pk
                or marker.task_id != assignment.task_id or marker.project_id != assignment.project_id
                or marker.assignee_id != assignment.assignee_id or marker.reference_id != binding.reference_id_frozen
                or marker.reference_hash != binding.reference_hash or type(binding.reference_id_frozen) is not int
                or binding.reference_id_frozen <= 0 or not re.fullmatch(r'[0-9a-f]{64}', binding.reference_hash or '')
                or not re.fullmatch(r'[0-9a-f]{64}', binding.proof_hash or '')))):
        _deny()
    try:
        if binding.binding_hash != digest({'schema': 'blind-mixed-read-binding/v1',
                                           **{name: getattr(binding, name) for name in FIELDS}}):
            _deny()
        signed = read_attestation(binding, payload=binding.bounded_attestation, has_row=binding.has_attestation)
        if signed is None:
            _deny()
        if binding.purpose == 'ordinary':
            guard = _read_live_guard(assignment=assignment, binding=binding, checks={})
            if guard['reference_lengths'] != [] or guard['marker_present'] or guard['proof_present']:
                _deny()
            return
        if connection.vendor == 'postgresql' and not privilege_clean:
            _deny()
        facts = signed['quality']
        value, lengths = facts['proof'], facts['text_lengths']
        generation = value['capture_generation']
        rule = value['rule']
        if (digest(value) != binding.proof_hash or type(generation) is not int or generation <= 0
                or rule not in ('local-first-assignment/v1', 'local-unseen-source/v2')
                or type(value['account_birth_sequence']) is not int or type(value['reference_sequence']) is not int
                or value['account_birth_sequence'] <= value['reference_sequence']
                or value['issuer_source'] != 'label_studio_local'
                or type(value['issued_by']) is not int or value['issued_by'] <= 0
                or value['issuer_principal'] != f"label_studio_local:{value['issued_by']}"
                or digest(assignment.project.label_config) != facts['config_hash']
                or digest(assignment.task.data) != facts['task_data_hash']
                or not _local_upload_backend_supported()):
            _deny()
        if (rule == 'local-unseen-source/v2'
                and (value['source_task_id'] != facts['source_task_id']
                     or facts['source_task_id'] == assignment.task_id
                     or facts['source_submitter_id'] == assignment.assignee_id)):
            _deny()
    except (KeyError, TypeError, ValueError, OverflowError):
        _deny()

    # The shared lifecycle guard checks current decisions and retained mixed
    # history for both bound and legacy work.
    # Very large historical signed sets retain the original exact SQL check;
    # the shared projection itself never returns an unbounded aggregate.
    large_length_set = len(lengths) > SHARED_REFERENCE_LENGTH_LIMIT
    # Signed proof issuance validates the immutable reference and provenance;
    # migrations 0057/0061 backfill invalid history and deletion triggers
    # revoke read_identity under the caller's capture fence. Keep the live
    # proof/marker checks and all conditions not represented by that state.
    # For PostgreSQL, exposure_clean uses the generation-bound flags that are
    # set by file/label exposure triggers in the same fenced transaction.
    # Signed issuance validates first assignment; history cannot be reset.
    # Source and text exposures are immutable ledgers, checked under the same
    # epoch fence. PostgreSQL writes do not update purpose-specific bindings.
    checks = {}
    if large_length_set:
        count, extra = _exact_reference_length_checks(reference_id=marker.reference_id, expected_lengths=lengths)
        checks.update(length_count=count, length_extra=extra)
    # Keep the existing SQLite source checks until its retained-evidence
    # replacement semantics have been addressed separately.
    if connection.vendor == 'sqlite':
        checks.update(
            account_event=Exists(BlindProvenanceEvent.objects.filter(kind='account', subject_id=assignment.assignee_id,
                                  sequence=value['account_birth_sequence'])),
            reference_event=Exists(BlindProvenanceEvent.objects.filter(kind='reference', subject_id=marker.reference_id,
                                    sequence=value['reference_sequence'])),
            reference=Exists(ClassificationGoldEvidence.objects.filter(pk=marker.reference_id,
                             project_id=assignment.project_id, kind='reference', manifest_hash=marker.reference_hash)),
            history=Exists(BlindPrivilegeHistory.objects.filter(account_id=assignment.assignee_id,
                                                                elevated=False, had_assignment=True)),
            certificate=Exists(BlindCaptureCertificate.objects.filter(account_id=assignment.assignee_id,
                                                                       generation=generation)),
            first_assignment=Exists(BlindPrivilegeHistory.objects.filter(account_id=assignment.assignee_id,
                                                                           first_assignment_id=assignment.pk)),
            file_exposure=Exists(BlindProjectFileExposure.objects.filter(account_id=assignment.assignee_id,
                                                                         generation=generation)),
            label_exposure=Exists(BlindLabelExposure.objects.filter(account_id=assignment.assignee_id,
                                                                   generation=generation)),
        )
    from io_storages.models import get_storage_classes

    registered = get_storage_classes('import')
    scopes = _external_scope_checks(storage_classes=registered, omit_projected=True)
    checks.update(scopes)
    guard = _read_live_guard(assignment=assignment, binding=binding, checks=checks, quality_generation=generation)
    global_scope = guard['global_scope']
    projected_flags = PROJECTED_STORAGE_FLAGS.values()
    if ((connection.vendor == 'sqlite' and (not all(
            guard[name] for name in ('account_event', 'reference_event', 'reference', 'history', 'certificate'))
            or guard['file_exposure'] or guard['label_exposure']
            or (rule == 'local-first-assignment/v1' and not guard['first_assignment'])))
            or not all(guard[name] for name in ('exposure_clean', 'proof_matches'))
            or guard['matching_marker_id'] != marker.pk
            or global_scope['credentials'] or global_scope['model']
            or any(global_scope[name] for name in projected_flags)
            or any(guard[name] for name in scopes)
            or (large_length_set and (guard['length_count'] != len(lengths) or guard['length_extra']))
            or (not large_length_set and guard['reference_lengths'] != lengths)
            or guard['text_exposed']
            or (rule == 'local-unseen-source/v2' and (guard['source_task_key'] != str(value['source_task_id'])
                or guard['source_exposed']))):
        _deny()
