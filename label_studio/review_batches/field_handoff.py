"""FIELD review provenance and bounded metadata. No decisions or answer copies."""
from access_control.project_operations import ORGANIZE_FIELD_ARBITRATION, project_operation_policy
from classification_disagreements import field_api, fields
from classification_disagreements import field_arbitration as a
from classification_disagreements import field_batches as b
from classification_disagreements.models import FieldArbitrationDecision, FieldArbitrationTask, FieldBatch
from django.conf import settings
from django.db import models
from django.db.models import Case, F, Sum, Value, When, Window
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import NotFound
from tasks.models import ReviewDecision, Submission

from . import field_priority as p
from . import service
from .handoff_metadata import BoundedMetadata
from .models import FieldReviewArbitrationHandoff, ReviewBatch

SCHEMA = 'review-field-arbitration-handoff/v1'
V1_ORIGIN = 64 * 1024
V1_STORED_ORIGIN = 256 * 1024
MAX_METADATA = 4 * 1024 * 1024
MAX_STORED_METADATA = 16 * 1024 * 1024


def error(reason, status=409):
    raise fields.Error('REVIEW_FIELD_HANDOFF_' + reason, status)


def guard(pk, principal, actor, *, create=False, **options):
    if not settings.ANNOTATION_FIELD_REVIEW_ARBITRATION_HANDOFF_ENABLED:
        raise NotFound()
    project, role = p.read_guard(pk, principal, actor)
    other, other_role = a.guard(pk, principal, actor, create=create)
    if other.pk != project.pk or role != other_role:
        error('AUTHORITY_CHANGED', 403)
    return project, role


def owner_hash(context):
    """Stable identity comparison across viewer-salt rotation, never authority."""
    principal, actor, project, role = context
    return fields.digest(
        {
            'schema': SCHEMA + '/owner',
            'principal': principal.principal_id,
            'source': principal.source,
            'actor_id': actor.pk,
            'organization_id': project.organization_id,
            'project_id': project.pk,
            'role': role,
        }
    )


class Metadata(BoundedMetadata):
    def __init__(self):
        super().__init__(MAX_METADATA, MAX_STORED_METADATA, fields.bounded, error)


def review_batch(context, identifier, budget):
    row = budget.load(ReviewBatch.objects.filter(project=context[2]), identifier, 'plan', p.V1_BYTES, 4 * p.V1_BYTES)
    return service.check_loaded_batch(row, context[2])


def analysis(context, identifier, budget, *, lock=False):
    row = budget.load(
        FieldBatch.objects.filter(project=context[2]),
        identifier,
        'manifest',
        b.MAX_MANIFEST,
        4 * b.MAX_MANIFEST,
        lock=lock,
    )
    return b.checked(row, context[2])


def metadata_row(query, identifier, fields, budget, *, lock=False):
    return budget.row(query, identifier, fields, a.JSON_LIMITS, a.guarded, a.hydrate, lock=lock)


def target(context, identifier, budget, *, lock=False):
    # Authorization before hydrating origin; scopes exclude the whole self cohort.
    visible = a.scoped(context[2], context[1], context[3]).values('pk')
    query = FieldArbitrationTask.objects.filter(project=context[2], pk__in=visible)
    row = metadata_row(query, identifier, ('origin', 'participant_ids', 'cancellation'), budget, lock=lock)

    def decision_loader(arbitration):
        query = FieldArbitrationDecision.objects.filter(project=context[2], arbitration_id=arbitration.pk)
        identifier = query.values_list('pk', flat=True).first()
        return metadata_row(query, identifier, ('winner', 'reason'), budget, lock=lock) if identifier else None

    return a.checked(row, context[2], decision_loader=decision_loader)


def require_pending(context, source_id, pin):
    row = get_object_or_404(
        Submission.objects.filter(assignment__project=context[2], assignment__task__project=context[2])
        .select_related('assignment')
        .only(
            'id',
            'assignment_id',
            'assignment__project_id',
            'assignment__task_id',
            'assignment__assignee_id',
            'revision',
            'result_hash',
            'submitted_by_id',
            'status',
        ),
        pk=source_id,
    )
    if service.identity(row) != pin or row.assignment.project_id != context[2].pk:
        error('SOURCE_CHANGED')
    if (
        row.status != 'pending'
        or ReviewDecision.objects.filter(submission_id=row.pk).exists()
        or Submission.objects.filter(assignment_id=row.assignment_id, revision__gt=row.revision).exists()
    ):
        error('SOURCE_NOT_PENDING')


