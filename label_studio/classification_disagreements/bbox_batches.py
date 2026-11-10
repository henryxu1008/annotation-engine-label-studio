"""Immutable bbox quality batch planning and scoped reads."""

from uuid import UUID

from access_control.authorization import authorization
from access_control.project_operations import (
    ORGANIZE_BBOX,
    READ_WORK,
    lock_project_operation,
    require_project_operation,
)
from django.conf import settings
from django.core import signing
from django.db import connection, models, transaction
from django.db.models import Case, F, JSONField, Value, When
from django.db.models.expressions import RawSQL
from django.utils import timezone
from rest_framework.exceptions import NotFound

from . import bbox, bbox_quality, ner_api, ner_batches
from .models import BboxQualityBatch

SCHEMA = 'bbox-quality-batch/v1'
SALT = 'bbox-quality-preview/v1'
PREVIEW_AGE = 15 * 60
MAX_TOKEN = 16384
MAX_STORED_BYTES = 4 * bbox_quality.MAX_PLAN_BYTES


def guard(project, principal):
    key = f'{project.organization_id}:{project.pk}'
    modes = settings.ANNOTATION_BBOX_QUALITY_MODES
    media = settings.ANNOTATION_BBOX_MEDIA_PROJECTS
    if (connection.vendor not in ('postgresql', 'sqlite') or not settings.ANNOTATION_BBOX_QUALITY_ENABLED
            or not settings.ANNOTATION_BBOX_MEDIA_ENABLED or not isinstance(modes, dict)
            or not isinstance(media, dict) or modes.get(key) != 'fork_review' or media.get(key) != 'managed'
            or not authorization.can_manage_project(principal, project)):
        raise NotFound()
    require_project_operation(project, READ_WORK)
    require_project_operation(project, ORGANIZE_BBOX)


def preview(project, principal, *, name, task_ids):
    guard(project, principal)
    name = ner_batches.validated_text(name, 160, trim=True)
    if not name:
        raise bbox.Error('BBOX_BATCH_INVALID_INPUT', 400)
    plan = bbox_quality.build_plan(project, task_ids)
    scope_enumerated_at = timezone.now().isoformat()
    payload = {'schema': SCHEMA, 'project_id': project.pk, 'organization_id': project.organization_id,
               'actor_id': principal.local_user_id, 'principal': principal.principal_id, 'source': principal.source,
               'name': name, 'task_ids': plan['task_ids'], 'plan_hash': bbox.digest(plan),
               'scope_enumerated_at': scope_enumerated_at}
    token = signing.dumps(payload, salt=SALT, compress=False)
    if len(token.encode()) > MAX_TOKEN:
        raise bbox.Error('BBOX_BATCH_TOO_LARGE', 422)
    guard(project, principal)
    return {'token': token, 'plan_hash': payload['plan_hash'], 'task_ids': plan['task_ids'],
            'scope_enumerated_at': scope_enumerated_at, 'display_counts': bbox_quality.counts(plan['items'])}


def query(project):
    column = connection.ops.quote_name(BboxQualityBatch._meta.db_table) + '.' + connection.ops.quote_name('manifest')
    length = (f'OCTET_LENGTH(CAST({column} AS TEXT))' if connection.vendor == 'postgresql'
              else f'LENGTH(CAST({column} AS BLOB))')
    return BboxQualityBatch.objects.filter(project=project).annotate(
        _bbox_size=RawSQL(length, [], output_field=models.BigIntegerField())
    ).annotate(_bbox_manifest=Case(When(_bbox_size__lte=MAX_STORED_BYTES, then=F('manifest')),
                                   default=Value(None), output_field=JSONField())).defer('manifest')


def visible_query(project, actor, role):
    """Filter hidden cohorts in SQL before counting or paginating."""
    rows = query(project)
    if role == 'reviewer':
        if connection.vendor == 'postgresql':
            items = (f"CASE WHEN OCTET_LENGTH(manifest::text)<={MAX_STORED_BYTES} "
                     "AND jsonb_typeof(manifest->'plan'->'items')='array' "
                     "THEN manifest->'plan'->'items' ELSE '[]'::jsonb END")
            expression = f"""EXISTS (SELECT 1 FROM jsonb_array_elements({items}) item
                WHERE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(item->'cohort')='array' THEN item->'cohort' ELSE '[]'::jsonb END) member
                WHERE member->>'assignee_id'=%s OR member->'source'->>'submitter_id'=%s))"""
            args = (str(actor.pk), str(actor.pk))
        else:
            items = (f"CASE WHEN LENGTH(CAST(manifest AS BLOB))<={MAX_STORED_BYTES} "
                     "AND json_type(manifest,'$.plan.items')='array' "
                     "THEN json_extract(manifest,'$.plan.items') ELSE '[]' END")
            expression = f"""EXISTS (SELECT 1 FROM json_each({items}) item WHERE NOT EXISTS
                (SELECT 1 FROM json_each(CASE WHEN json_valid(item.value) THEN
                 CASE WHEN json_type(item.value,'$.cohort')='array' THEN json_extract(item.value,'$.cohort')
                 ELSE '[]' END ELSE '[]' END) member
                 WHERE CASE WHEN json_valid(member.value) THEN json_extract(member.value,'$.assignee_id') END=%s
                 OR CASE WHEN json_valid(member.value) THEN json_extract(member.value,'$.source.submitter_id') END=%s))"""
            args = (actor.pk, actor.pk)
        rows = rows.annotate(_visible=RawSQL(expression, args, output_field=models.BooleanField())).filter(_visible=True)
    elif role != 'manager':
        return rows.none()
    return rows.order_by('-pk')


