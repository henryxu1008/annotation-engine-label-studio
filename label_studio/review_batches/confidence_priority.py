"""Revision confidence is a typed input adapter to the existing ReviewBatch."""
import copy
from types import SimpleNamespace

from access_control.project_operations import ORGANIZE_REVIEW, lock_project_operation
from django.db import IntegrityError, transaction
from django.db.models import BooleanField, Case, Q, Value, When
from django.db.models.expressions import RawSQL
from rest_framework.exceptions import APIException, NotFound
from tasks.models import Submission, Task, TaskAssignment

from . import confidence_contract as c
from . import confidence_observations as observations
from . import service
from .models import ReviewBatch, ReviewConfidenceObservation

KIND, ALGORITHM, SCHEMA = c.KIND, c.ALGORITHM, c.SCHEMA
MAX_BYTES = c.MAX_OUTPUT
bounded, load = c.bounded, c.load
UNKNOWN = {'not_covered', 'unsupported_input', 'revision_binding_changed', 'source_unavailable'}


def submission_serializer():
    from tasks.serializers import ReviewDecisionSerializer, SubmissionSerializer
    from users.serializers import UserSimpleSerializer

    class ConfidenceReviewSerializer(ReviewDecisionSerializer):
        reviewer = UserSimpleSerializer(read_only=True)

    class ConfidenceSubmissionSerializer(SubmissionSerializer):
        submitted_by = UserSimpleSerializer(read_only=True)
        review = ConfidenceReviewSerializer(read_only=True)

    return ConfidenceSubmissionSerializer


def selection(value):
    if (
        not isinstance(value, dict)
        or set(value) != {'kind', 'profile_id', 'profile_digest', 'threshold_units'}
        or value['kind'] != KIND
        or not c.uuid_string(value['profile_id'])
        or not c.hexadecimal(value['profile_digest'])
        or not c.integer(value['threshold_units'])
    ):
        c.fail('INVALID_INPUT', 400)
    return dict(value)


def is_selection(value):
    return isinstance(value, dict) and value.get('kind') == KIND


def is_plan(plan):
    p = plan.get('priority') if isinstance(plan, dict) else None
    return isinstance(plan, dict) and (
        isinstance(plan.get('algorithm'), str)
        and plan['algorithm'].startswith('revision-confidence-ranked/')
        or isinstance(p, dict)
        and (
            p.get('kind') == KIND
            or isinstance(p.get('schema'), str)
            and p['schema'].startswith('review-confidence-priority/')
        )
    )


def marker():
    return (
        Q(plan__algorithm__startswith='revision-confidence-ranked/')
        | Q(plan__priority__schema__startswith='review-confidence-priority/')
        | Q(plan__priority__kind=KIND)
    )


def read_guard(pk, principal, actor, plan=None):
    current = c.guard(pk, principal, actor)
    if plan is not None:
        if (
            plan['priority']['instance_id'] != c.instance_id()
            or plan['priority']['organization_id'] != current[2].organization_id
        ):
            c.fail('AUTHORITY_CHANGED', 403)
        if current[3] == 'reviewer' and not service.visible_candidates(SimpleNamespace(plan=plan), actor, 'reviewer'):
            raise NotFound()
    return current[2], current[3]


def available(pk, principal, actor, *, create=False):
    try:
        c.guard(pk, principal, actor, create=create)
        return True
    except APIException:
        return False


def filter_visible(query, actor, role):
    if role != 'reviewer':
        return query
    from django.db import connection

    column = connection.ops.quote_name(ReviewBatch._meta.db_table) + '.' + connection.ops.quote_name('plan')
    if connection.vendor == 'postgresql':
        sql = f"""EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof({column}->'candidates')='array'
            THEN {column}->'candidates' ELSE '[]'::jsonb END) c
            WHERE c->>'submitter_id' IS DISTINCT FROM %s AND NOT EXISTS (
                SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof({column}->'priority'->'evidence')='array'
                THEN {column}->'priority'->'evidence' ELSE '[]'::jsonb END) e
                WHERE e->>'submission_id'=c->>'submission_id' AND e->>'assignee_id'=%s))"""
        args = [str(actor.pk), str(actor.pk)]
    else:
        sql = f"""EXISTS (SELECT 1 FROM json_each({column}, '$.candidates') c
            WHERE json_extract(CASE WHEN c.type='object' THEN c.value ELSE '{{}}' END, '$.submitter_id') IS NOT %s
            AND NOT EXISTS (SELECT 1 FROM json_each({column}, '$.priority.evidence') e
                WHERE json_extract(CASE WHEN e.type='object' THEN e.value ELSE '{{}}' END, '$.submission_id')=
                    json_extract(CASE WHEN c.type='object' THEN c.value ELSE '{{}}' END, '$.submission_id')
                AND json_extract(CASE WHEN e.type='object' THEN e.value ELSE '{{}}' END, '$.assignee_id')=%s))"""
        args = [actor.pk, actor.pk]
    return query.alias(
        _confidence_marker=Case(When(marker(), then=Value(True)), default=Value(False), output_field=BooleanField()),
        _confidence_visible=RawSQL(sql, args, output_field=BooleanField()),
    ).filter(Q(_confidence_marker=False) | Q(_confidence_visible=True))