def source_metadata(context, batch_id, source_id, budget, *, lock=False, fresh=True):
    """Complete envelopes and exact proof; no snapshots or calculations."""
    batch = review_batch(context, batch_id, budget)
    if batch.plan.get('algorithm') != p.ALGORITHM:
        error('UNSUPPORTED_BATCH')
    p.read_guard(context[2].pk, context[0], context[1], batch.plan)
    candidate = next(
        (c for c in service.visible_candidates(batch, context[1], context[3]) if c['submission_id'] == source_id), None
    )
    if candidate is None:
        raise NotFound()
    selected = next((e for e in batch.plan['priority']['evidence'] if e['submission_id'] == source_id), None)
    if (
        not candidate['selected']
        or selected is None
        or (selected['state'], selected['reason']) != ('conflict', 'frozen_conflict')
    ):
        error('INELIGIBLE')
    frozen = batch.plan['priority']
    original = analysis(context, frozen['batch_id'], budget, lock=lock)
    origin = a.origin(context[2], original, candidate['task_id'], frozen['manifest_hash'])
    pin = {k: v for k, v in candidate.items() if k != 'selected'}
    if (
        selected['calculation_hash'] != origin['item']['result']['calculation_hash']
        or any(frozen[k] != origin[k] for k in p.VERSIONS)
        or not any(e['source'] == selected['source_pin'] for e in origin['item']['cohort'])
    ):
        error('SOURCE_CHANGED')
    if fresh:
        require_pending(context, source_id, pin)
    return {
        'batch': batch,
        'origin': origin,
        'input_set_hash': a.input_hash(context[2], origin['item']),
        'proof': {
            'review': {
                'batch_id': batch.pk,
                'plan_hash': batch.plan_hash,
                'algorithm': p.ALGORITHM,
                'candidate': pin,
                'source_pin': selected['source_pin'],
                'selected': True,
                'state': 'conflict',
                'reason': 'frozen_conflict',
                'calculation_hash': selected['calculation_hash'],
            },
            'analysis': {
                'batch_id': original.pk,
                'manifest_hash': original.manifest_hash,
                'task_id': candidate['task_id'],
                **{k: origin[k] for k in p.VERSIONS},
                'calculation_hash': selected['calculation_hash'],
                'input_set_hash': a.input_hash(context[2], origin['item']),
            },
        },
    }


def source(context, batch_id, source_id, budget, *, lock=False, replay=None):
    current = source_metadata(context, batch_id, source_id, budget, lock=lock)
    evidence, prepared = a.evidence(
        context,
        current['origin'],
        lock=lock,
        replay=replay,
        batch_loader=lambda identifier, **kw: analysis(context, identifier, budget, **kw),
    )
    a.require_evidence(evidence)
    require_pending(context, source_id, current['proof']['review']['candidate'])
    return {**current, 'evidence': evidence, 'prepared': prepared}


def same_input(context, current, row, budget, *, lock=False):
    original = analysis(context, row.field_batch_id_frozen, budget, lock=lock)
    if (
        a.origin(context[2], original, row.task_id_frozen, row.origin['batch_manifest_hash']) != row.origin
        or row.input_set_hash != current['input_set_hash']
        or row.origin['item'] != current['origin']['item']
    ):
        error('INPUT_SET_CHANGED')


def target_pin(row):
    return {
        'id': row.pk,
        'origin_hash': row.origin_hash,
        'input_set_hash': row.input_set_hash,
        'created_at': row.created_at.isoformat(),
        'actor_id': row.actor_id_frozen,
        'assignee_id': row.assignee_id_frozen,
        'batch_id': row.field_batch_id_frozen,
        'manifest_hash': row.origin['batch_manifest_hash'],
        'task_id': row.task_id_frozen,
        'version_at_link': row.version,
        'status_at_link': row.status,
    }


