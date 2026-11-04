"""Project-fenced audit capture, original-key recovery and frozen-input workers."""
import hashlib
import os
import uuid
from collections import Counter
from datetime import datetime, timedelta
from types import SimpleNamespace

from access_control.authorization import authorization
from access_control.identity import (
    LOCAL_IDENTITY_SOURCE,
    LocalIdentityProvider,
    Principal,
    get_identity_provider,
    resolve_actor,
)
from access_control.project_operations import (
    EXPORT_REVIEW_AUDIT,
    READ_WORK,
    lock_project_operation,
    project_operation_policy,
)
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models import CharField, F, Q, Value
from django.db.models.functions import Cast, Concat
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from projects.models import Project
from rest_framework.exceptions import APIException
from review_batches.second_review_bounded import Budget
from tasks.my_work import work_projects

from . import approved, audit_format, audit_source
from . import audit_contract as c
from .audit_models import (
    AuditAccessEvent,
    AuditActorBinding,
    AuditArtifact,
    AuditAttempt,
    AuditPackage,
    AuditRetryReceipt,
)

PROFILE = c.PROFILE
queue_available = approved.queue_available
ACTIVE = ('dispatch_pending', 'queued', 'running', 'failed_retryable')


def scope(request, project_id, *, resolved=None):
    principal, actor = resolved or resolve_actor(request)
    if (
        not isinstance(principal.principal_id, str)
        or not 1 <= len(principal.principal_id.encode()) <= 512
        or not isinstance(principal.source, str)
        or not 1 <= len(principal.source.encode()) <= 128
    ):
        c.fail('FORBIDDEN', 403)
    project = get_object_or_404(
        Project.objects.filter(pk__in=work_projects(actor, ['manager']), deleted_at__isnull=True), pk=project_id
    )
    if (
        not actor.is_active
        or actor.pk != principal.local_user_id
        or actor.active_organization_id != project.organization_id
        or authorization.project_role(principal, project) != 'manager'
        or connection.vendor not in ('postgresql', 'sqlite')
    ):
        c.fail('FORBIDDEN', 403)
    operation = project_operation_policy(project)
    if not operation.allows(READ_WORK) or not operation.allows(EXPORT_REVIEW_AUDIT):
        c.fail('FORBIDDEN', 403)
    if not isinstance(operation.version, str) or not 1 <= len(operation.version.encode()) <= 128:
        c.fail('POLICY_INVALID', 403)
    provider = get_identity_provider()
    if type(provider) is not LocalIdentityProvider and not callable(getattr(provider, 'resolve_background', None)):
        c.fail('BACKGROUND_UNAVAILABLE', 503)
    policy = c.policy(project)
    policy['_authority'] = dict(version=operation.version, state=operation.state)
    return project, principal, actor, policy


def audit_policy(ctx):
    return {key: value for key, value in ctx[3].items() if key != '_authority'}


def client_owner(ctx):
    # Presentation continuity only; never a substitute for creator generation.
    return c.digest(
        c.exact(
            dict(
                domain='audit-client-owner/v1',
                principal=ctx[1].principal_id,
                source=ctx[1].source,
                actor=ctx[2].pk,
                joined=approved.stamp(ctx[2].date_joined),
                project=ctx[0].pk,
                policy=audit_policy(ctx),
            )
        )
    )


def collection_supported():
    # Background identity resolution alone cannot establish a whole SQL-visible
    # creator collection. A future host adapter needs an explicit contract.
    return type(get_identity_provider()) is LocalIdentityProvider


def visible_packages(ctx):
    if not collection_supported():
        c.fail('COLLECTION_UNAVAILABLE', 503)
    project = ctx[0]
    # Keep the same creator-manager and live membership rules as work_projects.
    from organizations.models import OrganizationMember
    from projects.models import ProjectMember

    members = OrganizationMember.objects.filter(
        organization_id=project.organization_id, deleted_at__isnull=True
    ).values('user_id')
    managers = ProjectMember.objects.filter(project=project, enabled=True, role='manager').values('user_id')
    actors = (
        get_user_model()
        .objects.filter(pk__in=members, is_active=True, active_organization_id=project.organization_id)
        .filter(Q(pk=project.created_by_id) | Q(pk__in=managers))
        .values('pk')
    )
    return AuditPackage.objects.filter(
        project=project,
        organization_id_frozen=project.organization_id,
        instance_id=ctx[3]['instance_id'],
        creator__instance_id=ctx[3]['instance_id'],
        creator__tombstone=False,
        creator__live_user_id__in=actors,
        creator_source=LOCAL_IDENTITY_SOURCE,
        creator_principal=Concat(Value(LOCAL_IDENTITY_SOURCE + ':'), Cast(F('creator__live_user_id'), CharField())),
        access_policy=c.exact(audit_policy(ctx)).decode(),
    )