def rank_key(project_id, seed, item, evidence):
    e = evidence[item['submission_id']]
    low = e['state'] == 'low_confidence'
    units = e['observation']['proof']['score_units'] if low else 0
    return (0 if low else 1, units, service.digest([ALGORITHM, project_id, seed, item]), item['submission_id'])


def assemble(project_id, args, candidates, p):
    evidence = {e['submission_id']: e for e in p['evidence']}
    return service.assemble_plan(
        project_id,
        args,
        candidates,
        p,
        ALGORITHM,
        ranking=lambda item: rank_key(project_id, args['seed'], item, evidence),
    )


def selected_prefix_valid(plan):
    """Validate the frozen selected prefix in linear work, without sorting again."""
    rule, p = plan['rule'], plan['priority']
    evidence = {e['submission_id']: e for e in p['evidence']}
    groups = {}
    for item in plan['candidates']:
        identity = {k: v for k, v in item.items() if k != 'selected'}
        key = rank_key(plan['project_id'], rule['seed'], identity, evidence)
        group = groups.setdefault(item['submitter_id'] if rule['per_submitter'] else 'all', [[], []])
        group[0 if item['selected'] else 1].append(key)
    for chosen, remaining in groups.values():
        if len(chosen) != ((len(chosen) + len(remaining)) * rule['rate'] + 99) // 100:
            return False
        if chosen and remaining and max(chosen) >= min(remaining):
            return False
    return True


def verify_linked_source(fact, row, current, descriptor):
    """A later eligibility change cannot conceal damage to the original input."""
    try:
        actual = c.source_pins(row, current)
        frozen = fact.proof['subject']
        if (
            any(actual[k] != frozen[k] for k in c.PINS if k not in ('assignee_id', 'submitter_id'))
            or actual['submitter_id'] not in (None, frozen['submitter_id'])
            or fact.proof['profile'] != descriptor
            or c.input_envelope(row.result_snapshot, frozen, current)[1] != fact.proof['input_hash']
        ):
            c.fail()
    except (APIException, KeyError, TypeError, ValueError, UnicodeError):
        c.fail()


