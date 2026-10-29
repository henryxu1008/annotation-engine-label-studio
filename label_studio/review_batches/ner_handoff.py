"""NER review provenance and bounded metadata. No decisions or answer copies."""
from access_control.project_operations import ORGANIZE_NER_ARBITRATION, project_operation_policy
from classification_disagreements import ner
from classification_disagreements import ner_arbitration as a
from classification_disagreements import ner_batches as b
from classification_disagreements.models import NerArbitrationDecision, NerArbitrationTask, NerBatch
from django.conf import settings
from django.db import connection, models
from django.db.models import Case, F, Sum, Value, When, Window
from django.db.models.expressions import RawSQL
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import NotFound
from tasks.models import ReviewDecision, Submission

from . import ner_priority as p
from . import service
from .models import NerReviewArbitrationHandoff, ReviewBatch

SCHEMA = 'review-ner-arbitration-handoff/v1'
V1_ORIGIN = 64 * 1024
V1_STORED_ORIGIN = 256 * 1024
MAX_METADATA = 4 * 1024 * 1024
MAX_STORED_METADATA = 16 * 1024 * 1024


def error(reason, status=409):
    raise ner.Error('REVIEW_NER_HANDOFF_' + reason, status)


def guard(pk, principal, actor, *, create=False, **options):
    if not settings.ANNOTATION_NER_REVIEW_ARBITRATION_HANDOFF_ENABLED:
        raise NotFound()
    project, role = p.read_guard(pk, principal, actor)
    other, other_role = a.guard(pk, principal, actor, create=create)
    if other.pk != project.pk or role != other_role:
        error('AUTHORITY_CHANGED', 403)
    return project, role


class Metadata:
    """Per-request distinct objects; SQL CASE gates precede JSON decoding."""

    def __init__(self):
        self.canonical = self.stored = 0
        self.cache = {}

    def add(self, key, value, stored):
        if key in self.cache:
            return
        size = ner.bounded(value, MAX_METADATA)
        if self.canonical + size > MAX_METADATA or self.stored + stored > MAX_STORED_METADATA:
            error('BUDGET_EXCEEDED', 422)
        self.canonical += size
        self.stored += stored
        self.cache[key] = value

    def load(self, query, identifier, field, canonical, stored, *, lock=False, row_total=False):
        key = (query.model._meta.db_table, identifier, field)
        if key in self.cache:
            return self.cache[key]
        table = connection.ops.quote_name(query.model._meta.db_table)
        column = table + '.' + connection.ops.quote_name(field)
        sql = (
            f'OCTET_LENGTH(CAST({column} AS TEXT))'
            if connection.vendor == 'postgresql'
            else f'LENGTH(CAST({column} AS BLOB))'
        )
        ceiling = min(stored, MAX_STORED_METADATA - self.stored)
        gates = {'_handoff_size__lte': ceiling}
        if row_total:
            gates['_target_bytes__lte'] = MAX_STORED_METADATA - self.stored
        query = query.annotate(_handoff_size=RawSQL(sql, [], output_field=models.BigIntegerField()))
        query = query.annotate(
            _handoff_value=Case(When(**gates, then=F(field)), default=Value(None), output_field=models.JSONField())
        ).defer(field)
        if lock:
            query = query.select_for_update()
        row = get_object_or_404(query, pk=identifier)
        if type(row._handoff_size) is not int or row._handoff_size < 0 or row._handoff_size > ceiling:
            error('BUDGET_EXCEEDED', 422)
        if row_total and row._target_bytes > MAX_STORED_METADATA - self.stored:
            error('BUDGET_EXCEEDED', 422)
        value = row._handoff_value
        ner.bounded(value, canonical)
        # Cache stores the row to prevent accidental reload of a deferred field.
        self.add(key, value, row._handoff_size)
        setattr(row, field, value)
        self.cache[key] = row
        return row


def review_batch(context, identifier, budget):
    row = budget.load(
        ReviewBatch.objects.filter(project=context[2]), identifier, 'plan', p.V1_PLAN_BYTES, 4 * p.V1_PLAN_BYTES
    )
    return service.check_loaded_batch(row, context[2])