def history_gate(ctx, rows):
    """One bounded collection authority check, including the cursor lookahead."""
    ids = {row.pk for row in rows}
    if set(visible_packages(ctx).filter(pk__in=ids).values_list('pk', flat=True)) != ids:
        c.fail('AUTHORITY_CHANGED', 403)
    bindings = dict(
        AuditActorBinding.objects.filter(
            pk__in={row.creator_id for row in rows},
            tombstone=False,
            instance_id=ctx[3]['instance_id'],
            live_user__isnull=False,
        ).values_list('pk', 'live_user_id')
    )
    for row in rows:
        actor_id = bindings.get(row.creator_id)
        if actor_id is None:
            c.fail('CREATOR_REVOKED', 403)
        who = SimpleNamespace(pk=row.creator_id, live_user_id=actor_id, tombstone=False)
        principal = Principal(row.creator_principal, row.creator_source, '', actor_id)
        if owner((ctx[0], principal, SimpleNamespace(pk=actor_id), ctx[3]), who) != row.owner_hash:
            c.fail('INPUT_INVALID')


def fence(ctx):
    project, principal, actor, policy = ctx
    if connection.vendor == 'sqlite':
        # A no-op Project claim acquires SQLite's writer reservation before reads.
        Project.all_objects.filter(pk=project.pk).update(id=F('id'))
    project, actor = lock_project_operation(project.pk, principal, EXPORT_REVIEW_AUDIT)
    if actor.date_joined != ctx[2].date_joined:
        c.fail('AUTHORITY_CHANGED', 403)
    fresh = scope(None, project.pk, resolved=(principal, actor))
    if fresh[3] != policy:
        c.fail('POLICY_CHANGED')
    return fresh


def owner(ctx, binding):
    if binding is None or binding.tombstone or binding.live_user_id != ctx[2].pk:
        c.fail('CREATOR_REVOKED', 403)
    project, principal, actor, policy = ctx
    return c.digest(
        c.exact(
            dict(
                generation=str(binding.pk),
                principal=principal.principal_id,
                source=principal.source,
                actor=actor.pk,
                project=project.pk,
                organization=project.organization_id,
                instance=policy['instance_id'],
            )
        )
    )


def viewer(ctx):
    # Presentation epoch only. Permanent creator security uses AuditActorBinding.
    return c.preview_hash(
        'viewer',
        dict(
            principal=ctx[1].principal_id,
            source=ctx[1].source,
            actor=ctx[2].pk,
            joined=approved.stamp(ctx[2].date_joined),
            project=ctx[0].pk,
            policy=ctx[3],
        ),
    )


def binding(ctx, *, create=False):
    row = AuditActorBinding.objects.filter(
        instance_id=ctx[3]['instance_id'], live_user_id=ctx[2].pk, tombstone=False
    ).first()
    if row is None and create:
        row = AuditActorBinding.objects.create(instance_id=ctx[3]['instance_id'], live_user=ctx[2])
    return row