def materialize(current, args, *, lock=False, build=True):
    project = current[2]
    registry = c.selected_profile(project, args['priority'])
    budget = c.metadata_budget()
    budget.add(('registry', registry['digest']), registry['descriptor'], 0)
    query, identifiers = service.candidate_rows(project, args)
    if lock:
        metadata = list(query.filter(pk__in=identifiers).values_list('assignment__task_id', 'assignment_id'))
        for model, keys in (
            (Task, {t for t, _ in metadata}),
            (TaskAssignment, {a for _, a in metadata}),
            (Submission, set(identifiers)),
        ):
            locked = model.objects.filter(pk__in=keys)
            locked = (
                locked.filter(assignment__project=project, assignment__task__project=project)
                if model is Submission
                else locked.filter(project=project)
            )
            list(locked.order_by('pk').select_for_update().values_list('pk', flat=True))
    loaded = load(query.filter(pk__in=identifiers))
    if {row.pk for row in loaded} != set(identifiers):
        service.fail('REVIEW_BATCH_PREVIEW_CHANGED')
    facts = observations.rows(
        ReviewConfidenceObservation.objects.filter(
            project=project,
            instance_id=c.instance_id(),
            profile_id=args['priority']['profile_id'],
            profile_digest=args['priority']['profile_digest'],
            submission_id_frozen__in=identifiers,
        ),
        budget,
        lock=lock,
    )
    by_source = {}
    for fact in facts:
        if fact.submission_id_frozen in by_source:
            c.fail()
        if fact.proof['organization_id'] != project.organization_id:
            c.fail()
        by_source[fact.submission_id_frozen] = fact
    candidates, evidence = [], []
    for row in loaded:
        candidates.append(service.identity(row))
        state, reason, packet, pin = 'unknown', 'not_covered', None, None
        fact = by_source.get(row.pk)
        if fact is not None and fact.submission_id is not None:
            verify_linked_source(fact, row, current, registry['descriptor'])
        supported = c.supported(row)
        if not supported:
            reason = 'unsupported_input'
        elif row.assignment.assignee_id != row.submitted_by_id or row.submitted_by_id is None:
            reason = 'revision_binding_changed'
        else:
            _, input_hash = c.envelope(row, current)
            pin = c.subject(row, current)
            if fact is not None:
                if fact.submission_id is None:
                    reason = 'source_unavailable'
                elif fact.proof['subject'] != pin:
                    # Legitimate reassignment was handled above. A changed
                    # frozen result/config/task pin is corruption, not low risk.
                    c.fail()
                elif fact.proof['input_hash'] != input_hash or fact.proof['profile'] != registry['descriptor']:
                    c.fail()
                else:
                    state = (
                        'low_confidence'
                        if fact.proof['score_units'] < args['priority']['threshold_units']
                        else 'not_low_confidence'
                    )
                    reason = 'frozen_' + state
                    packet = {
                        'id': str(fact.pk),
                        'proof': fact.proof,
                        'fact_hash': fact.fact_hash,
                        'receipt_hash': fact.receipt_hash,
                    }
        evidence.append(
            {
                'submission_id': row.pk,
                'assignee_id': row.assignment.assignee_id,
                'state': state,
                'reason': reason,
                'subject': pin,
                'observation': packet,
            }
        )
    principal, actor = current[:2]
    p = {
        'schema': SCHEMA,
        'kind': KIND,
        'instance_id': c.instance_id(),
        'organization_id': project.organization_id,
        'profile': registry['descriptor'],
        'profile_digest': registry['digest'],
        'threshold_units': args['priority']['threshold_units'],
        'creator': {'principal': principal.principal_id, 'source': principal.source, 'actor_id': actor.pk},
        'evidence': evidence,
    }
    rule = {k: args[k] for k in ('rate', 'per_submitter', 'seed', 'package_ids', 'submitter_ids')}
    proof = {'project_id': project.pk, 'rule': rule, 'candidates': candidates, 'priority': p}
    cost = {
        'snapshot_stored_bytes': sum(row.ner_row_size for row in loaded),
        'snapshot_canonical_bytes': sum(c.bounded(row.result_snapshot, c.MAX_SINGLE) for row in loaded),
        'metadata_stored_bytes': budget.stored,
        'metadata_canonical_bytes': budget.canonical,
        'candidate_count': len(candidates),
        'fact_count': len(facts),
    }
    prepared = {'source_hash': c.digest(proof), 'cost': cost}
    plan = assemble(project.pk, args, candidates, p) if build else None
    if plan is not None:
        validate(plan)
    return plan, prepared


