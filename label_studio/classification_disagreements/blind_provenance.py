"""Forward-only ordering evidence; deliberately not an admission decision."""

from django.db import connections, transaction
from django.db.models import CharField, Exists, F, OuterRef, Q, Subquery
from django.db.models.fields.json import KeyTextTransform, KeyTransform
from django.db.models.functions import Cast

from .models import (
    BlindCaptureCertificate,
    BlindCaptureEpoch,
    BlindAssignmentExposure,
    BlindEligibilityProof,
    BlindMixDecision,
    BlindMixCompletion,
    BlindMixEvidence,
    BlindPrivilegeHistory,
    BlindLabelExposure,
    BlindProjectFileExposure,
    BlindReferenceTextLength,
    BlindTextLengthExposure,
    BlindProvenanceEvent,
    BlindQualityAssignment,
    ClassificationGoldEvidence,
)


def _local_upload_backend_supported():
    from django.conf import settings
    from django.core.files.storage import FileSystemStorage, storages

    # Label metadata exposure must commit before a handler can take project
    # locks; request-wide transactions would reverse the admission lock order.
    return (not connections['default'].settings_dict.get('ATOMIC_REQUESTS', False)
            and not settings.CLOUD_FILE_STORAGE_ENABLED and type(storages['default']) is FileSystemStorage)


def _projects_with_external_credentials(using='default'):
    from projects.models import Project

    return Project.all_objects.using(using).filter(
        (Q(task_data_login__isnull=False) & ~Q(task_data_login=''))
        | (Q(task_data_password__isnull=False) & ~Q(task_data_password=''))
    )


def project_file_scope_supported(project_id):
    """External project storage can issue links outside prospective read capture."""
    if not _local_upload_backend_supported():
        return False
    from io_storages.models import get_storage_classes
    from ml.models import MLBackend
    from projects.models import Project

    # Remote object links can be shared across projects and may bypass this
    # process after issuance. Until cross-project identity is enforced, any
    # configured remote import storage makes this capture generation unsuitable.
    # Evaluate the same global fences in one database snapshot and round trip.
    # The project anchor also fails closed when the requested scope disappeared.
    checks = {'external_credentials': Exists(_projects_with_external_credentials()),
              'external_ml': Exists(MLBackend.objects.all())}
    checks.update({f'external_storage_{index}': Exists(model.objects.all())
                   for index, model in enumerate(get_storage_classes('import'))})
    state = Project.all_objects.filter(pk=project_id).annotate(**checks).values(*checks).first()
    return state is not None and not any(state.values())


@transaction.atomic
def record_project_file_accesses(*, account_id, project_ids):
    """Record a certified account's access before granting project file bytes.

    Missing/old capture cannot become eligible, so no historical claim is made
    for accounts without a current certificate. A database failure propagates
    and the caller must not serve the file.
    """
    project_ids = set(project_ids)
    if (type(account_id) is not int or account_id <= 0
            or any(type(project_id) is not int or project_id <= 0 for project_id in project_ids)):
        raise ValueError('Invalid project file access scope.')
    if connections['default'].vendor not in ('postgresql', 'sqlite'):
        return
    epoch = BlindCaptureEpoch.objects.select_for_update().filter(pk=1, enabled=True).first()
    if epoch is None or not BlindCaptureCertificate.objects.filter(
            account_id=account_id, generation=epoch.generation).exists():
        return
    for project_id in sorted(project_ids):
        BlindProjectFileExposure.objects.get_or_create(
            account_id=account_id, project_id=project_id, generation=epoch.generation,
        )


def record_project_file_access(*, account_id, project_id):
    return record_project_file_accesses(account_id=account_id, project_ids=[project_id])


@transaction.atomic
def record_label_metadata_access(*, account_id):
    """Fence organization-wide label metadata before it is returned."""
    if type(account_id) is not int or account_id <= 0:
        raise ValueError('Invalid label metadata actor.')
    if connections['default'].vendor not in ('postgresql', 'sqlite'):
        return
    epoch = BlindCaptureEpoch.objects.select_for_update().filter(pk=1, enabled=True).first()
    if epoch and BlindCaptureCertificate.objects.filter(account_id=account_id, generation=epoch.generation).exists():
        BlindLabelExposure.objects.get_or_create(account_id=account_id, generation=epoch.generation)