def context_data(context, batch_id, source_id, replay=None):
    if context[3] != 'manager':
        raise NotFound()
    budget = Metadata()
    base = {
        'schema': SCHEMA,
        'kind': 'field',
        'viewer_hash': a.viewer_hash(context),
        'actor_id': context[1].pk,
        'project_id': context[2].pk,
        'review_batch_id': batch_id,
        'submission_id': source_id,
    }
    try:
        current = source_metadata(context, batch_id, source_id, budget)
    except fields.Error as exc:
        if exc.code in (
            'REVIEW_FIELD_HANDOFF_UNSUPPORTED_BATCH',
            'REVIEW_FIELD_HANDOFF_INELIGIBLE',
            'REVIEW_FIELD_HANDOFF_SOURCE_NOT_PENDING',
            'FIELD_ARBITRATION_INELIGIBLE',
        ):
            return {**base, 'eligible': False, 'reason': exc.code, 'can_handoff': False}, {}
        raise
    existing = (
        FieldArbitrationTask.objects.filter(
            project=context[2],
            task_id_frozen=current['origin']['item']['task_id'],
            input_set_hash=current['input_set_hash'],
            status__in=('pending', 'resolved'),
        )
        .values_list('pk', flat=True)
        .first()
    )
    row = target(context, existing, budget) if existing else None
    if row:
        same_input(context, current, row, budget)
    # Only eligible metadata reaches the bounded target cohort calculation.
    current = source(context, batch_id, source_id, budget, replay=replay)
    return {
        **base,
        'eligible': True,
        'reason': None,
        'revision': current['proof']['review']['candidate']['revision'],
        'expected_plan_hash': current['batch'].plan_hash,
        'expected_input_set_hash': current['input_set_hash'],
        'can_handoff': project_operation_policy(context[2]).allows(ORGANIZE_FIELD_ARBITRATION),
        'dispatch_origin': {
            'field_batch_id': current['origin']['field_batch_id'],
            'task_id': current['origin']['item']['task_id'],
            'expected_manifest_hash': current['origin']['batch_manifest_hash'],
        },
        'existing_arbitration': a.meta(row, context) if row else None,
        'limits': {
            'participants': a.MAX_PARTICIPANTS,
            'scorer_calls': 0,
            'normalizations_per_pass': a.MAX_NORMALIZATIONS,
            'equalities_per_pass': a.MAX_EQUALITIES,
        },
    }, current['prepared']


def rows(project):
    return a.guarded(FieldReviewArbitrationHandoff.objects.filter(project=project), ('origin',))


def prepare_page(context, identifiers, budget):
    remaining = MAX_STORED_METADATA - budget.stored
    query = rows(context[2]).filter(pk__in=identifiers).annotate(_page_bytes=Window(Sum('_size_origin')))
    query = query.annotate(
        _safe_origin=Case(
            When(_page_bytes__lte=remaining, _size_origin__lte=V1_STORED_ORIGIN, then=F('origin')),
            default=Value(None),
            output_field=models.JSONField(),
        )
    ).order_by('-pk')
    page = list(query)
    if page and page[0]._page_bytes > remaining:
        error('BUDGET_EXCEEDED', 422)
    # Reserve every decoded page proof before loading any target JSON.
    for row in page:
        checked(row, context[2])
        budget.add(('handoff', row.pk), row.origin, row._size_origin)
    return page