def validate(plan, batch=None):
    try:
        c.bounded(plan, c.MAX_PLAN)
        p, candidates, rule = plan['priority'], plan['candidates'], plan['rule']
        descriptor = c.descriptor(p['profile'])
        valid = (
            set(plan) == {'algorithm', 'project_id', 'rule', 'candidates', 'priority'}
            and plan['algorithm'] == ALGORITHM
            and c.positive(plan['project_id'])
            and set(p)
            == {
                'schema',
                'kind',
                'instance_id',
                'organization_id',
                'profile',
                'profile_digest',
                'threshold_units',
                'creator',
                'evidence',
            }
            and p['schema'] == SCHEMA
            and p['kind'] == KIND
            and c.uuid_string(p['instance_id'])
            and c.positive(p['organization_id'])
            and c.digest(descriptor) == p['profile_digest']
            and c.integer(p['threshold_units'])
            and set(p['creator']) == {'principal', 'source', 'actor_id'}
            and c.text(p['creator']['principal'], 512)
            and c.text(p['creator']['source'], 128)
            and c.positive(p['creator']['actor_id'])
            and isinstance(candidates, list)
            and 1 <= len(candidates) <= 1000
            and [item['submission_id'] for item in candidates]
            == sorted({item['submission_id'] for item in candidates})
            and set(rule) == {'rate', 'per_submitter', 'seed', 'package_ids', 'submitter_ids'}
            and c.integer(rule['rate'], 1, 100)
            and type(rule['per_submitter']) is bool
            and c.text(rule['seed'], 512)
            and len(rule['seed']) <= 128
            and all(
                isinstance(rule[k], list)
                and len(rule[k]) <= 100
                and rule[k] == sorted(set(rule[k]))
                and all(c.positive(n) for n in rule[k])
                for k in ('package_ids', 'submitter_ids')
            )
            and isinstance(p['evidence'], list)
            and [e['submission_id'] for e in p['evidence']] == [item['submission_id'] for item in candidates]
        )
        for item, e in zip(candidates, p['evidence']):
            valid = valid and (
                set(item)
                == {'submission_id', 'assignment_id', 'task_id', 'revision', 'result_hash', 'submitter_id', 'selected'}
                and all(c.positive(item[k]) for k in ('submission_id', 'assignment_id', 'task_id', 'revision'))
                and (item['submitter_id'] is None or c.positive(item['submitter_id']))
                and c.hexadecimal(item['result_hash'])
                and type(item['selected']) is bool
                and set(e) == {'submission_id', 'assignee_id', 'state', 'reason', 'subject', 'observation'}
                and (e['assignee_id'] is None or c.positive(e['assignee_id']))
            )
            if e['subject'] is not None:
                pin = e['subject']
                valid = valid and (
                    set(pin) == set(c.PINS)
                    and all(c.positive(pin[k]) for k in c.PINS[:6])
                    and all(c.hexadecimal(pin[k]) for k in c.PINS[6:])
                    and all(pin[k] == v for k, v in item.items() if k != 'selected')
                    and pin['assignee_id'] == e['assignee_id']
                )
            if e['state'] == 'unknown':
                valid = valid and e['reason'] in UNKNOWN and e['observation'] is None
            else:
                packet, fact = e['observation'], e['observation']['proof']
                valid = valid and (
                    set(packet) == {'id', 'proof', 'fact_hash', 'receipt_hash'}
                    and set(fact)
                    == {
                        'schema',
                        'observation_id',
                        'instance_id',
                        'organization_id',
                        'project_id',
                        'profile',
                        'profile_digest',
                        'subject',
                        'input_hash',
                        'score_units',
                        'producer_namespace',
                        'event_uuid',
                        'evaluated_at',
                        'accepted_at',
                    }
                    and fact['schema'] == c.FACT
                    and c.uuid_string(packet['id'])
                    and packet['id'] == fact['observation_id']
                    and fact['instance_id'] == p['instance_id']
                    and fact['organization_id'] == p['organization_id']
                    and fact['project_id'] == plan['project_id']
                    and fact['profile'] == descriptor
                    and fact['profile_digest'] == p['profile_digest']
                    and fact['subject'] == e['subject']
                    and fact['producer_namespace'] == descriptor['producer_namespace']
                    and c.uuid_string(fact['event_uuid'])
                    and c.hexadecimal(fact['input_hash'])
                    and c.integer(fact['score_units'])
                    and observations.utc(fact['evaluated_at']) == fact['evaluated_at']
                    and observations.utc(fact['accepted_at']) == fact['accepted_at']
                    and c.digest(fact) == packet['fact_hash']
                    and c.hexadecimal(packet['receipt_hash'])
                    and e['state']
                    == ('low_confidence' if fact['score_units'] < p['threshold_units'] else 'not_low_confidence')
                    and e['reason'] == 'frozen_' + e['state']
                )
        if valid:
            valid = selected_prefix_valid(plan)
        if batch is not None:
            valid = valid and (
                p['creator']['principal'] == batch.creator_principal
                and p['creator']['actor_id'] == batch.actor_id_frozen
                and plan['project_id'] == batch.project_id
                and p['organization_id'] == batch.project.organization_id
            )
    except APIException as exc:
        if exc.status_code == 422:
            raise
        valid = False
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        c.fail()