def record_uploaded_file_access(*, account_id, upload):
    from data_import.models import FileUpload

    project_ids = FileUpload.objects.filter(file=upload.file.name).values_list('project_id', flat=True)
    return record_project_file_accesses(account_id=account_id, project_ids=project_ids)


def lock_capture_epoch_for_read(using='default'):
    """Fence exposure/revocation until the participant response is assembled.

    PostgreSQL shared row locks allow concurrent readers, but conflict with the
    exclusive epoch lock taken before exposure evidence is recorded. SQLite
    holds its read transaction until the surrounding atomic block completes.
    Call only after the project lock to preserve the admission lock order.
    """
    connection = connections[using]
    if not connection.in_atomic_block:
        raise RuntimeError('Capture read fencing requires an atomic transaction.')
    if connection.vendor == 'postgresql':
        table = connection.ops.quote_name(BlindCaptureEpoch._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT id FROM {table} WHERE id = %s FOR SHARE', [1])
            cursor.fetchone()
    elif connection.vendor == 'sqlite':
        BlindCaptureEpoch.objects.using(using).filter(pk=1).exists()


def _mixed_evidence_queries(*, account_id, assignment_id, project_id, reference_id, using='default'):
    """Build the same live-source and completion checks for lists and reads."""
    from tasks.models import Submission, TaskAssignment

    mixes = BlindMixDecision.objects.using(using).filter(chosen_id_frozen=assignment_id, selected=True)
    history = BlindMixEvidence.objects.using(using).filter(chosen_id_frozen=assignment_id)
    matching_history = BlindMixEvidence.objects.using(using).filter(
        decision_id_frozen=OuterRef('pk'), project_id_frozen=OuterRef('project_id'),
        source_id_frozen=OuterRef('source_id_frozen'), source_task_id_frozen=OuterRef('source_task_id_frozen'),
        source_assignee_id_frozen=OuterRef('source_assignee_id_frozen'), chosen_id_frozen=OuterRef('chosen_id_frozen'),
    )
    live_source = TaskAssignment.objects.using(using).filter(
        pk=OuterRef('source_assignment_id'), project_id=OuterRef('project_id'),
        assignee_id=account_id, status__in=TaskAssignment.ACTIVE_STATUSES,
        task__project_id=OuterRef('project_id'), task_id=OuterRef('source_task_id_frozen'),
    ).filter(pk=OuterRef('source_id_frozen'))
    completed_source = TaskAssignment.objects.using(using).filter(
        pk=OuterRef('source_id_frozen'), project_id=OuterRef('project_id_frozen'),
        assignee_id=account_id, status=TaskAssignment.Status.CANCELLED,
        version=OuterRef('source_version_after'), task__project_id=OuterRef('project_id_frozen'),
        task_id=OuterRef(OuterRef('source_task_id_frozen')),
    )
    completed_submission = Submission.objects.using(using).filter(
        pk=OuterRef('submission_id_frozen'), assignment_id=OuterRef('chosen_id_frozen'), submitted_by_id=account_id,
    )
    completion = BlindMixCompletion.objects.using(using).filter(
        decision_id_frozen=OuterRef('pk'), project_id_frozen=OuterRef('project_id'),
        source_id_frozen=OuterRef('source_id_frozen'), chosen_id_frozen=OuterRef('chosen_id_frozen'),
    ).filter(Exists(completed_source), Exists(completed_submission))
    valid = mixes.filter(
        project_id=project_id, chosen_assignment_id=assignment_id, reference_id_frozen=reference_id,
        source_assignee_id_frozen=account_id, source_assignment_id=F('source_id_frozen'),
    ).filter(Exists(matching_history)).filter(Exists(live_source) | Exists(completion))
    return mixes, history, valid


def _matching_text_exposures(*, account_id, generation, lengths, assignment_id, using='default'):
    """One live exposure predicate for collection and object authorization."""
    return (BlindTextLengthExposure.objects.using(using).filter(account_id=account_id, generation=generation)
            .exclude(assignment_id=assignment_id)
            .filter(Q(byte_length=-1) | Q(byte_length__in=lengths)))


def current_quality_assignment_ids(account_id, using='default'):
    """SQL prefilter for participant lists; object access must still verify the proof hash.

    This checks the live epoch and retained evidence in the executed query, so
    a stale cached user or assignment cannot keep a revoked row in a list.
    """
    if (connections[using].vendor not in ('postgresql', 'sqlite') or type(account_id) is not int
            or not _local_upload_backend_supported()):
        return BlindQualityAssignment.objects.using(using).none().values('assignment_id')
    epoch = BlindCaptureEpoch.objects.using(using).filter(pk=1, enabled=True).values('generation')[:1]
    cert = BlindCaptureCertificate.objects.using(using).filter(
        account_id=account_id, generation=Subquery(epoch),
    )
    account_event = BlindProvenanceEvent.objects.using(using).filter(
        kind='account', subject_id=account_id,
    ).annotate(sequence_text=Cast('sequence', CharField())).filter(sequence_text=OuterRef('_account_sequence'))
    reference_event = BlindProvenanceEvent.objects.using(using).filter(kind='reference').annotate(
        subject_text=Cast('subject_id', CharField()), sequence_text=Cast('sequence', CharField()),
    ).filter(subject_text=OuterRef('_reference'), sequence_text=OuterRef('_reference_sequence'))
    source_exposure = BlindAssignmentExposure.objects.using(using).filter(
        account_id=account_id, generation=Subquery(epoch),
    ).annotate(_task_text=Cast('task_id', CharField())).filter(_task_text=OuterRef('_source_task'))
    first_history = BlindPrivilegeHistory.objects.using(using).filter(
        account_id=account_id, first_assignment_id=OuterRef('assignment_id'),
    )
    source_reference = ClassificationGoldEvidence.objects.using(using).annotate(
        _task_text=Cast(KeyTextTransform('task_id', KeyTransform('source', KeyTransform('contents', 'manifest'))),
                        CharField()),
    ).filter(pk=OuterRef(OuterRef('reference_id')), _task_text=OuterRef('_source_task'))
    proofs = BlindEligibilityProof.objects.using(using).annotate(
        _assignment=Cast(KeyTextTransform('assignment_id', 'manifest'), CharField()),
        _project=Cast(KeyTextTransform('project_id', 'manifest'), CharField()),
        _task=Cast(KeyTextTransform('task_id', 'manifest'), CharField()),
        _assignee=Cast(KeyTextTransform('assignee_id', 'manifest'), CharField()),
        _reference=Cast(KeyTextTransform('reference_id', 'manifest'), CharField()),
        _reference_hash=KeyTextTransform('reference_hash', 'manifest'),
        _generation=Cast(KeyTextTransform('capture_generation', 'manifest'), CharField()),
        _account_sequence=Cast(KeyTextTransform('account_birth_sequence', 'manifest'), CharField()),
        _reference_sequence=Cast(KeyTextTransform('reference_sequence', 'manifest'), CharField()),
        _source_task=Cast(KeyTextTransform('source_task_id', 'manifest'), CharField()),
    ).filter(
        assignment_id=OuterRef('assignment_id'),
        manifest__schema='classification-blind-eligibility/v1',
        _assignment=Cast(OuterRef('assignment_id'), CharField()),
        _project=Cast(OuterRef('project_id'), CharField()), _task=Cast(OuterRef('task_id'), CharField()),
        _assignee=str(account_id), _reference=Cast(OuterRef('reference_id'), CharField()),
        _reference_hash=OuterRef('reference_hash'), _generation=Cast(Subquery(epoch), CharField()),
    ).filter(Exists(account_event), Exists(reference_event)).filter(
        (Q(manifest__rule='local-first-assignment/v1') & Exists(first_history))
        | (Q(manifest__rule='local-unseen-source/v2', _source_task__isnull=False)
           & ~Q(_source_task='') & ~Q(_source_task=Cast(OuterRef('task_id'), CharField()))
           & Exists(source_reference) & ~Exists(source_exposure))
    )
    history = BlindPrivilegeHistory.objects.using(using).filter(
        account_id=account_id, elevated=False, had_assignment=True,
    )
    reference = ClassificationGoldEvidence.objects.using(using).filter(
        pk=OuterRef('reference_id'), project_id=OuterRef('project_id'),
        kind='reference', manifest_hash=OuterRef('reference_hash'),
    )
    from projects.models import Project
    from tasks.models import Task
    from tasks.my_work import JSONContentEqual

    mixes, mix_history, valid_mix = _mixed_evidence_queries(
        account_id=account_id, assignment_id=OuterRef('assignment_id'), project_id=OuterRef('project_id'),
        reference_id=OuterRef('reference_id'), using=using,
    )
    file_exposure = BlindProjectFileExposure.objects.using(using).filter(
        account_id=account_id, generation=Subquery(epoch),
    )
    label_exposure = BlindLabelExposure.objects.using(using).filter(
        account_id=account_id, generation=Subquery(epoch),
    )
    reference_lengths = BlindReferenceTextLength.objects.using(using).filter(
        reference_id=OuterRef(OuterRef('reference_id')),
    ).values('byte_length')
    text_exposure = _matching_text_exposures(
        account_id=account_id, generation=Subquery(epoch), lengths=reference_lengths,
        assignment_id=OuterRef('assignment_id'), using=using,
    )
    has_reference_lengths = BlindReferenceTextLength.objects.using(using).filter(reference_id=OuterRef('reference_id'))
    # A stale pinned config or task payload must disappear from collection
    # queries as well as object reads; otherwise a single stale sample turns an
    # entire work-list response into an observable error.
    pinned_config = reference.annotate(
        value=KeyTextTransform('config', KeyTransform('contents', 'manifest')),
    ).values('value')[:1]
    pinned_data = reference.annotate(
        value=KeyTransform('task_data', KeyTransform('contents', 'manifest')),
    ).values('value')[:1]
    current_project = Project.all_objects.using(using).filter(pk=OuterRef('project_id'))
    current_task = Task.objects.using(using).filter(pk=OuterRef('task_id'), project_id=OuterRef('project_id'))
    result = BlindQualityAssignment.objects.using(using).filter(
        assignee_id=account_id, reference_id__isnull=False,
    ).filter(Exists(cert), Exists(history), Exists(proofs), Exists(reference), ~Exists(file_exposure), ~Exists(label_exposure),
             Exists(has_reference_lengths), ~Exists(text_exposure)).filter(
                 (~Exists(mixes) & ~Exists(mix_history)) | Exists(valid_mix)).annotate(
                     _pinned_config=Subquery(pinned_config),
                     _current_config=Subquery(current_project.values('label_config')[:1]),
                     _pinned_data=Subquery(pinned_data),
                     _current_data=Subquery(current_task.values('data')[:1]),
                 ).filter(_pinned_config__isnull=False, _pinned_data__isnull=False,
                          _current_data__isnull=False, _pinned_config=F('_current_config')).annotate(
                     _matching_data=JSONContentEqual(F('_pinned_data'), F('_current_data')),
                 ).filter(_matching_data=True).values('assignment_id')
    from io_storages.models import get_storage_classes
    from ml.models import MLBackend

    for model in get_storage_classes('import'):
        result = result.filter(~Exists(model.objects.using(using).all()))
    result = result.filter(~Exists(MLBackend.objects.using(using).all()))
    result = result.filter(~Exists(_projects_with_external_credentials(using)))
    return result


def account_born_after_reference(*, account_id, reference_id, using='default'):
    """Whether database capture proves account insertion followed publication.

    This is necessary but insufficient for blind admission. It says nothing
    about later access, role history, mapped identities, or external knowledge.
    Missing or unsupported evidence denies rather than inferring from dates.
    """
    if (connections[using].vendor not in ('postgresql', 'sqlite')
            or type(account_id) is not int or account_id <= 0
            or type(reference_id) is not int or reference_id <= 0):
        return False
    events = BlindProvenanceEvent.objects.using(using)
    reference = events.filter(kind='reference', subject_id=reference_id).values('sequence')[:1]
    return events.filter(kind='account', subject_id=account_id, sequence__gt=Subquery(reference)).exists()


def account_has_untainted_privilege_history(*, account_id, using='default'):
    """Check one necessary condition; this is not an admission decision."""
    if (connections[using].vendor not in ('postgresql', 'sqlite')
            or type(account_id) is not int or account_id <= 0):
        return False
    generation = BlindCaptureEpoch.objects.using(using).filter(pk=1, enabled=True).values('generation')[:1]
    certificate = BlindCaptureCertificate.objects.using(using).filter(
        generation=Subquery(generation),
    ).values('account_id')
    return BlindPrivilegeHistory.objects.using(using).filter(
        account_id=account_id, elevated=False, account_id__in=Subquery(certificate),
    ).exists()


def account_has_never_been_assigned(*, account_id, using='default'):
    """Conservative necessary condition; not a complete blind admission check."""
    if (connections[using].vendor not in ('postgresql', 'sqlite')
            or type(account_id) is not int or account_id <= 0):
        return False
    generation = BlindCaptureEpoch.objects.using(using).filter(pk=1, enabled=True).values('generation')[:1]
    certificate = BlindCaptureCertificate.objects.using(using).filter(generation=Subquery(generation)).values('account_id')
    return BlindPrivilegeHistory.objects.using(using).filter(
        account_id=account_id, elevated=False, had_assignment=False, account_id__in=Subquery(certificate),
    ).exists()


def checked_quality_proof(*, assignment, marker, lock_epoch=True, require_current_content=True):
    """Check admission evidence; frozen-revision scoring may use historical content."""
    import re

    from rest_framework.exceptions import ValidationError
    from review_batches.service import digest
    from .gold_api import bounded_query

    def unavailable():
        raise ValidationError({'detail': 'The quality admission evidence is unavailable.'})

    if lock_epoch and not connections['default'].in_atomic_block:
        raise RuntimeError('Quality proof validation requires a transaction.')
    epochs = BlindCaptureEpoch.objects
    if lock_epoch:
        epochs = epochs.select_for_update()
    epoch = epochs.filter(pk=1, enabled=True).first()
    proof = bounded_query(BlindEligibilityProof.objects.all(), 'manifest').filter(
        assignment_id=assignment.pk).first()
    if (epoch is None or proof is None or type(proof._gold_bytes) is not int
            or not 0 <= proof._gold_bytes <= 16384):
        unavailable()
    if not project_file_scope_supported(assignment.project_id):
        unavailable()
    value = proof.manifest
    try:
        manifest_hash = digest(value)
    except (TypeError, ValueError, OverflowError):
        unavailable()
    fields = {'schema', 'rule', 'project_id', 'task_id', 'assignment_id', 'assignee_id', 'reference_id',
              'reference_hash', 'capture_generation', 'account_birth_sequence', 'reference_sequence',
              'issued_by', 'issuer_principal', 'issuer_source'}
    integer_fields = ('project_id', 'task_id', 'assignment_id', 'assignee_id', 'reference_id',
                      'capture_generation', 'account_birth_sequence', 'reference_sequence', 'issued_by')
    if not isinstance(value, dict):
        unavailable()
    rule = value.get('rule')
    if rule == 'local-unseen-source/v2':
        fields = fields | {'source_task_id'}
        integer_fields = (*integer_fields, 'source_task_id')
    if (set(value) != fields
            or any(type(value[name]) is not int or value[name] <= 0 for name in integer_fields)
            or not isinstance(value['reference_hash'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', value['reference_hash'])
            or manifest_hash != proof.manifest_hash
            or value['schema'] != 'classification-blind-eligibility/v1'
            or rule not in ('local-first-assignment/v1', 'local-unseen-source/v2')
            or value['assignment_id'] != assignment.pk or value['project_id'] != assignment.project_id
            or value['task_id'] != assignment.task_id or value['assignee_id'] != assignment.assignee_id
            or marker.assignment_id != assignment.pk or marker.project_id != assignment.project_id
            or marker.task_id != assignment.task_id or marker.assignee_id != assignment.assignee_id
            or value['reference_id'] != marker.reference_id or value['reference_hash'] != marker.reference_hash
            or value['capture_generation'] != epoch.generation or value['issuer_source'] != 'label_studio_local'
            or value['issuer_principal'] != f"label_studio_local:{value['issued_by']}"
            or value['account_birth_sequence'] <= value['reference_sequence']):
        unavailable()
    # Keep all prospective admission and exposure predicates, but evaluate
    # them together against one epoch rather than separate round trips.
    mixes, mix_history, valid_mix = _mixed_evidence_queries(
        account_id=assignment.assignee_id, assignment_id=assignment.pk, project_id=assignment.project_id,
        reference_id=marker.reference_id,
    )
    checks = {
        'has_certificate': Exists(BlindCaptureCertificate.objects.filter(
            account_id=assignment.assignee_id, generation=epoch.generation)),
        'has_history': Exists(BlindPrivilegeHistory.objects.filter(
            account_id=assignment.assignee_id, elevated=False, had_assignment=True)),
        'has_account_event': Exists(BlindProvenanceEvent.objects.filter(
            kind='account', subject_id=assignment.assignee_id, sequence=value['account_birth_sequence'])),
        'has_reference_event': Exists(BlindProvenanceEvent.objects.filter(
            kind='reference', subject_id=marker.reference_id, sequence=value['reference_sequence'])),
        'has_file_exposure': Exists(BlindProjectFileExposure.objects.filter(
            account_id=assignment.assignee_id, generation=epoch.generation)),
        'has_label_exposure': Exists(BlindLabelExposure.objects.filter(
            account_id=assignment.assignee_id, generation=epoch.generation)),
        'has_mix': Exists(mixes),
        'has_mix_history': Exists(mix_history),
        'has_valid_mix': Exists(valid_mix),
        'has_first_assignment': Exists(BlindPrivilegeHistory.objects.filter(
            account_id=assignment.assignee_id, first_assignment_id=assignment.pk)),
        'has_source_exposure': Exists(BlindAssignmentExposure.objects.filter(
            account_id=assignment.assignee_id, task_id=value.get('source_task_id'), generation=epoch.generation)),
    }
    guard = (BlindCaptureEpoch.objects.filter(pk=1, generation=epoch.generation, enabled=True)
             .annotate(**checks).values(*checks).first())
    if (guard is None or not all(guard[key] for key in (
            'has_certificate', 'has_history', 'has_account_event', 'has_reference_event'))
            or guard['has_file_exposure'] or guard['has_label_exposure']
            or guard['has_mix'] != guard['has_mix_history']
            or (guard['has_mix'] and not guard['has_valid_mix'])):
        unavailable()
    # The eligibility certificate is not authority to keep serving a task
    # after its pinned configuration or text has changed. Use the same frozen
    # reference for this check and the source-provenance rule below.
    from django.http import Http404
    from rest_framework.exceptions import APIException
    from .gold_api import checked

    try:
        reference = checked(assignment.project, marker.reference_id, 'reference')
        contents = reference.manifest['contents']
        if (reference.manifest_hash != marker.reference_hash or not isinstance(contents.get('task_data'), dict)
                or (require_current_content and (
                    contents.get('config') != assignment.project.label_config
                    or digest(contents['task_data']) != digest(assignment.task.data)))):
            unavailable()
    except (APIException, Http404, KeyError, TypeError, ValueError, OverflowError):
        unavailable()
    if rule == 'local-first-assignment/v1':
        if not guard['has_first_assignment']:
            unavailable()
    else:
        try:
            source = reference.manifest['contents']['source']
            if (reference.manifest_hash != marker.reference_hash or not isinstance(source, dict)
                    or source.get('task_id') != value['source_task_id']
                    or source.get('submitter_id') == assignment.assignee_id):
                unavailable()
        except (APIException, Http404, KeyError, TypeError):
            unavailable()
        if value['source_task_id'] == assignment.task_id or guard['has_source_exposure']:
            unavailable()
    # Later assignments can expose matching-length text regardless of which
    # admission rule originally qualified the member. Collection prefilters
    # already enforce this for both rules; object authorization must agree.
    from .blind_text_lengths import reference_text_lengths

    try:
        lengths = reference_text_lengths(reference)
    except ValidationError:
        unavailable()
    recorded = set(BlindReferenceTextLength.objects.filter(reference_id=reference.pk)
                   .values_list('byte_length', flat=True))
    if (recorded != lengths or _matching_text_exposures(
            account_id=assignment.assignee_id, generation=epoch.generation,
            lengths=lengths, assignment_id=assignment.pk,
    ).exists()):
        unavailable()
    return proof