def checked(row, project):
    a.hydrate(row, ('origin',))
    try:
        o = row.origin
        valid = (
            set(o)
            == {
                'schema',
                'kind',
                'project_id',
                'organization_id',
                'actor_id',
                'principal',
                'source',
                'created_at',
                'key',
                'request_hash',
                'mode',
                'review',
                'analysis',
                'target',
            }
            and o['schema'] == SCHEMA
            and o['kind'] == 'field'
            and all(a.integer(o[k]) for k in ('project_id', 'organization_id', 'actor_id'))
            and o['project_id'] == project.pk == row.project_id
            and o['organization_id'] == project.organization_id
            and o['actor_id'] == row.actor_id_frozen
            and a.integer(row.actor_id_frozen)
            and o['principal'] == row.creator_principal
            and isinstance(o['principal'], str)
            and 1 <= len(o['principal']) <= 512
            and o['source'] == row.creator_source
            and isinstance(o['source'], str)
            and 1 <= len(o['source']) <= 128
            and o['created_at'] == row.created_at.isoformat()
            and o['key'] == str(row.idempotency_key)
            and o['request_hash'] == row.request_hash
            and a.hash_value(row.request_hash)
            and row.origin_hash == fields.digest(o)
            and o['mode'] in ('dispatch', 'link_existing')
            and o['review']['batch_id'] == row.review_batch_id_frozen
            and set(o['review'])
            == {
                'batch_id',
                'plan_hash',
                'algorithm',
                'candidate',
                'source_pin',
                'selected',
                'state',
                'reason',
                'calculation_hash',
            }
            and a.integer(o['review']['batch_id'])
            and a.hash_value(o['review']['plan_hash'])
            and set(o['review']['candidate'])
            == {'submission_id', 'assignment_id', 'task_id', 'revision', 'submitter_id', 'result_hash'}
            and all(a.integer(o['review']['candidate'][k]) for k in o['review']['candidate'] if k != 'result_hash')
            and a.hash_value(o['review']['candidate']['result_hash'])
            and o['review']['candidate']['submission_id'] == row.submission_id_frozen
            and o['review']['algorithm'] == p.ALGORITHM
            and o['review']['selected'] is True
            and o['review']['state'] == 'conflict'
            and o['review']['reason'] == 'frozen_conflict'
            and a.hash_value(o['review']['calculation_hash'])
            and o['review']['calculation_hash'] == o['analysis']['calculation_hash']
            and set(o['review']['source_pin']) == set(field_api.PIN_IDS + field_api.PIN_HASHES)
            and all(a.integer(o['review']['source_pin'][k]) for k in field_api.PIN_IDS)
            and all(a.hash_value(o['review']['source_pin'][k]) for k in field_api.PIN_HASHES)
            and all(o['review']['source_pin'][k] == v for k, v in o['review']['candidate'].items())
            and o['review']['source_pin']['assignee_id'] == o['review']['source_pin']['submitter_id']
            and o['target']['id'] == row.arbitration_id_frozen
            and set(o['target'])
            == {
                'id',
                'origin_hash',
                'input_set_hash',
                'created_at',
                'actor_id',
                'assignee_id',
                'batch_id',
                'manifest_hash',
                'task_id',
                'status_at_link',
                'version_at_link',
            }
            and a.hash_value(o['target']['origin_hash'])
            and a.hash_value(o['target']['manifest_hash'])
            and all(
                a.integer(o['target'][k])
                for k in ('id', 'actor_id', 'assignee_id', 'batch_id', 'task_id', 'version_at_link')
            )
            and set(o['analysis'])
            == {
                'batch_id',
                'manifest_hash',
                'task_id',
                *p.VERSIONS,
                'calculation_hash',
                'input_set_hash',
            }
            and a.integer(o['analysis']['batch_id'])
            and a.integer(o['analysis']['task_id'])
            and o['analysis']['task_id'] == o['target']['task_id'] == o['review']['candidate']['task_id']
            and o['target']['input_set_hash'] == o['analysis']['input_set_hash']
            and all(a.hash_value(o['analysis'][k]) for k in ('manifest_hash', 'calculation_hash', 'input_set_hash'))
            and all(o['analysis'][k] == v for k, v in p.VERSIONS.items())
            and (o['target']['status_at_link'], o['target']['version_at_link']) in (('pending', 1), ('resolved', 2))
            and (row.review_batch_id is None or row.review_batch_id == row.review_batch_id_frozen)
            and (row.field_arbitration_id is None or row.field_arbitration_id == row.arbitration_id_frozen)
            and (row.created_by_id is None or row.created_by_id == row.actor_id_frozen)
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        error('LEDGER_INVALID')
    return row


def scoped(context, batch_id, source_id=None, *, budget=None):
    query = rows(context[2]).filter(review_batch_id_frozen=batch_id)
    if source_id is not None:
        query = query.filter(submission_id_frozen=source_id)
    if context[3] == 'reviewer':
        batch = review_batch(context, batch_id, budget if budget is not None else Metadata())
        visible = [c['submission_id'] for c in service.visible_candidates(batch, context[1], context[3])]
        query = query.filter(
            review_batch_id=batch.pk,
            submission_id_frozen__in=visible,
            field_arbitration_id__in=a.scoped(context[2], context[1], context[3]).values('pk'),
        )
    return query.order_by('-pk')


def record(context, identifier, budget):
    return checked(
        budget.load(
            FieldReviewArbitrationHandoff.objects.filter(project=context[2]),
            identifier,
            'origin',
            V1_ORIGIN,
            V1_STORED_ORIGIN,
        ),
        context[2],
    )


def current_target(row, context, budget):
    live = target(context, row.field_arbitration_id, budget)
    expected = row.origin['target']
    if any(target_pin(live)[k] != expected[k] for k in expected if k not in ('status_at_link', 'version_at_link')):
        error('TARGET_CHANGED')
    return live


def detail(row, context, budget):
    if getattr(row, '_page_bytes', 0) > MAX_STORED_METADATA:
        error('BUDGET_EXCEEDED', 422)
    checked(row, context[2])
    budget.add(('handoff', row.pk), row.origin, getattr(row, '_size_origin', 0))
    live = None
    if row.field_arbitration_id is not None:
        try:
            live = current_target(row, context, budget)
        except NotFound:
            live = None
    status = 'unavailable'
    if row.review_batch_id is not None:
        current = (
            Submission.objects.filter(
                pk=row.submission_id_frozen, assignment__project=context[2], assignment__task__project=context[2]
            )
            .values(
                'status',
                'assignment_id',
                'revision',
                'submitted_by_id',
                'result_hash',
                'assignment__task_id',
                'assignment__assignee_id',
            )
            .first()
        )
        pin = row.origin['review']['source_pin']
        if current and all(
            current[k] == pin[v]
            for k, v in (
                ('assignment_id', 'assignment_id'),
                ('revision', 'revision'),
                ('submitted_by_id', 'submitter_id'),
                ('result_hash', 'result_hash'),
                ('assignment__task_id', 'task_id'),
                ('assignment__assignee_id', 'assignee_id'),
            )
        ):
            status = (
                current['status']
                if current['status'] in ('pending', 'approved', 'rejected', 'superseded')
                else 'unavailable'
            )
    return {
        'schema': SCHEMA,
        'kind': 'field',
        'id': row.pk,
        'project_id': context[2].pk,
        'actor_id': row.actor_id_frozen,
        'viewer_hash': a.viewer_hash(context),
        'review_batch_id': row.review_batch_id_frozen,
        'submission_id': row.submission_id_frozen,
        'revision': row.origin['review']['candidate']['revision'],
        'arbitration_id': row.arbitration_id_frozen,
        'input_set_hash': row.origin['target']['input_set_hash'],
        'mode': row.origin['mode'],
        'created_at': row.created_at.isoformat(),
        'source_integrity': 'not_checked',
        'source_status': status,
        'available': row.review_batch_id is not None and live is not None,
        'arbitration_status': live.status if live else None,
        'arbitration_version': live.version if live else None,
        'decision': None,
        'target': {
            k: v
            for k, v in row.origin['target'].items()
            if context[3] == 'manager' or k not in ('origin_hash', 'manifest_hash', 'actor_id')
        },
    }


def inspect_graph(context, item):
    """Current graph metadata only; snapshot integrity belongs to the B detail."""
    pins = [entry['source'] for entry in item['cohort']]
    rows = field_api.metadata(b.scoped_sources(context[2], [pin['submission_id'] for pin in pins]))
    current = {row['id']: row for row in rows}
    for pin in pins:
        row = current.get(pin['submission_id'])
        if row is None or any(
            row[k] != pin[v]
            for k, v in (
                ('assignment_id', 'assignment_id'),
                ('assignment__assignee_id', 'assignee_id'),
                ('assignment__task_id', 'task_id'),
                ('submitted_by_id', 'submitter_id'),
                ('revision', 'revision'),
                ('result_hash', 'result_hash'),
            )
        ):
            error('SOURCE_CHANGED')


def provenance(row, context, budget):
    """Validate both complete origins after the B detail, without fresh eligibility."""
    checked(row, context[2])
    base = {
        'schema': SCHEMA,
        'kind': 'field',
        'project_id': context[2].pk,
        'id': row.pk,
        'review_batch_id': row.review_batch_id_frozen,
        'submission_id': row.submission_id_frozen,
        'viewer_hash': a.viewer_hash(context),
        'arbitration_id': row.arbitration_id_frozen,
        'snapshot_integrity': 'not_checked',
    }
    unavailable = {**base, 'provenance_status': 'unavailable', 'reason': 'source_unavailable', 'target': None}
    if row.review_batch_id is None or row.field_arbitration_id is None:
        return unavailable
    try:
        current = source_metadata(context, row.review_batch_id, row.submission_id_frozen, budget, fresh=False)
        if current['proof'] != {k: row.origin[k] for k in ('review', 'analysis')}:
            error('SOURCE_CHANGED')
        live = current_target(row, context, budget)
        same_input(context, current, live, budget)
        inspect_graph(context, current['origin']['item'])
    except (NotFound, Http404):
        return unavailable
    return {
        **base,
        'provenance_status': 'verified',
        'reason': None,
        'target': {
            k: v
            for k, v in a.meta(live, context).items()
            if k
            in (
                'id',
                'viewer_hash',
                'input_set_hash',
                'task_evidence_hash',
                'field_batch_id',
                'task_id',
                'created_at',
                'assignee_id',
                'status',
                'version',
            )
        },
    }


def create_record(context, current, row, mode, key, request_hash, budget):
    principal, actor, project, _ = context
    if FieldReviewArbitrationHandoff.objects.filter(
        project=project,
        review_batch_id_frozen=current['batch'].pk,
        submission_id_frozen=current['proof']['review']['candidate']['submission_id'],
        arbitration_id_frozen=row.pk,
    ).exists():
        error('ALREADY_EXISTS')
    created = timezone.now()
    origin = {
        'schema': SCHEMA,
        'kind': 'field',
        'project_id': project.pk,
        'organization_id': project.organization_id,
        'actor_id': actor.pk,
        'principal': principal.principal_id,
        'source': principal.source,
        'created_at': created.isoformat(),
        'key': str(key),
        'request_hash': request_hash,
        'mode': mode,
        **current['proof'],
        'target': target_pin(row),
    }
    fields.bounded(origin, V1_ORIGIN)
    record = FieldReviewArbitrationHandoff.objects.create(
        project=project,
        review_batch=current['batch'],
        field_arbitration=row,
        created_by=actor,
        review_batch_id_frozen=current['batch'].pk,
        submission_id_frozen=origin['review']['candidate']['submission_id'],
        arbitration_id_frozen=row.pk,
        actor_id_frozen=actor.pk,
        creator_principal=principal.principal_id,
        creator_source=principal.source,
        created_at=created,
        origin=origin,
        origin_hash=fields.digest(origin),
        idempotency_key=key,
        request_hash=request_hash,
    )
    stored = rows(project).filter(pk=record.pk).values_list('_size_origin', flat=True).get()
    budget.add(('new_proof', str(key)), origin, stored)
    return record


def receipt(row, context, replayed):
    checked(row, context[2])
    value = {
        'operation': 'handoff',
        'key': str(row.idempotency_key),
        'request_hash': row.request_hash,
        'mode': row.origin['mode'],
        'plan_hash': row.origin['review']['plan_hash'],
        'arbitration_version_at_commit': row.origin['target']['version_at_link'],
        'arbitration_status_at_commit': row.origin['target']['status_at_link'],
        'origin_hash': row.origin_hash,
    }
    return {
        'schema': SCHEMA,
        'kind': 'field',
        'id': row.pk,
        'project_id': context[2].pk,
        'actor_id': row.actor_id_frozen,
        'review_batch_id': row.review_batch_id_frozen,
        'submission_id': row.submission_id_frozen,
        'arbitration_id': row.arbitration_id_frozen,
        'input_set_hash': row.origin['target']['input_set_hash'],
        'viewer_hash': a.viewer_hash(context),
        'assignee_id': row.origin['target']['assignee_id'],
        'replayed': replayed,
        'receipt': value,
    }