def summary(plan, candidates):
    states = {e['submission_id']: e['state'] for e in plan['priority']['evidence']}
    low = [item for item in candidates if states[item['submission_id']] == 'low_confidence']
    known = sum(states[item['submission_id']] != 'unknown' for item in candidates)
    selected_low = sum(item['selected'] for item in low)
    return {
        'kind': KIND,
        'known_candidates': known,
        'unknown_candidates': len(candidates) - known,
        'low_confidence_candidates': len(low),
        'selected_low_confidence': selected_low,
        'uncovered_low_confidence': len(low) - selected_low,
        'fallback_selected': sum(item['selected'] for item in candidates) - selected_low,
    }


def public_plan(plan):
    p = plan['priority']
    return {
        **{k: copy.deepcopy(plan[k]) for k in ('algorithm', 'project_id', 'rule', 'candidates')},
        'priority': {
            'schema': SCHEMA,
            'kind': KIND,
            'profile': c.public_profile(p['profile']),
            'profile_digest': p['profile_digest'],
            'threshold_units': p['threshold_units'],
            'summary': summary(plan, plan['candidates']),
            'evidence': [
                {
                    'submission_id': e['submission_id'],
                    'state': e['state'],
                    'reason': e['reason'],
                    'score_units': e['observation']['proof']['score_units'] if e['observation'] else None,
                    'fact_id': e['observation']['id'] if e['observation'] else None,
                    'fact_hash': e['observation']['fact_hash'] if e['observation'] else None,
                }
                for e in p['evidence']
            ],
        },
    }


def projection(plan, items, role):
    p = plan['priority']
    if role == 'manager':
        by_id = {e['submission_id']: e for e in p['evidence']}
        for item in items:
            e = by_id[item['submission_id']]
            item['confidence_score_units'] = e['observation']['proof']['score_units'] if e['observation'] else None
        return {
            'priority_source': {
                'schema': SCHEMA,
                'kind': KIND,
                'profile': c.public_profile(p['profile']),
                'profile_digest': p['profile_digest'],
                'threshold_units': p['threshold_units'],
            }
        }
    return {}


def linked_sources(batch, query, *, budget=None):
    """Nullable original links cannot rebind to a recreated numeric ID."""
    ids = set(query.values_list('pk', flat=True))
    entries = [e for e in batch.plan['priority']['evidence'] if e['submission_id'] in ids and e['observation']]
    observations = ReviewConfidenceObservation.objects.filter(
        project_id=batch.project_id, pk__in=[e['observation']['id'] for e in entries]
    )
    if budget is None:
        current = {
            str(row['pk']): row
            for row in observations.values('pk', 'submission_id', 'fact_hash', 'receipt_hash', 'instance_id')
        }
    else:
        current = {
            str(row.pk): dict(
                pk=row.pk,
                submission_id=row.submission_id,
                fact_hash=row.fact_hash,
                receipt_hash=row.receipt_hash,
                instance_id=row.instance_id,
            )
            for row in budget.rows(
                observations.only('id', 'submission_id', 'fact_hash', 'receipt_hash', 'instance_id'),
                {'fact_hash': (66, 64), 'receipt_hash': (66, 64)},
            )
        }
    unavailable = set()
    for e in entries:
        packet = e['observation']
        row = current.get(packet['id'])
        if row is None or row['submission_id'] != e['submission_id']:
            unavailable.add(e['submission_id'])
        elif (
            row['fact_hash'] != packet['fact_hash']
            or row['receipt_hash'] != packet['receipt_hash']
            or str(row['instance_id']) != batch.plan['priority']['instance_id']
        ):
            c.fail()
    return query.exclude(pk__in=unavailable)


def check_sources(batch, rows):
    for e in batch.plan['priority']['evidence']:
        row = rows.get(e['submission_id'])
        if row is not None and (
            row.assignment.assignee_id != e['assignee_id']
            or e['subject'] is not None
            and c.subject(row, (None, None, batch.project, None)) != e['subject']
        ):
            rows.pop(row.pk)


def processing(prepared=None):
    if prepared is None:
        return {'materialization_passes': 0, 'sorting_passes': 0, 'per_pass': {}, 'total': {}, 'scorer_calls': 0}
    cost = prepared['cost']
    return {
        'materialization_passes': 3,
        'sorting_passes': 2,
        'per_pass': cost,
        'total': {k: 3 * v for k, v in cost.items()},
        'scorer_calls': 0,
    }


