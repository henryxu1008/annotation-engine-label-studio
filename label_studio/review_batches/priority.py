"""Optional frozen classification evidence for selection, never review authority."""

import json
import re
from types import SimpleNamespace

from access_control.identity import resolve_actor
from access_control.project_operations import ORGANIZE_REVIEW, lock_project_operation, require_project_operation
from django.conf import settings
from django.db import connection, transaction
from django.utils.crypto import salted_hmac
from rest_framework.exceptions import APIException
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.utils.encoders import JSONEncoder

from . import service
from .models import ReviewBatch

ALGORITHM = 'classification-conflict-ranked/v1'
SCHEMA = 'review-classification-priority/v1'


def viewer_hash(principal, actor, project, role):
    value = service.digest(
        {
            'principal': principal.principal_id,
            'source': principal.source,
            'actor': actor.pk,
            'project': project.pk,
            'organization': project.organization_id,
            'role': role,
        }
    )
    return salted_hmac(SCHEMA + '/viewer', value, algorithm='sha256').hexdigest()


def read_guard(project_id, principal, actor, plan=None):
    from . import field_priority, ner_priority

    if field_priority.is_plan(plan):
        project, role = field_priority.read_guard(project_id, principal, actor, plan)
        if role == 'reviewer' and not service.visible_candidates(SimpleNamespace(plan=plan), actor, role):
            from rest_framework.exceptions import NotFound

            raise NotFound()
        return project, role
    if ner_priority.is_plan(plan):
        project, role = ner_priority.read_guard(project_id, principal, actor)
        if role == 'reviewer':
            hidden = {t['task_id'] for t in plan['priority']['participants'] if actor.pk in t['actor_ids']}
            if not any(c['submitter_id'] != actor.pk and c['task_id'] not in hidden for c in plan['candidates']):
                from rest_framework.exceptions import NotFound

                raise NotFound()
        return project, role
    from classification_disagreements import service as comparison

    if not settings.ANNOTATION_CLASSIFICATION_BATCHES_ENABLED or connection.vendor not in ('postgresql', 'sqlite'):
        service.fail('REVIEW_PRIORITY_UNAVAILABLE', 403)
    return comparison.guard(project_id, principal, actor)


def guard(project_id, principal, actor):
    project, role = read_guard(project_id, principal, actor)
    if role != 'manager':
        service.fail('REVIEW_PRIORITY_FORBIDDEN', 403)
    return project


def recheck_read(request, pk, principal, actor, role, batch):
    current, current_actor = resolve_actor(request)
    project, current_role, _ = service.guard(pk, current, current_actor)
    read_guard(pk, current, current_actor, batch.plan)
    if current != principal or current_actor.pk != actor.pk or current_role != role:
        service.fail('REVIEW_PRIORITY_AUTHORITY_CHANGED', 403)
    if service.checked_batch(project, batch.pk).plan_hash != batch.plan_hash:
        service.fail('REVIEW_BATCH_PLAN_INVALID')


def available(project, principal, actor):
    try:
        guard(project.pk, principal, actor)
        return True
    except APIException:
        return False


def read_available(project, principal, actor):
    try:
        read_guard(project.pk, principal, actor)
        return True
    except APIException:
        return False


def context(request, pk, principal, actor_id):
    current, actor = resolve_actor(request)
    if current != principal or actor.pk != actor_id:
        service.fail('REVIEW_PRIORITY_AUTHORITY_CHANGED', 403)
    project, _, _ = service.guard(pk, current, actor, manager=True)
    require_project_operation(project, ORGANIZE_REVIEW)
    guard(pk, current, actor)
    return project


def freeze(project, candidates, selection):
    if selection.get('kind') != 'classification_conflict':
        service.fail('REVIEW_BATCH_INVALID_INPUT', 400)
    from classification_disagreements import batches
    from classification_disagreements import service as comparison
    from classification_disagreements.models import ClassificationBatch

    batch = ClassificationBatch.objects.filter(pk=selection['batch_id'], project=project).first()
    if batch is None:
        service.fail('REVIEW_PRIORITY_SOURCE_UNAVAILABLE')
    batch = batches.checked(batch, project)
    if batch.manifest_hash != selection['manifest_hash']:
        service.fail('REVIEW_PRIORITY_MANIFEST_CHANGED')
    tasks = batch.manifest['tasks']
    if (
        len(tasks) > batches.MAX_TASKS
        or any(len(t['cohort']) > batches.MAX_PARTICIPANTS for t in tasks)
        or sum(len(t['cohort']) for t in tasks) > batches.MAX_INPUTS
    ):
        service.fail('REVIEW_PRIORITY_TOO_LARGE', 422)
    reports = {
        r['task_id']: r
        for r in batches.project_items(
            batch,
            None,
            'manager',
            strict=True,
            lock=connection.in_atomic_block,
        )
    }
    if any(r['reason'] in ('source_unavailable', 'source_integrity_failed') for r in reports.values()):
        service.fail('REVIEW_PRIORITY_SOURCE_UNAVAILABLE')
    frozen = {t['task_id']: t for t in tasks}
    evidence = []
    for candidate in candidates:
        item = frozen.get(candidate['task_id'])
        state, reason, calculation = 'unknown', 'not_covered', None
        if item is not None:
            pin = next(
                (
                    e['source']
                    for e in item['cohort']
                    if e['source'] is not None and all(e['source'].get(k) == v for k, v in candidate.items())
                ),
                None,
            )
            reason = 'revision_mismatch'
            if pin is not None:
                report = reports[candidate['task_id']]
                calculation = item['result']['calculation_hash']
                if report['status'] == 'comparable':
                    state = 'conflict' if report['matching_pairs'] < report['total_pairs'] else 'agreement'
                    reason = 'frozen_' + state
                else:
                    reason = 'metric_unavailable'
        evidence.append(
            {
                'submission_id': candidate['submission_id'],
                'state': state,
                'reason': reason,
                'calculation_hash': calculation,
            }
        )
    return {
        'schema': SCHEMA,
        'batch_id': batch.pk,
        'manifest_hash': batch.manifest_hash,
        'metric_rule': comparison.RULE,
        'selection': batches.SELECTION,
        'evidence': evidence,
        'participants': [
            {
                'task_id': t['task_id'],
                'actor_ids': sorted(
                    {
                        v
                        for e in t['cohort']
                        for v in (e['assignee_id'], (e['source'] or {}).get('submitter_id'))
                        if v is not None
                    }
                ),
            }
            for t in tasks
        ],
    }


