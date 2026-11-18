"""Read-only AWS target inspection; this does not authorize deletion."""
import json

from django.core.management.base import BaseCommand, CommandError
from io_storages.admission_models import StorageOperation
from io_storages.s3.deletion_plan import describe_version_deletion
from io_storages.s3.models import S3ExportStorage
from io_storages.s3.version_provenance import inspect_aws_deletion_history, verify_aws_deletion_target


class Command(BaseCommand):
    help = 'Inspect the AWS identity and version of one pending deletion without deleting it.'

    def add_arguments(self, parser):
        parser.add_argument('--operation-id', required=True)
        parser.add_argument('--expected-owner', required=True)

    def handle(self, *args, **options):
        try:
            record = StorageOperation.objects.filter(
                pk=options['operation_id'],
                state=StorageOperation.State.QUEUED,
                operation='annotation_delete_pending',
                storage_model=S3ExportStorage._meta.label_lower,
            ).first()
            if record is None:
                raise CommandError('A queued S3 annotation deletion is required.')
            storage = S3ExportStorage.objects.filter(pk=record.storage_id, project_id=record.project_id).first()
            if storage is None:
                raise CommandError('The recorded S3 storage no longer exists in this project.')
            if (
                not storage.bucket
                or not storage.aws_access_key_id
                or not storage.aws_secret_access_key
                or storage.s3_endpoint
            ):
                raise CommandError('Explicit S3 credentials and a standard AWS endpoint are required.')
            client = storage.get_client()
            verify_aws_deletion_target(storage, record, options['expected_owner'], client)
            history = inspect_aws_deletion_history(storage, record, options['expected_owner'], client)
            # Provider reads are not a database snapshot. If the local claim
            # changed while they ran, do not report a plan for stale identity.
            if not StorageOperation.objects.filter(
                pk=record.pk,
                project_id=record.project_id,
                storage_model=record.storage_model,
                storage_id=record.storage_id,
                operation='annotation_delete_pending',
                state=StorageOperation.State.QUEUED,
                lifecycle_revision=record.lifecycle_revision,
                subject_id=record.subject_id,
                target_key=record.target_key,
                target_version_id=record.target_version_id,
                target_object_token=record.target_object_token,
                target_storage_binding=record.target_storage_binding,
            ).exists():
                raise CommandError('The pending S3 deletion changed during inspection; no deletion is authorized.')
            plan = describe_version_deletion(history)
        except CommandError:
            raise
        except Exception as exc:
            # Provider messages and URLs can contain sensitive configuration.
            raise CommandError(f'S3 target inspection failed: {type(exc).__name__}') from None
        self.stdout.write(
            f'The observed AWS target matches the recorded version and token; history: {history}. '
            'This read-only observation does not prove isolation or drain and does not authorize deletion. '
            f'Plan: {json.dumps(plan, sort_keys=True)}'
        )