def creator(package, project, *, check_input=True):
    try:
        live = AuditActorBinding.objects.get(
            pk=package.creator_id, instance_id=package.instance_id, tombstone=False, live_user__isnull=False
        )
        actor = get_user_model().objects.get(pk=live.live_user_id, is_active=True)
        provider = get_identity_provider()
        # Only the exact built-in provider has the mechanical local contract.
        # A subclass may add Host grant checks and must execute its trusted hook.
        if type(provider) is LocalIdentityProvider:
            principal = Principal(f'{LOCAL_IDENTITY_SOURCE}:{actor.pk}', LOCAL_IDENTITY_SOURCE, '', actor.pk)
        else:
            try:
                principal, mapped = provider.resolve_background(package.creator_principal)
            except APIException:
                raise
            except Exception:
                # A trusted custom hook may fail unexpectedly. Never let its
                # raw detail escape to RQ logs; publish DB uncertainty remains
                # outside this narrow adapter and retains its original rules.
                error = APIException('AUDIT_GENERATION_FAILED')
                error.status_code = 500
                raise error from None
            if mapped.pk != actor.pk:
                c.fail('CREATOR_REVOKED', 403)
        ctx = scope(None, project.pk, resolved=(principal, actor))
        frozen = integrity(package) if check_input else {'policy': c.strict(package.access_policy, 4096)}
        if (
            package.creator_principal != principal.principal_id
            or package.creator_source != principal.source
            or package.organization_id_frozen != project.organization_id
            or str(package.instance_id) != ctx[3]['instance_id']
            or owner(ctx, live) != package.owner_hash
            or frozen['policy'] != audit_policy(ctx)
        ):
            c.fail('CREATOR_REVOKED', 403)
        return ctx
    except (AuditActorBinding.DoesNotExist, get_user_model().DoesNotExist, AttributeError, TypeError, ValueError):
        c.fail('CREATOR_REVOKED', 403)