def checked(batch, project):
    try:
        if type(batch._bbox_size) is not int or not 0 <= batch._bbox_size <= MAX_STORED_BYTES:
            raise ValueError('size')
        manifest = batch._bbox_manifest
        bbox.bounded(manifest, bbox_quality.MAX_PLAN_BYTES)
        if (not isinstance(manifest, dict) or set(manifest) != {'schema', 'project_id', 'organization_id', 'actor_id',
                'principal', 'source', 'name', 'plan', 'calculated_at', 'scope_enumerated_at'} or manifest['schema'] != SCHEMA
                or manifest['project_id'] != project.pk or manifest['organization_id'] != project.organization_id
                or manifest['actor_id'] != batch.actor_id_frozen or manifest['principal'] != batch.creator_principal
                or manifest['source'] != batch.creator_source or manifest['name'] != batch.name
                or bbox.digest(manifest) != batch.manifest_hash):
            raise ValueError('manifest')
        batch.manifest = manifest
        return batch
    except (ValueError, TypeError, KeyError, AttributeError, bbox.Error) as exc:
        raise bbox.Error('BBOX_BATCH_MANIFEST_INVALID', 409) from exc


def create(project, principal, *, token, idempotency_key, expected_plan_hash=None):
    guard(project, principal)
    try:
        key = UUID(str(idempotency_key))
        if (not isinstance(token, str) or len(token.encode()) > MAX_TOKEN or token.startswith('.')
                or expected_plan_hash is not None and (not isinstance(expected_plan_hash, str)
                                                        or not ner_api.HEX.fullmatch(expected_plan_hash))):
            raise ValueError('token')
    except (ValueError, TypeError, AttributeError) as exc:
        raise bbox.Error('BBOX_BATCH_PREVIEW_INVALID', 400) from exc
    request = {'token': token}
    if expected_plan_hash is not None:
        request['expected_plan_hash'] = expected_plan_hash
    request_hash = bbox.digest(request)
    with transaction.atomic():
        project, actor = lock_project_operation(project.pk, principal, ORGANIZE_BBOX)
        guard(project, principal)
        previous = query(project).filter(idempotency_key=key).first()
        if previous:
            previous = checked(previous, project)
            if (previous.request_hash != request_hash or previous.actor_id_frozen != actor.pk
                    or previous.creator_principal != principal.principal_id or previous.creator_source != principal.source):
                raise bbox.Error('BBOX_BATCH_KEY_CONFLICT', 409)
            return previous, True
        payload, name = decode_preview(project, principal, token)
        if expected_plan_hash is not None and payload['plan_hash'] != expected_plan_hash:
            raise bbox.Error('BBOX_BATCH_PREVIEW_INVALID', 400)
        plan = bbox_quality.build_plan(project, payload['task_ids'], lock=True)
        if bbox.digest(plan) != payload['plan_hash']:
            raise bbox.Error('BBOX_BATCH_PREVIEW_CHANGED', 409)
        manifest = {'schema': SCHEMA, 'project_id': project.pk, 'organization_id': project.organization_id,
                    'actor_id': actor.pk, 'principal': principal.principal_id, 'source': principal.source,
                    'name': name, 'plan': plan, 'calculated_at': timezone.now().isoformat(),
                    'scope_enumerated_at': payload['scope_enumerated_at']}
        bbox.bounded(manifest, bbox_quality.MAX_PLAN_BYTES)
        batch = BboxQualityBatch.objects.create(
            project=project, created_by=actor, creator_principal=principal.principal_id,
            creator_source=principal.source, actor_id_frozen=actor.pk, name=name, idempotency_key=key,
            request_hash=request_hash, manifest=manifest, manifest_hash=bbox.digest(manifest),
        )
        guard(project, principal)
        return batch, False


def decode_preview(project, principal, token):
    try:
        payload = signing.loads(token, salt=SALT, max_age=PREVIEW_AGE)
        expected = {'schema', 'project_id', 'organization_id', 'actor_id', 'principal', 'source', 'name',
                    'task_ids', 'plan_hash', 'scope_enumerated_at'}
        if (not isinstance(payload, dict) or set(payload) != expected or payload['schema'] != SCHEMA
                or payload['project_id'] != project.pk or payload['organization_id'] != project.organization_id
                or payload['actor_id'] != principal.local_user_id or payload['principal'] != principal.principal_id
                or payload['source'] != principal.source):
            raise ValueError('scope')
        name = ner_batches.validated_text(payload['name'], 160, trim=True)
        if not name or name != payload['name']:
            raise ValueError('name')
    except (ValueError, TypeError, KeyError, AttributeError, signing.BadSignature) as exc:
        raise bbox.Error('BBOX_BATCH_PREVIEW_INVALID', 400) from exc
    return payload, name