def analysis(context, identifier, budget, *, lock=False):
    row = budget.load(
        NerBatch.objects.filter(project=context[2]),
        identifier,
        'manifest',
        b.V1_MANIFEST,
        4 * b.V1_MANIFEST,
        lock=lock,
    )
    return b.checked(row, context[2])


def metadata_row(query, identifier, fields, budget, *, lock=False):
    """Reserve every decoded field before dependent ledger validation reads more."""
    query = a.guarded(query, fields[1:])
    table = connection.ops.quote_name(query.model._meta.db_table)
    measure = 'OCTET_LENGTH(CAST({} AS TEXT))' if connection.vendor == 'postgresql' else 'LENGTH(CAST({} AS BLOB))'
    total = '+'.join(
        'COALESCE(' + measure.format(table + '.' + connection.ops.quote_name(field)) + ',0)' for field in fields
    )
    query = query.annotate(_target_bytes=RawSQL(total, [], output_field=models.BigIntegerField()))
    remaining = MAX_STORED_METADATA - budget.stored
    for field in fields[1:]:
        output = query.model._meta.get_field(field)
        stored = a.JSON_LIMITS.get(field, (16 * 1024, 16 * 1024))[1]
        query = query.annotate(
            **{
                f'_safe_{field}': Case(
                    When(
                        **{'_target_bytes__lte': remaining, f'_size_{field}__lte': stored},
                        then=F(field),
                    ),
                    default=Value(None),
                    output_field=output,
                )
            }
        )
    row = budget.load(query, identifier, fields[0], *a.JSON_LIMITS[fields[0]], lock=lock, row_total=True)
    a.hydrate(row, fields)
    for field in fields[1:]:
        budget.add((row._meta.db_table, identifier, field), getattr(row, field), getattr(row, f'_size_{field}') or 0)
    return row


def target(context, identifier, budget, *, lock=False):
    # Authorization before hydrating origin; scopes exclude the whole self cohort.
    visible = a.scoped(context[2], context[1], context[3]).values('pk')
    query = NerArbitrationTask.objects.filter(project=context[2], pk__in=visible)
    row = metadata_row(query, identifier, ('origin', 'participant_ids', 'cancellation'), budget, lock=lock)

    def decision_loader(arbitration):
        query = NerArbitrationDecision.objects.filter(project=context[2], arbitration_id=arbitration.pk)
        identifier = query.values_list('pk', flat=True).first()
        return metadata_row(query, identifier, ('winner', 'reason'), budget, lock=lock) if identifier else None

    return a.checked(row, context[2], decision_loader=decision_loader)