def response(current, data, prepared=None, status=200):
    from .priority import viewer_hash

    return observations.response(
        {
            **data,
            'project_id': current[2].pk,
            'schema': SCHEMA,
            'kind': KIND,
            'algorithm': ALGORITHM,
            'actor_id': current[1].pk,
            'owner_hash': c.owner_hash(current),
            'viewer_hash': viewer_hash(*current),
            'processing_cost': processing(prepared),
        },
        status=status,
    )


def verify(request, initial, args, prepared, *, lock=False):
    current = c.context(request, initial, create=True)
    _, repeated = materialize(current, args, lock=lock, build=False)
    if repeated != prepared:
        service.fail('REVIEW_BATCH_PREVIEW_CHANGED')
    c.context(request, initial, create=True)


def preview(request, pk, principal, actor, args):
    current = c.guard(pk, principal, actor, create=True)
    plan, prepared = materialize(current, args)
    replay, repeated = materialize(c.context(request, current, create=True), args)
    if plan != replay or prepared != repeated:
        service.fail('REVIEW_BATCH_PREVIEW_CHANGED')
    result = response(current, {'plan': public_plan(plan), 'plan_hash': service.digest(plan)}, prepared)
    verify(request, current, args, prepared)
    observations.check_response(result)
    c.context(request, current, create=True)
    return result


def create(request, pk, principal, args, expected, key, request_hash):
    try:
        return _create(request, pk, principal, args, expected, key, request_hash)
    except APIException as exc:
        if getattr(exc, 'display_context', {}).get('reason') == 'REVIEW_BATCH_EMPTY':
            service.fail('REVIEW_BATCH_PREVIEW_CHANGED')
        raise
    except IntegrityError as exc:
        name = getattr(getattr(exc.__cause__, 'diag', None), 'constraint_name', None)
        sqlite = 'UNIQUE constraint failed: review_batches_reviewbatch.project_id, review_batches_reviewbatch.idempotency_key'
        if name == 'review_batch_project_key_unique' or str(exc) == sqlite:
            service.fail('REVIEW_BATCH_KEY_CONFLICT')
        raise


def _create(request, pk, principal, args, expected, key, request_hash):
    from . import guarded

    with transaction.atomic():
        project, actor = lock_project_operation(pk, principal, ORGANIZE_REVIEW)
        initial = c.guard(pk, principal, actor, create=True)
        previous = guarded.query(project).filter(idempotency_key=key).first()
        if previous is not None:
            previous = service.check_loaded_batch(guarded.hydrate(previous), project)
            if (
                not is_plan(previous.plan)
                or previous.request_hash != request_hash
                or previous.creator_principal != principal.principal_id
                or previous.actor_id_frozen != actor.pk
                or previous.plan['priority']['creator']['source'] != principal.source
            ):
                service.fail('REVIEW_BATCH_KEY_CONFLICT')
            read_guard(pk, principal, actor, previous.plan)
            result = response(
                initial,
                {
                    **service.metadata(previous),
                    'created_at': previous.created_at.isoformat(),
                    'idempotency_key': str(key),
                    'request_hash': request_hash,
                    'replayed': True,
                },
            )
            c.context(request, initial, create=True)
            service.checked_batch(project, previous.pk)
            observations.check_response(result)
            return result
        plan, prepared = materialize(initial, args, lock=True)
        if service.digest(plan) != expected:
            service.fail('REVIEW_BATCH_PREVIEW_CHANGED')
        replay, repeated = materialize(c.context(request, initial, create=True), args, lock=True)
        if plan != replay or prepared != repeated:
            service.fail('REVIEW_BATCH_PREVIEW_CHANGED')
        batch = ReviewBatch.objects.create(
            project=project,
            created_by=actor,
            actor_id_frozen=actor.pk,
            creator_principal=principal.principal_id,
            name=args['name'],
            idempotency_key=key,
            request_hash=request_hash,
            plan=plan,
            plan_hash=expected,
        )
        result = response(
            initial,
            {
                **service.metadata(batch),
                'created_at': batch.created_at.isoformat(),
                'idempotency_key': str(key),
                'request_hash': request_hash,
                'replayed': False,
            },
            prepared,
            201,
        )
        verify(request, initial, args, prepared, lock=True)
        observations.check_response(result)
        c.context(request, initial, create=True)
        return result