def integrity(package):
    if (
        c.digest(package.input_bundle.encode()) != package.input_sha256
        or c.digest(package.projection.encode()) != package.projection_sha256
    ):
        c.fail('INPUT_INVALID')
    bundle = c.strict(package.input_bundle, c.limits()['input'])
    projection = audit_format.projection(package.projection)
    try:
        valid = (
            bundle['schema'] == c.SCHEMA
            and bundle['package_id'] == str(package.pk)
            and bundle['creator_generation'] == str(package.creator_id)
            and bundle['project_id'] == package.project_id
            and bundle['organization_id'] == package.organization_id_frozen
            and bundle['projection_sha256'] == package.projection_sha256
            and projection['package_id'] == str(package.pk)
            and bundle['owner_hash'] == package.owner_hash
            and bundle['request_hash'] == package.request_hash
            and bundle['formatter']['format'] == c.FORMAT
            and bundle['formatter']['entries'] == ['manifest.json', 'records.jsonl', 'README.txt', 'SHA256SUMS']
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        c.fail('INPUT_INVALID')
    return bundle


def event(ctx, phase, *, package=None, allowed=True, reason=''):
    return AuditAccessEvent.objects.create(
        project=ctx[0],
        package=package,
        actor_id_frozen=ctx[2].pk,
        phase=phase,
        allowed=allowed,
        reason=reason,
        policy_version=ctx[3]['version'],
        authorization_version=ctx[3]['_authority']['version'],
    )


def recheck(request, ctx, package=None):
    fresh = scope(
        request,
        ctx[0].pk,
        resolved=None if request is not None else (ctx[1], get_user_model().objects.get(pk=ctx[2].pk)),
    )
    if (fresh[1].principal_id, fresh[1].source, fresh[2].pk, fresh[2].date_joined, fresh[3]) != (
        ctx[1].principal_id,
        ctx[1].source,
        ctx[2].pk,
        ctx[2].date_joined,
        ctx[3],
    ):
        c.fail('AUTHORITY_CHANGED', 403)
    if package:
        creator(package, fresh[0])
    return fresh


def graph(ctx, batch_id, *, lock=False):
    budget = Budget(c.limits()['canonical'], c.limits()['stored'])
    batch, first = audit_source.load(ctx[0], batch_id, budget, lock=lock)
    again, second = audit_source.load(ctx[0], batch_id, budget, lock=lock)
    if c.exact(first.private()) != c.exact(second.private()) or batch.plan_hash != again.plan_hash:
        c.fail('SOURCE_CHANGED')
    root = dict(
        batch_id=batch.pk,
        plan_hash=batch.plan_hash,
        request_hash=batch.request_hash,
        actor=batch.actor_id_frozen,
        created_at=approved.stamp(batch.created_at),
    )
    return batch, second, root


def preview_value(ctx, batch_id, *, lock=False):
    batch, g, root = graph(ctx, batch_id, lock=lock)
    return (
        g,
        root,
        dict(
            batch_binding_hash=c.preview_hash('binding', root),
            capture_preview_hash=c.preview_hash('graph', dict(root=root, graph=g.private(), policy=ctx[3])),
            profile=c.PROFILE,
            effect=c.EFFECT,
            candidate_count=len(batch.plan['candidates']),
            entity_count=len(g.facts),
            edge_count=len(g.edges),
            gaps=dict(g.gaps),
            redacted=True,
        ),
    )


def parse(body, *, preview=False):
    keys = (
        {'review_batch_id'}
        if preview
        else {'review_batch_id', 'expected_batch_binding_hash', 'expected_capture_preview_hash', 'profile'}
    )
    if not isinstance(body, dict) or set(body) != keys or not c.positive(body['review_batch_id']):
        c.fail('INVALID_REQUEST', 400)
    if not preview and (
        body['profile'] != c.PROFILE or any(not c.hexadecimal(body[k]) for k in keys - {'profile', 'review_batch_id'})
    ):
        c.fail('INVALID_REQUEST', 400)
    return body


@transaction.atomic
def preview(request, ctx, body):
    body = parse(body, preview=True)
    ctx = fence(ctx)
    value = preview_value(ctx, body['review_batch_id'])[2]
    event(ctx, 'preview')
    recheck(request, ctx)
    return value


def capture(request, ctx, body, key):
    body = parse(body)
    if not c.uuid(key):
        c.fail('INVALID_KEY', 400)
    # Recover before any file/queue preflight. A frozen original command never
    # depends on source survival, current formatter or live Redis availability.
    with transaction.atomic():
        fresh = fence(ctx)
        previous = _recover(request, fresh, body, key)
        if previous:
            return previous
    from .audit_files import private_root

    private_root()
    if not queue_available():
        c.fail('QUEUE_UNAVAILABLE', 503)
    formatter = c.formatter()
    return _capture(request, ctx, body, key, formatter)


def _recover(request, ctx, body, key):
    who = binding(ctx)
    previous = AuditPackage.objects.filter(project=ctx[0], idempotency_key=key).first()
    if previous:
        if (
            who is None
            or previous.creator_id != who.pk
            or str(previous.instance_id) != ctx[3]['instance_id']
            or previous.owner_hash != owner(ctx, who)
            or previous.request_hash != c.digest(c.exact(body))
        ):
            c.fail('IDEMPOTENCY_CONFLICT')
        creator(previous, ctx[0])
        event(ctx, 'recover', package=previous)
        recheck(request, ctx, previous)
        return previous


@transaction.atomic
def _capture(request, ctx, body, key, formatter):
    ctx = fence(ctx)
    previous = _recover(request, ctx, body, key)
    if previous:
        return previous
    try:
        return _capture_new(request, ctx, body, key, formatter)
    except APIException as exc:
        # Raised inside the atomic body, before its commit/on_commit boundary.
        # Original-key recovery above deliberately does not carry this marker.
        exc.audit_command_rejected = True
        raise


def _capture_new(request, ctx, body, key, formatter):
    who = binding(ctx)
    request_hash = c.digest(c.exact(body))
    active = AuditPackage.objects.filter(project=ctx[0], state__in=ACTIVE)
    if (
        active.count() >= c.limits()['active_project']
        or who
        and active.filter(creator=who).count() >= c.limits()['active_owner']
    ):
        c.fail('ACTIVE_LIMIT', 429)
    g, root, value = preview_value(ctx, body['review_batch_id'], lock=True)
    if any(body['expected_' + k] != value[k] for k in ('batch_binding_hash', 'capture_preview_hash')):
        c.fail('PREVIEW_CHANGED')
    who = binding(ctx, create=True)
    pk = uuid.uuid4()
    at = timezone.now()
    ref = c.aliases()
    records = g.project(ref)
    projection = dict(
        schema=c.SCHEMA,
        profile=c.PROFILE,
        effect=c.EFFECT,
        package_id=str(pk),
        captured_at=approved.stamp(at),
        coverage='complete',
        evidence='with_gaps' if g.gaps else 'complete',
        history_completeness='not_provable',
        live_source_integrity='not_checked',
        records=records,
        counts=dict(Counter(r['type'] for r in records)),
        gaps=dict(g.gaps),
    )
    public = c.canonical(projection)
    if len(public) > c.limits()['projection']:
        c.fail('TOO_LARGE', 422)
    private = dict(
        schema=c.SCHEMA,
        package_id=str(pk),
        project_id=ctx[0].pk,
        organization_id=ctx[0].organization_id,
        policy=audit_policy(ctx),
        authorization_witness=ctx[3]['_authority'],
        root=root,
        graph=g.private(),
        creator_generation=str(who.pk),
        owner_hash=owner(ctx, who),
        request_hash=request_hash,
        formatter=formatter,
        projection_sha256=c.digest(public),
        binding_map=[dict(type=kind, key=key, ref=ref(kind, key)) for kind, key in sorted(g.facts)],
    )
    blob = c.exact(private)
    if len(blob) > c.limits()['input']:
        c.fail('TOO_LARGE', 422)
    package = AuditPackage.objects.create(
        id=pk,
        project=ctx[0],
        organization_id_frozen=ctx[0].organization_id,
        instance_id=ctx[3]['instance_id'],
        creator=who,
        creator_principal=ctx[1].principal_id,
        creator_source=ctx[1].source,
        owner_hash=owner(ctx, who),
        idempotency_key=key,
        request_hash=request_hash,
        input_bundle=blob.decode(),
        access_policy=c.exact(audit_policy(ctx)).decode(),
        summary_record=c.canonical(
            {k: projection[k] for k in ('captured_at', 'coverage', 'evidence', 'counts', 'gaps')}
        ).decode(),
        input_sha256=c.digest(blob),
        projection=public.decode(),
        projection_sha256=c.digest(public),
        captured_at=at,
    )
    event(ctx, 'create', package=package)
    recheck(request, ctx, package)
    transaction.on_commit(lambda: enqueue(str(pk)))
    return package


def summary(package, actor=None):
    value = c.strict(package.summary_record, 128 * 1024)
    if (
        package.error_code not in c.ERROR_CODES | {''}
        or not isinstance(value, dict)
        or set(value) != {'captured_at', 'coverage', 'evidence', 'counts', 'gaps'}
        or not isinstance(package.captured_at, datetime)
        or package.captured_at.tzinfo is None
        or value['captured_at'] != approved.stamp(package.captured_at)
        or value['coverage'] != 'complete'
        or value['evidence'] not in ('complete', 'with_gaps')
        or any(
            not isinstance(value[k], dict) or any(type(v) is not int or not 0 <= v <= 15000 for v in value[k].values())
            for k in ('counts', 'gaps')
        )
    ):
        c.fail('INPUT_INVALID')
    return dict(
        package_id=str(package.pk),
        state=package.state,
        error_code=package.error_code,
        captured_at=value['captured_at'],
        profile=c.PROFILE,
        effect=c.EFFECT,
        coverage=value['coverage'],
        evidence=value['evidence'],
        counts=value['counts'],
        gaps=value['gaps'],
        retryable=actor is not None
        and actor.principal_id == package.creator_principal
        and actor.source == package.creator_source
        and package.state
        in ('dispatch_pending', 'failed_retryable', 'blocked_auth', 'blocked_runtime', 'queued', 'running'),
        status_url=f'/api/projects/{package.project_id}/review-audit-packages/{package.pk}/',
    )


def enqueue(package_id):
    """One producer token. Redis is outside the DB transaction; duplicate delivery is harmless."""
    try:
        with transaction.atomic():
            hint = AuditPackage.objects.only('project_id').get(pk=package_id)
            project = Project.all_objects.select_for_update().get(pk=hint.project_id)
            package = AuditPackage.objects.select_for_update().get(pk=package_id)
            if package.state != 'dispatch_pending':
                return
            ctx = creator(package, project)
            approved.lock_authority(ctx[2].pk, project)
            creator(package, project)
            token = uuid.uuid4()
            attempt = AuditAttempt.objects.create(id=token, package=package, number=package.attempt_number + 1)
            package.state, package.token, package.attempt_number, package.queued_at, package.started_at = (
                'queued',
                token,
                attempt.number,
                timezone.now(),
                None,
            )
            package.save(update_fields=['state', 'token', 'attempt_number', 'queued_at', 'started_at'])
        try:
            queue = approved.api_queue()
            try:
                queue.enqueue(
                    generate,
                    str(package.pk),
                    str(token),
                    job_timeout=c.limits()['timeout'],
                    job_id=f'audit-{package.pk}-{token}',
                )
            finally:
                queue.connection.close()
        except Exception:
            # Commit/dispatch ACK may be unknown. Keep the token queued for lease recovery.
            return
    except (AuditPackage.DoesNotExist, Project.DoesNotExist, APIException):
        return


def retry(request, ctx, package, key):
    if not c.uuid(key):
        c.fail('INVALID_KEY', 400)
    # Queue health and module fingerprints may read files/network, never while
    # holding the mutation fence. Exact old retry receipts are source-free.
    with transaction.atomic():
        fresh = fence(ctx)
        creator(package, fresh[0])
        if package.owner_hash != owner(fresh, binding(fresh)):
            c.fail('FORBIDDEN', 403)
        previous = AuditRetryReceipt.objects.filter(package=package, key=key).first()
        if previous:
            return c.strict(previous.receipt, 128 * 1024)
    health = queue_available()
    formatter = c.formatter()
    return _retry(request, ctx, package, key, health, formatter)


@transaction.atomic
def _retry(request, ctx, package, key, health, formatter):
    ctx = fence(ctx)
    package = get_object_or_404(AuditPackage.objects.select_for_update(), pk=package.pk, project=ctx[0])
    creator(package, ctx[0])
    if package.owner_hash != owner(ctx, binding(ctx)):
        c.fail('FORBIDDEN', 403)
    previous = AuditRetryReceipt.objects.filter(package=package, key=key).first()
    if previous:
        return c.strict(previous.receipt, 128 * 1024)
    now = timezone.now()
    expired = (
        package.state == 'queued'
        and package.queued_at
        and package.queued_at <= now - timedelta(minutes=30)
        or package.state == 'running'
        and package.started_at
        and package.started_at <= now - timedelta(minutes=31)
    )
    if package.state in ('ready', 'quarantined', 'failed_final'):
        c.fail('NOT_RETRYABLE')
    if package.state == 'blocked_runtime' and integrity(package)['formatter'] != formatter:
        c.fail('RUNTIME_CHANGED')
    old_token = package.token
    if package.state in ('dispatch_pending', 'failed_retryable', 'blocked_auth', 'blocked_runtime') or expired:
        if not health:
            c.fail('QUEUE_UNAVAILABLE', 503)
        package.state, package.error_code = 'dispatch_pending', ''
        package.save(update_fields=['state', 'error_code'])
        transaction.on_commit(lambda: enqueue(str(package.pk)))
    result = dict(
        schema='audit-retry/v1',
        key=key,
        package_id=str(package.pk),
        state=package.state,
        previous_token=str(old_token) if old_token else None,
    )
    AuditRetryReceipt.objects.create(package=package, key=key, receipt=c.canonical(result).decode())
    event(ctx, 'retry', package=package)
    recheck(request, ctx, package)
    return result


def generate(package_id, token):
    from .audit_files import ScratchLease, directory

    lease = path = None
    directory_created = False
    try:
        formatter = c.formatter()
        with transaction.atomic():
            hint = AuditPackage.objects.only('project_id').get(pk=package_id)
            project = Project.all_objects.select_for_update().get(pk=hint.project_id)
            package = AuditPackage.objects.select_for_update().get(pk=package_id)
            if package.state != 'queued' or str(package.token) != token:
                return
            ctx = creator(package, project)
            approved.lock_authority(ctx[2].pk, project)
            creator(package, project)
            bundle = integrity(package)
            if bundle['formatter'] != formatter:
                c.fail('RUNTIME_CHANGED')
            package.state, package.started_at = 'running', timezone.now()
            package.save(update_fields=['state', 'started_at'])
            AuditAttempt.objects.filter(pk=token, package=package).update(state='running')
            event(ctx, 'job_start', package=package)
        lease = ScratchLease(package, token)
        path = directory(package)
        path.mkdir(parents=True, exist_ok=False, mode=0o700)
        directory_created = True
        target = path / 'review-audit.zip'
        with target.open('xb') as output:
            manifest = audit_format.write(package, bundle, output, maximum=lease.capacity)
            output.flush()
            os.fsync(output.fileno())
        size = target.stat().st_size
        if size > c.limits()['artifact'] or size * 2 > c.limits()['scratch']:
            c.fail('TOO_LARGE', 422)
        with target.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        target.chmod(0o400)
        current_formatter = c.formatter()
        with transaction.atomic():
            project = Project.all_objects.select_for_update().get(pk=package.project_id)
            current = AuditPackage.objects.select_for_update().get(pk=package_id)
            if current.state != 'running' or str(current.token) != token:
                return
            ctx = creator(current, project)
            approved.lock_authority(ctx[2].pk, project)
            creator(current, project)
            if integrity(current)['formatter'] != current_formatter:
                c.fail('RUNTIME_CHANGED')
            AuditArtifact.objects.create(
                package=current,
                attempt_id=token,
                storage_key=target.relative_to(lease.root).as_posix(),
                sha256=digest,
                size_bytes=size,
            )
            AuditAttempt.objects.filter(pk=token, package=current).update(state='ready', finished_at=timezone.now())
            current.state, current.manifest, current.manifest_sha256 = (
                'ready',
                manifest.decode('utf-8'),
                c.digest(manifest),
            )
            current.save(update_fields=['state', 'manifest', 'manifest_sha256'])
            event(ctx, 'publish', package=current)
    except (AuditPackage.DoesNotExist, Project.DoesNotExist):
        return
    except (APIException, OSError, Http404) as exc:
        detail = str(exc.detail) if isinstance(exc, APIException) else None
        code = (
            detail
            if detail in c.ERROR_CODES
            else 'AUDIT_CREATOR_REVOKED'
            if isinstance(exc, Http404) or isinstance(exc, APIException) and exc.status_code in (401, 403)
            else 'AUDIT_IO_FAILED'
            if isinstance(exc, OSError)
            else 'AUDIT_GENERATION_FAILED'
        )
        state = (
            'blocked_runtime'
            if code == 'AUDIT_RUNTIME_CHANGED'
            else 'blocked_auth'
            if isinstance(exc, Http404) or isinstance(exc, APIException) and exc.status_code in (401, 403)
            else 'failed_retryable'
            if isinstance(exc, OSError)
            or code in ('AUDIT_SCRATCH_LIMIT', 'AUDIT_PRIVATE_STORAGE_REQUIRED', 'AUDIT_IO_FAILED')
            else 'failed_final'
        )
        with transaction.atomic():
            hint = AuditPackage.objects.filter(pk=package_id).values('project_id').first()
            if not hint:
                return
            Project.all_objects.select_for_update().filter(pk=hint['project_id']).first()
            current = AuditPackage.objects.select_for_update().filter(pk=package_id).first()
            if current and str(current.token) == token and current.state in ('queued', 'running'):
                current.state, current.error_code = state, code[:64]
                current.save(update_fields=['state', 'error_code'])
                AuditAttempt.objects.filter(pk=token).update(state=state, finished_at=timezone.now())
    finally:
        if lease and path:
            # If DB status itself is unknown, leave both file and reservation.
            # A later/stale worker only cleans its own unpublished token tree.
            with transaction.atomic():
                hint = AuditPackage.objects.filter(pk=package_id).values('project_id').first()
                if hint:
                    Project.all_objects.select_for_update().filter(pk=hint['project_id']).first()
                    current = AuditPackage.objects.select_for_update().filter(pk=package_id).first()
                    published = AuditArtifact.objects.filter(package_id=package_id, attempt_id=token).exists()
                    safe = current and (str(current.token) != token or current.state not in ('queued', 'running'))
                else:
                    published = safe = False
            if published:
                # READY bytes still occupy scratch. Retain this reservation so
                # paused stale writers cannot consume two further full slots.
                pass
            elif safe and directory_created:
                lease.discard(path)
            elif safe:
                lease.release()
