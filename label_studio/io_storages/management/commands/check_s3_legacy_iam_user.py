"""Inspect one old IAM user without changing cloud resources."""
import json

import boto3
from django.core.management.base import BaseCommand, CommandError
from io_storages.s3.legacy_iam_inventory import inspect_legacy_iam_user


class Command(BaseCommand):
    help = 'Inspect one historical IAM user long-term key inventory; never attest isolation or drain.'

    def add_arguments(self, parser):
        parser.add_argument('--account-id', required=True)
        parser.add_argument('--user-name', required=True)
        parser.add_argument('--user-id', required=True)

    def handle(self, *args, **options):
        try:
            session = boto3.Session()
            result = inspect_legacy_iam_user(
                session.client('iam'),
                session.client('sts'),
                account_id=options['account_id'],
                user_name=options['user_name'],
                user_id=options['user_id'],
            )
        except Exception as exc:
            # Provider errors may contain identities, URLs or credential material.
            raise CommandError(f'IAM inventory failed: {type(exc).__name__}') from None
        result['notice'] = 'Point-in-time long-term key inventory only; isolation and request drain are not proven.'
        self.stdout.write(json.dumps(result, separators=(',', ':')))
