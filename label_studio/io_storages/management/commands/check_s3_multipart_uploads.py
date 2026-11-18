"""Read-only multipart inventory for a blocking, owner-bound S3 operation."""
import json

from django.core.management.base import BaseCommand, CommandError
from io_storages.admission_models import StorageOperation
from io_storages.s3.models import S3ExportStorage
from io_storages.s3.multipart_inventory import inspect_aws_multipart_page


class Command(BaseCommand):
    help = 'Inspect incomplete multipart uploads for a blocking S3 operation without changing it.'

    def add_arguments(self, parser):
        parser.add_argument('--operation-id', required=True)
        parser.add_argument('--expected-owner', required=True)
        parser.add_argument('--cursor')
        parser.add_argument('--page-size', type=int, default=25)
        parser.add_argument(
            '--legacy-current-configuration',
            action='store_true',
            help='Observe the current configuration for an unbound legacy record; history remains unknown.',
        )

    def handle(self, *args, **options):
        try:
            record = StorageOperation.objects.filter(
                pk=options['operation_id'],
                storage_model=S3ExportStorage._meta.label_lower,
            ).first()
            if record is None:
                raise CommandError('A blocking S3 operation is required.')
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
            result = inspect_aws_multipart_page(
                storage,
                record,
                options['expected_owner'],
                storage.get_client(),
                cursor=options['cursor'],
                page_size=options['page_size'],
                legacy_current_configuration=options['legacy_current_configuration'],
            )
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(f'S3 multipart inspection failed: {type(exc).__name__}') from None
        result['notice'] = (
            'Non-snapshot observation; not proof of request drain or authorization '
            'to abort, replay, reconcile or delete.'
        )
        if options['legacy_current_configuration']:
            result['notice'] += ' Current configuration is not proof of the historical request destination.'
        self.stdout.write(json.dumps(result, separators=(',', ':')))