def validate(plan, batch=None):
    """Receipt recovery checks local frozen structure, without needing live sources."""
    from . import field_priority, ner_priority

    if field_priority.is_plan(plan):
        return field_priority.validate(plan, batch)
    if ner_priority.is_plan(plan):
        return ner_priority.validate(plan, batch)
    try:
        p = plan['priority']
        evidence = p['evidence']
        valid = (
            plan['algorithm'] == ALGORITHM
            and p['schema'] == SCHEMA
            and type(p['batch_id']) is int
            and p['batch_id'] > 0
            and isinstance(p['manifest_hash'], str)
            and re.fullmatch(r'[0-9a-f]{64}', p['manifest_hash']) is not None
            and p['metric_rule'] == 'classification-exact-sets/v1'
            and p['selection'] == 'active-assignment-latest/v1'
            and isinstance(p['participants'], list)
            and len(p['participants']) <= 100
            and len({t['task_id'] for t in p['participants']}) == len(p['participants'])
            and all(
                type(t['task_id']) is int
                and t['task_id'] > 0
                and isinstance(t['actor_ids'], list)
                and all(type(v) is int and v > 0 for v in t['actor_ids'])
                for t in p['participants']
            )
            and [e['submission_id'] for e in evidence] == [c['submission_id'] for c in plan['candidates']]
            and all(
                (e['state'], e['reason'])
                in {
                    ('conflict', 'frozen_conflict'),
                    ('agreement', 'frozen_agreement'),
                    ('unknown', 'not_covered'),
                    ('unknown', 'revision_mismatch'),
                    ('unknown', 'metric_unavailable'),
                }
                for e in evidence
            )
            and all(
                e['calculation_hash'] is None
                if e['reason'] in ('not_covered', 'revision_mismatch')
                else isinstance(e['calculation_hash'], str) and re.fullmatch(r'[0-9a-f]{64}', e['calculation_hash'])
                for e in evidence
            )
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        service.fail('REVIEW_BATCH_PLAN_INVALID')


def summary(plan, candidates):
    states = {e['submission_id']: e['state'] for e in plan['priority']['evidence']}
    conflicts = [c for c in candidates if states[c['submission_id']] == 'conflict']
    return {
        'conflict_candidates': len(conflicts),
        'selected_conflicts': sum(c['selected'] for c in conflicts),
        'unknown_candidates': sum(states[c['submission_id']] == 'unknown' for c in candidates),
    }


def response(request, pk, principal, actor_id, data, status=200):
    emitted = json.loads(json.dumps(data, cls=JSONEncoder, ensure_ascii=False, allow_nan=False))
    result = Response(data, status=status)
    result.accepted_renderer = JSONRenderer()
    result.accepted_media_type = 'application/json'
    result.renderer_context = {}
    result.render()
    if json.loads(result.content) != emitted:
        service.fail('REVIEW_PRIORITY_REPRESENTATION_CHANGED')
    result['Referrer-Policy'] = 'no-referrer'
    context(request, pk, principal, actor_id)
    return result


def create(request, pk, principal, args, expected, key, request_hash):
    with transaction.atomic():
        project, actor = lock_project_operation(pk, principal, ORGANIZE_REVIEW)
        context(request, pk, principal, actor.pk)
        previous = ReviewBatch.objects.filter(project=project, idempotency_key=key).defer('plan').first()
        if previous:
            if (
                previous.request_hash != request_hash
                or previous.creator_principal != principal.principal_id
                or previous.actor_id_frozen != actor.pk
            ):
                service.fail('REVIEW_BATCH_KEY_CONFLICT')
            service.checked_batch(project, previous.pk)
            return response(
                request,
                pk,
                principal,
                actor.pk,
                {**service.metadata(previous), 'replayed': True, 'idempotency_key': str(key)},
            )
        try:
            plan = service.build_plan(project, args)
        except APIException as exc:
            if getattr(exc, 'display_context', {}).get('reason') == 'REVIEW_BATCH_EMPTY':
                service.fail('REVIEW_BATCH_PREVIEW_CHANGED')
            raise
        if service.digest(plan) != expected:
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
            request,
            pk,
            principal,
            actor.pk,
            {**service.metadata(batch), 'replayed': False, 'idempotency_key': str(key)},
            201,
        )
        if service.digest(service.build_plan(project, args)) != expected:
            service.fail('REVIEW_BATCH_PREVIEW_CHANGED')
        context(request, pk, principal, actor.pk)
        return result
