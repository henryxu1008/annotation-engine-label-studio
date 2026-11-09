"""Every audit surface enforces current requester and creator before metadata/bytes."""
from contextlib import ExitStack, closing

from core.permissions import all_permissions
from django.core import signing
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.utils.cache import get_conditional_response
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import APIException
from rest_framework.generics import GenericAPIView, get_object_or_404
from rest_framework.parsers import BaseParser
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from review_batches.second_review_bounded import Budget

from . import audit_contract as c
from . import audit_format, snapshots
from . import review_audit as s
from .approved_api import BoundedRangeReader, PrivateRangeResponse
from .audit_files import artifact_path
from .audit_models import AuditArtifact, AuditPackage


class AuditJSONParser(BaseParser):
    media_type = 'application/json'

    def parse(self, stream, media_type=None, parser_context=None):
        chunks = []
        used = 0
        while True:
            chunk = stream.read(16 * 1024 + 1 - used)
            if not chunk:
                break
            used += len(chunk)
            if used > 16 * 1024:
                c.fail('TOO_LARGE', 422)
            chunks.append(chunk)
        return c.strict(b''.join(chunks), 16 * 1024)


class AuditAPI(GenericAPIView):
    permission_required = all_permissions.projects_change
    parser_classes = [AuditJSONParser]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.ctx = s.scope(request, kwargs['pk'])
        if request.headers.get('X-Audit-Viewer') not in (None, s.viewer(self.ctx)):
            c.fail('VIEWER_CHANGED', 403)
        if any(len(request.query_params.getlist(k)) != 1 for k in request.query_params):
            c.fail('INVALID_REQUEST', 400)

    def finalize_response(self, request, response, *args, **kwargs):
        result = super().finalize_response(request, response, *args, **kwargs)
        result['Cache-Control'] = 'private, no-store'
        result['Referrer-Policy'] = 'no-referrer'
        result['X-Content-Type-Options'] = 'nosniff'
        if hasattr(self, 'ctx'):
            result['X-Audit-Viewer'] = s.viewer(self.ctx)
        return result

    def handle_exception(self, exc):
        rejected = getattr(exc, 'audit_command_rejected', False) is True
        if isinstance(exc, APIException):
            code = str(exc.detail) if str(exc.detail) in c.ERROR_CODES else 'AUDIT_REQUEST_REJECTED'
            status = exc.status_code
        elif isinstance(exc, Http404):
            code, status = 'AUDIT_NOT_FOUND', 404
        elif isinstance(exc, DjangoPermissionDenied):
            code, status = 'AUDIT_FORBIDDEN', 403
        else:
            code, status = 'AUDIT_REQUEST_REJECTED', 500
        sanitized = APIException(code)
        sanitized.status_code = status if type(status) is int and 400 <= status <= 599 else 500
        sanitized.display_context = {'reason': code}
        if getattr(self, 'quarantine_id', None):
            with transaction.atomic():
                from projects.models import Project

                Project.all_objects.select_for_update().filter(pk=self.kwargs['pk']).first()
                AuditPackage.objects.filter(pk=self.quarantine_id, state='ready').update(
                    state='quarantined', error_code='AUDIT_FILE_INVALID'
                )
        try:
            if hasattr(self, 'ctx'):
                s.event(self.ctx, 'request_denied', allowed=False, reason='REQUEST_DENIED')
        except Exception:
            pass
        # The shared handler logs sys.exc_info(), not merely its argument. Make
        # the fixed exception current, suppressing the provider's original chain
        # in both formatted logs and DEBUG error responses.
        try:
            raise sanitized from None
        except APIException as safe:
            response = super().handle_exception(safe)
        if rejected:
            response['X-Audit-Command-State'] = 'rejected'
        return response

    def package(self, *, metadata=False):
        query = AuditPackage.objects.filter(
            pk=self.kwargs['package_id'], project=self.ctx[0], instance_id=self.ctx[3]['instance_id']
        )
        payloads = {'access_policy': (4096, 16384), 'summary_record': (128 * 1024, 512 * 1024)}
        if metadata:
            query = query.defer('input_bundle', 'projection', 'manifest')
        else:
            payloads.update(
                input_bundle=(8 * c.MIB, 8 * c.MIB),
                projection=(8 * c.MIB, 8 * c.MIB),
                manifest=(256 * 1024, 256 * 1024),
            )
        rows = Budget(20 * c.MIB, 32 * c.MIB).rows(query, payloads)
        if not rows:
            c.fail('NOT_FOUND', 404)
        package = rows[0]
        s.creator(package, self.ctx[0], check_input=not metadata)
        return package

    def result(self, request, value, *, package=None, status=200, maximum=128 * 1024, final_check=None):
        value = dict(
            value,
            schema='review-audit-response/v1',
            profile=c.PROFILE,
            effect=c.EFFECT,
            viewer_hash=s.viewer(self.ctx),
            owner_hash=s.client_owner(self.ctx),
        )
        if len(c.canonical(value)) > maximum:
            c.fail('TOO_LARGE', 422)
        result = Response(value, status=status)
        result.accepted_renderer = JSONRenderer()
        result.accepted_media_type = 'application/json'
        result.renderer_context = {}
        result.render()
        if final_check:
            final_check()
        s.recheck(request, self.ctx, package)
        return result

    @transaction.atomic
    def options(self, request, *args, **kwargs):
        self.ctx = s.fence(self.ctx)
        package = self.package() if 'package_id' in kwargs else None
        return self.result(request, {'methods': ['GET', 'HEAD', 'POST', 'OPTIONS']}, package=package)

    def post(self, request, *args, **kwargs):
        if request.query_params:
            c.fail('INVALID_REQUEST', 400)
        action = kwargs.get('action', 'list')
        if action == 'preview':
            value = s.preview(request, self.ctx, request.data)
            with transaction.atomic():
                self.ctx = s.fence(self.ctx)
                return self.result(request, value)
        if action == 'list':
            package = s.capture(request, self.ctx, request.data, request.headers.get('Idempotency-Key'))
            with transaction.atomic():
                self.ctx = s.fence(self.ctx)
                return self.result(request, s.summary(package, self.ctx[1]), package=package, status=202)
        if action == 'retry':
            package = self.package()
            if request.data != {}:
                c.fail('INVALID_REQUEST', 400)
            value = s.retry(request, self.ctx, package, request.headers.get('Idempotency-Key'))
            with transaction.atomic():
                self.ctx = s.fence(self.ctx)
                return self.result(request, value, package=package)
        c.fail('NOT_FOUND', 404)

    @transaction.atomic
    def get(self, request, *args, **kwargs):
        self.ctx = s.fence(self.ctx)
        action = kwargs.get('action', 'list')
        if action == 'capabilities':
            if request.query_params:
                c.fail('INVALID_REQUEST', 400)
            return self.result(
                request,
                dict(
                    profile=c.PROFILE,
                    effect=c.EFFECT,
                    limits=c.limits(),
                    collection_supported=s.collection_supported(),
                ),
            )
        if action == 'list':
            return self.history(request)
        if request.query_params:
            c.fail('INVALID_REQUEST', 400)
        package = self.package()
        if action == 'detail':
            value = s.summary(package, self.ctx[1])
            value['artifacts'] = []
            if package.state == 'ready':
                with self.checked(package) as (handle, artifact):
                    value['artifacts'] = [
                        dict(
                            artifact_id=str(artifact.pk),
                            size_bytes=artifact.size_bytes,
                            sha256=artifact.sha256,
                            download_url=value['status_url'] + f'artifacts/{artifact.pk}/download/',
                        )
                    ]
            s.event(self.ctx, 'detail', package=package)
            return self.result(request, value, package=package)
        if package.state != 'ready':
            c.fail('NOT_READY')
        with ExitStack() as stack:
            handle, artifact = stack.enter_context(self.checked(package))
            if action == 'manifest':
                response = HttpResponse(package.manifest, content_type='application/json')
                response['X-Manifest-SHA256'] = package.manifest_sha256
            elif action == 'download':
                if artifact.pk != kwargs['artifact_id']:
                    c.fail('NOT_FOUND', 404)
                etag = f'"{artifact.sha256}"'
                # Preconditions must not bypass the final authority check or
                # leave the verified artifact handle open on a bodyless response.
                conditional = get_conditional_response(request, etag=etag)
                if conditional is not None:
                    conditional['ETag'] = etag
                    s.event(self.ctx, 'download', package=package)
                    s.recheck(request, self.ctx, package)
                    return conditional
                if request.META.get('HTTP_IF_RANGE') not in (None, etag):
                    request.META.pop('HTTP_RANGE', None)
                if request.META.get('HTTP_RANGE'):
                    try:
                        ranges = BoundedRangeReader(handle).parse_range_header(
                            request.META['HTTP_RANGE'], artifact.size_bytes
                        )
                    except ValueError:
                        ranges = None
                    if (
                        not ranges
                        or len(ranges) != 1
                        or not (0 <= ranges[0][0] < artifact.size_bytes and ranges[0][0] < ranges[0][1])
                    ):
                        response = HttpResponse(status=416)
                        response['Content-Range'] = f'bytes */{artifact.size_bytes}'
                        s.event(self.ctx, 'range_rejected', package=package)
                        s.recheck(request, self.ctx, package)
                        return response
                response = PrivateRangeResponse(request, handle, content_type='application/zip')
                if response.status_code == 200:
                    response['Content-Length'] = artifact.size_bytes
                response['ETag'], response['Accept-Ranges'] = etag, 'bytes'
                response['Content-Disposition'] = f'attachment; filename="review-audit-{package.pk}.zip"'
            else:
                c.fail('NOT_FOUND', 404)
            s.event(self.ctx, action, package=package)
            s.recheck(request, self.ctx, package)
            if action == 'download':
                closer = stack.pop_all()
                response._resource_closers.append(closer.close)
            return response

    def checked(self, package):
        from contextlib import contextmanager

        @contextmanager
        def checked():
            # Close file contexts without injecting caller exceptions into the
            # shared descriptor verifier's integrity-error translation.
            with closing(ExitStack()) as stack:
                try:
                    artifact = get_object_or_404(AuditArtifact, package=package)
                    if artifact.size_bytes > c.limits()['artifact']:
                        c.fail('FILE_INVALID')
                    handle = stack.enter_context(
                        snapshots.checked_file(artifact_path(package, artifact), artifact.sha256, artifact.size_bytes)
                    )
                    audit_format.verify(handle, package)
                except (APIException, OSError):
                    # The request savepoint rolls back first; handle_exception
                    # quarantines only an acquisition/verification failure.
                    self.quarantine_id = package.pk
                    c.fail('FILE_INVALID')
                yield handle, artifact

        return checked()

    def history(self, request):
        if set(request.query_params) - {'cursor', 'page_size'}:
            c.fail('INVALID_REQUEST', 400)
        size = request.query_params.get('page_size', '20')
        if len(size) > 2 or not size.isascii() or not size.isdecimal() or not 1 <= int(size) <= c.limits()['page']:
            c.fail('INVALID_REQUEST', 400)
        ctx = self.ctx
        bind = c.preview_hash(
            'cursor',
            dict(
                project=ctx[0].pk,
                principal=ctx[1].principal_id,
                source=ctx[1].source,
                actor=ctx[2].pk,
                policy=ctx[3],
                size=int(size),
                order='captured_at-uuid-desc',
            ),
        )
        query = s.visible_packages(ctx)
        token = request.query_params.get('cursor')
        if token:
            try:
                if len(token) > 4096:
                    raise ValueError()
                decoded = signing.loads(token, salt='review-audit-history/v1', max_age=3600)
                if (
                    set(decoded) != {'binding', 'after', 'time'}
                    or decoded['binding'] != bind
                    or not c.uuid(decoded['after'])
                    or not isinstance(decoded['time'], str)
                    or len(decoded['time']) > 128
                ):
                    raise ValueError()
                timestamp = parse_datetime(decoded['time'])
                if timestamp is None or timestamp.tzinfo is None:
                    raise ValueError()
                query = query.filter(Q(captured_at__lt=timestamp) | Q(captured_at=timestamp, id__lt=decoded['after']))
            except (signing.BadSignature, KeyError, TypeError, ValueError):
                c.fail('INVALID_CURSOR', 400)
        query = query.defer('input_bundle', 'projection', 'manifest').order_by('-captured_at', '-id')[: int(size) + 1]
        rows = Budget(8 * c.MIB, 16 * c.MIB).rows(
            query, {'access_policy': (4096, 16384), 'summary_record': (128 * 1024, 512 * 1024)}
        )
        visible = []
        for package in rows[: int(size)]:
            visible.append(s.summary(package, ctx[1]))
        cursor = (
            signing.dumps(
                dict(
                    binding=bind,
                    after=str(rows[int(size) - 1].pk),
                    time=s.approved.stamp(rows[int(size) - 1].captured_at),
                ),
                salt='review-audit-history/v1',
            )
            if len(rows) > int(size)
            else None
        )
        s.event(ctx, 'history')
        return self.result(
            request,
            dict(results=visible, next_cursor=cursor),
            maximum=256 * 1024,
            final_check=lambda: s.history_gate(ctx, rows),
        )
