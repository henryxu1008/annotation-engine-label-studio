"""Default-off, Manager-scoped lifecycle and storage-operation commands."""
from access_control.authorization import authorization
from access_control.identity import resolve_actor
from access_control.project_operations import SECURITY_CANCEL, lock_project_operation
from core.permissions import all_permissions
from django.conf import settings
from django.db import transaction
from io_storages.admission import (
    cancel_queued_storage_dispatch,
    cancel_unsubmitted_s3_preflight,
    legacy_export_blocker_counts,
    legacy_export_blocker_details,
    reconcile_pending_deletion,
    reconcile_uncertain_storage_operation,
    recover_unstarted_storage,
    recover_unsubmitted_webhook,
    storage_operation_page,
)
from io_storages.admission_models import StorageOperation
from projects.lifecycle import transition_project
from rest_framework import generics, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response


class LifecycleBase(generics.GenericAPIView):
    permission_required = all_permissions.projects_change

    def require_feature(self):
        if (
            not getattr(settings, 'ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED', False)
            or not getattr(settings, 'ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED', False)
            or not getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False)
            or getattr(settings, 'ANNOTATION_PROJECT_OPERATION_PROVIDER', None)
            != 'access_control.project_operations.LifecycleProjectOperationProvider'
        ):
            raise NotFound()

    def principal(self, request):
        return resolve_actor(request)[0]

    def locked_manager(self, project_id, principal):
        project, _ = lock_project_operation(project_id, principal, SECURITY_CANCEL)
        authorization.require(
            authorization.can_manage_project(principal, project), 'Project manager role is required.'
        )
        return project

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'private, no-store'
        return response


def strict_body(request, fields):
    data = request.data
    if not isinstance(data, dict) or set(data) != fields:
        raise ValidationError('The request must contain exactly the documented fields.')
    return data


class LifecycleAPI(LifecycleBase):
    def get(self, request, pk):
        self.require_feature()
        with transaction.atomic():
            project = self.locked_manager(pk, self.principal(request))
            return Response({'state': project.lifecycle_state, 'revision': project.lifecycle_revision})

    def post(self, request, pk):
        self.require_feature()
        data = strict_body(request, {'target', 'expected_revision', 'reason'})
        project = transition_project(
            pk,
            self.principal(request),
            target=data['target'],
            expected_revision=data['expected_revision'],
            reason=data['reason'],
        )
        return Response({'state': project.lifecycle_state, 'revision': project.lifecycle_revision})


class StorageOperationsAPI(LifecycleBase):
    def get(self, request, pk):
        self.require_feature()
        cursor_values = request.query_params.getlist('legacy_cursor')
        if len(cursor_values) > 1:
            raise ValidationError('Only one legacy export cursor is allowed.')
        storage_cursors = request.query_params.getlist('storage_cursor')
        if len(storage_cursors) > 1:
            raise ValidationError('Only one storage operation cursor is allowed.')
        with transaction.atomic():
            project = self.locked_manager(pk, self.principal(request))
            return Response(
                {
                    **storage_operation_page(project, cursor=storage_cursors[0] if storage_cursors else None),
                    'legacy_export_blockers': legacy_export_blocker_counts(project),
                    'legacy_export_details': legacy_export_blocker_details(
                        project,
                        cursor=cursor_values[0] if cursor_values else None,
                    ),
                }
            )


class StorageOperationResolveAPI(LifecycleBase):
    def post(self, request, pk, operation_id):
        self.require_feature()
        data = strict_body(request, {'action', 'reason', 'attested'})
        principal = self.principal(request)
        if data['action'] == 'cancel_queued':
            if data['attested'] is not False:
                raise ValidationError('Cancellation does not use an attestation.')
            cancel_queued_storage_dispatch(pk, operation_id, principal, reason=data['reason'])
        elif data['action'] == 'cancel_s3_preflight':
            if data['attested'] is not False:
                raise ValidationError('Cancellation does not use an attestation.')
            cancel_unsubmitted_s3_preflight(pk, operation_id, principal, reason=data['reason'])
        elif data['action'] == 'recover_webhook_pre_send':
            if data['attested'] is not False:
                raise ValidationError('Cancellation does not use an attestation.')
            recover_unsubmitted_webhook(pk, operation_id, principal, reason=data['reason'])
        elif data['action'] == 'recover_storage_pre_effect':
            if data['attested'] is not False:
                raise ValidationError('Recovery does not use an attestation.')
            recover_unstarted_storage(pk, operation_id, principal, reason=data['reason'])
        elif data['action'] == 'recover_s3_delete_pre_effect':
            if data['attested'] is not False:
                raise ValidationError('Recovery does not use an attestation.')
            from io_storages.s3.version_delete import recover_unsubmitted_delete

            recover_unsubmitted_delete(pk, operation_id, principal, reason=data['reason'])
        elif data['action'] == 'confirm_deletion':
            reconcile_pending_deletion(
                pk,
                operation_id,
                principal,
                reason=data['reason'],
                attested_complete=data['attested'],
            )
        elif data['action'] == 'confirm_uncertain':
            # A running operation may still perform an external effect. The
            # service repeats this check under lock as a defense in depth.
            with transaction.atomic():
                project = self.locked_manager(pk, principal)
                record = StorageOperation.objects.select_for_update().filter(pk=operation_id, project=project).first()
                if record is None or record.state != StorageOperation.State.UNCERTAIN:
                    raise ValidationError('An uncertain operation in this project is required.')
                reconcile_uncertain_storage_operation(
                    pk,
                    operation_id,
                    principal,
                    reason=data['reason'],
                    attested_quiescent=data['attested'],
                )
        else:
            raise ValidationError('Unknown operation resolution action.')
        return Response(status=status.HTTP_204_NO_CONTENT)
