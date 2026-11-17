"""Read-only S3 request-token inventory; it never resolves a ledger entry."""
import json

from django.core.management.base import BaseCommand, CommandError
from io_storages.admission_models import StorageOperation
from io_storages.s3.models import S3ExportStorage
from io_storages.s3.version_provenance import inspect_aws_export_request_page


class Command(BaseCommand):
    help = 'Inspect object versions for one blocking S3 export request without resolving it.'

    def add_arguments(self, parser):
        parser.add_argument('--operation-id', required=True)
        parser.add_argument('--expected-owner', required=True)
        parser.add_argument('--cursor')
        parser.add_argument('--page-size', type=int, default=25)

    def handle(self, *args, **options):
        try:
            record = StorageOperation.objects.filter(
                pk=options['operation_id'],
                operation='annotation_export',
                storage_model=S3ExportStorage._meta.label_lower,
                state__in=(StorageOperation.State.RUNNING, StorageOperation.State.UNCERTAIN),
            ).first()
            if record is None:
                raise CommandError('A blocking S3 annotation export is required.')
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
            result = inspect_aws_export_request_page(
                storage,
                record,
                options['expected_owner'],
                storage.get_client(),
                cursor=options['cursor'],
                page_size=options['page_size'],
            )
        except CommandError:
            raise
        except Exception as exc:
            # Never echo provider messages, request tokens, credentials or URLs.
            raise CommandError(f'S3 export inspection failed: {type(exc).__name__}') from None
        result[
            'notice'
        ] = 'Non-snapshot observation; not proof of request drain or authorization to replay, reconcile or delete.'
        self.stdout.write(json.dumps(result, ensure_ascii=True, separators=(',', ':')))