def require_pending(context, source_id, pin):
    row = get_object_or_404(
        Submission.objects.select_related('assignment').only(
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


def source(context, batch_id, source_id, budget, *, lock=False):
    batch = review_batch(context, batch_id, budget)
    if batch.plan.get('algorithm') != p.ALGORITHM:
        error('UNSUPPORTED_BATCH')
    candidate = next(
        (c for c in service.visible_candidates(batch, context[1], context[3]) if c['submission_id'] == source_id), None
    )
    if candidate is None:
        raise NotFound()
    selected = next((e for e in batch.plan['priority']['evidence'] if e['submission_id'] == source_id), None)
    if not candidate['selected'] or selected is None or selected['state'] != 'conflict':
        error('INELIGIBLE')
    frozen = batch.plan['priority']
    original = analysis(context, frozen['batch_id'], budget, lock=lock)
    origin = a.origin(context[2], original, candidate['task_id'], frozen['manifest_hash'])
    pin = {k: v for k, v in candidate.items() if k != 'selected'}
    if (
        selected['calculation_hash'] != origin['item']['result']['calculation_hash']
        or frozen['metric_rule'] != origin['metric_rule']
        or frozen['selection'] != origin['selection']
        or frozen['explanation'] != origin['explanation']
        or frozen['engine'] != origin['engine']
        or not any(
            e['source'] and all(e['source'].get(k) == v for k, v in pin.items()) for e in origin['item']['cohort']
        )
    ):
        error('SOURCE_CHANGED')
    require_pending(context, source_id, pin)
    evidence, prepared = a.evidence(context, origin, lock=lock)
    a.require_evidence(evidence)
    require_pending(context, source_id, pin)
    candidate_source = next(
        e['source'] for e in origin['item']['cohort'] if e['source'] and e['source']['submission_id'] == source_id
    )
    return {
        'batch': batch,
        'origin': origin,
        'evidence': evidence,
        'prepared': prepared,
        'input_set_hash': a.input_hash(context[2], origin['item']),
        'proof': {
            'review': {
                'batch_id': batch.pk,
                'plan_hash': batch.plan_hash,
                'algorithm': p.ALGORITHM,
                'candidate': {**pin, 'assignee_id': candidate_source['assignee_id']},
                'selected': True,
                'state': 'conflict',
            },
            'analysis': {
                'batch_id': original.pk,
                'manifest_hash': original.manifest_hash,
                'selection': origin['selection'],
                'metric_rule': origin['metric_rule'],
                'explanation': origin['explanation'],
                'engine': origin['engine'],
                'calculation_hash': selected['calculation_hash'],
                'input_set_hash': a.input_hash(context[2], origin['item']),
            },
        },
    }


def same_input(context, current, row, budget, *, lock=False):
    original = analysis(context, row.ner_batch_id_frozen, budget, lock=lock)
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
        'batch_id': row.ner_batch_id_frozen,
        'version_at_link': row.version,
        'status_at_link': row.status,
    }


def context_data(context, batch_id, source_id):
    if context[3] != 'manager':
        raise NotFound()
    budget = Metadata()
    base = {
        'schema': SCHEMA,
        'kind': 'ner',
        'viewer_hash': a.viewer_hash(context),
        'actor_id': context[1].pk,
        'project_id': context[2].pk,
        'review_batch_id': batch_id,
        'submission_id': source_id,
    }
    try:
        current = source(context, batch_id, source_id, budget)
    except ner.Error as exc:
        if exc.code in (
            'REVIEW_NER_HANDOFF_UNSUPPORTED_BATCH',
            'REVIEW_NER_HANDOFF_INELIGIBLE',
            'REVIEW_NER_HANDOFF_SOURCE_NOT_PENDING',
            'NER_ARBITRATION_SOURCE_UNAVAILABLE',
        ):
            return {**base, 'eligible': False, 'reason': exc.code, 'can_handoff': False}, {}
        raise
    existing = (
        NerArbitrationTask.objects.filter(
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
    return {
        **base,
        'eligible': True,
        'reason': None,
        'revision': current['proof']['review']['candidate']['revision'],
        'expected_plan_hash': current['batch'].plan_hash,
        'expected_input_set_hash': current['input_set_hash'],
        'can_handoff': project_operation_policy(context[2]).allows(ORGANIZE_NER_ARBITRATION),
        'dispatch_origin': {
            'ner_batch_id': current['origin']['ner_batch_id'],
            'task_id': current['origin']['item']['task_id'],
            'expected_manifest_hash': current['origin']['batch_manifest_hash'],
        },
        'existing_arbitration': a.meta(row, context) if row else None,
        'limits': {'participants': a.MAX_PARTICIPANTS, 'scorer_calls': a.MAX_CALLS},
    }, current['prepared']


def rows(project):
    return a.guarded(NerReviewArbitrationHandoff.objects.filter(project=project), ('origin',))


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
            and o['kind'] == 'ner'
            and o['project_id'] == project.pk == row.project_id
            and o['organization_id'] == project.organization_id
            and o['actor_id'] == row.actor_id_frozen
            and a.integer(row.actor_id_frozen)
            and o['principal'] == row.creator_principal
            and bool(o['principal'])
            and o['source'] == row.creator_source
            and bool(o['source'])
            and o['created_at'] == row.created_at.isoformat()
            and o['key'] == str(row.idempotency_key)
            and o['request_hash'] == row.request_hash
            and a.hash_value(row.request_hash)
            and row.origin_hash == ner.digest(o)
            and o['mode'] in ('dispatch', 'link_existing')
            and o['review']['batch_id'] == row.review_batch_id_frozen
            and set(o['review']) == {'batch_id', 'plan_hash', 'algorithm', 'candidate', 'selected', 'state'}
            and a.hash_value(o['review']['plan_hash'])
            and set(o['review']['candidate'])
            == {'submission_id', 'assignment_id', 'assignee_id', 'task_id', 'revision', 'submitter_id', 'result_hash'}
            and all(a.integer(o['review']['candidate'][k]) for k in o['review']['candidate'] if k != 'result_hash')
            and a.hash_value(o['review']['candidate']['result_hash'])
            and o['review']['candidate']['submission_id'] == row.submission_id_frozen
            and o['review']['algorithm'] == p.ALGORITHM
            and o['review']['selected'] is True
            and o['review']['state'] == 'conflict'
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
                'status_at_link',
                'version_at_link',
            }
            and a.hash_value(o['target']['origin_hash'])
            and all(
                a.integer(o['target'][k]) for k in ('id', 'actor_id', 'assignee_id', 'batch_id', 'version_at_link')
            )
            and set(o['analysis'])
            == {
                'batch_id',
                'manifest_hash',
                'selection',
                'metric_rule',
                'explanation',
                'engine',
                'calculation_hash',
                'input_set_hash',
            }
            and a.integer(o['analysis']['batch_id'])
            and o['target']['input_set_hash'] == o['analysis']['input_set_hash']
            and all(a.hash_value(o['analysis'][k]) for k in ('manifest_hash', 'calculation_hash', 'input_set_hash'))
            and o['analysis']['selection'] == b.SELECTION
            and o['analysis']['metric_rule'] == ner.RULE
            and o['analysis']['explanation'] == ner.EXPLANATION
            and o['analysis']['engine'] == ner.ENGINE
            and (o['target']['status_at_link'], o['target']['version_at_link']) in (('pending', 1), ('resolved', 2))
            and (row.review_batch_id is None or row.review_batch_id == row.review_batch_id_frozen)
            and (row.ner_arbitration_id is None or row.ner_arbitration_id == row.arbitration_id_frozen)
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
            ner_arbitration_id__in=a.scoped(context[2], context[1], context[3]).values('pk'),
        )
    return query.order_by('-pk')


def detail(row, context, budget):
    if getattr(row, '_page_bytes', 0) > MAX_STORED_METADATA:
        error('BUDGET_EXCEEDED', 422)
    checked(row, context[2])
    budget.add(('handoff', row.pk), row.origin, getattr(row, '_size_origin', 0))
    live = None
    if row.ner_arbitration_id is not None:
        try:
            live = target(context, row.ner_arbitration_id, budget)
            expected = row.origin['target']
            if any(
                target_pin(live)[k] != expected[k] for k in expected if k not in ('status_at_link', 'version_at_link')
            ):
                error('TARGET_CHANGED')
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
        pin = row.origin['review']['candidate']
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
        'kind': 'ner',
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
        'target': row.origin['target'],
    }


def create_record(context, current, row, mode, key, request_hash, budget):
    principal, actor, project, _ = context
    if NerReviewArbitrationHandoff.objects.filter(
        project=project,
        review_batch_id_frozen=current['batch'].pk,
        submission_id_frozen=current['proof']['review']['candidate']['submission_id'],
        arbitration_id_frozen=row.pk,
    ).exists():
        error('ALREADY_EXISTS')
    created = timezone.now()
    origin = {
        'schema': SCHEMA,
        'kind': 'ner',
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
    ner.bounded(origin, V1_ORIGIN)
    record = NerReviewArbitrationHandoff.objects.create(
        project=project,
        review_batch=current['batch'],
        ner_arbitration=row,
        created_by=actor,
        review_batch_id_frozen=current['batch'].pk,
        submission_id_frozen=origin['review']['candidate']['submission_id'],
        arbitration_id_frozen=row.pk,
        actor_id_frozen=actor.pk,
        creator_principal=principal.principal_id,
        creator_source=principal.source,
        created_at=created,
        origin=origin,
        origin_hash=ner.digest(origin),
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
        'kind': 'ner',
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
