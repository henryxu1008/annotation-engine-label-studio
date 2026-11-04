"""Extend the existing owned supervision fixture with frozen audit artifacts."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from access_control.project_operations import EXPORT_REVIEW_AUDIT, ProjectOperationPolicy, local_principal
from core.management.commands.seed_second_review_e2e import SecondReviewE2EPolicy
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.test import override_settings

PROVIDER = 'core.management.commands.seed_review_audit_e2e.ReviewAuditE2EPolicy'


class ReviewAuditE2EPolicy:
    def evaluate(self, project):
        base = SecondReviewE2EPolicy().evaluate(project)
        if base.version == 'synthetic-supervision/v1':
            return ProjectOperationPolicy(base.version, base.state, base.allowed_operations | {EXPORT_REVIEW_AUDIT})
        return base


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--output', default='web/apps/labelstudio-e2e/.review-audit-e2e.json')

    def handle(self, *args, **options):
        output = Path(options['output'])
        if output.exists() or not settings.TEST_ENVIRONMENT:
            raise CommandError('Fresh owned synthetic output required')
        # The original command verifies fresh native cluster/database ownership,
        # empty business scope and exact private bootstrap before any insert.
        source = output.with_name(output.stem + '-supervision-private.json')
        call_command('seed_second_review_e2e', output=str(source))
        value = json.loads(source.read_text(encoding='utf-8'))
        configured = {
            **value['settings'],
            'ANNOTATION_PROJECT_OPERATION_PROVIDER': PROVIDER,
            'ANNOTATION_REVIEW_AUDIT_ENABLED': True,
            'ANNOTATION_REVIEW_AUDIT_POLICY': {
                'instance_id': str(uuid4()),
                'projects': {
                    key: {'mode': 'fork_review', 'version': 'synthetic-audit/v1'}
                    for key in value['settings']['ANNOTATION_REVIEW_BATCH_MODES']
                },
            },
            'APPROVED_EXPORT_ROOT': str(Path(settings.BASE_DATA_DIR).parent / 'audit-private-exports'),
        }
        from data_export import approved
        from data_export import review_audit as audit
        from data_export.audit_models import AuditPackage
        from users.models import User

        actor = User.objects.get(pk=value['users']['manager']['id'])
        ready = {}
        # Deliberately synthetic queue transport; production capture, frozen
        # formatter, token claims, publish, files and HTTP readers are exercised.
        # No synchronous production fallback or real Redis service is enabled.
        with override_settings(**configured), patch.object(audit, 'queue_available', return_value=True):
            for locale, project in value['projects'].items():
                ctx = audit.scope(None, project['id'], resolved=(local_principal(actor), actor))
                preview = audit.preview(None, ctx, {'review_batch_id': project['batch_id']})
                body = dict(
                    review_batch_id=project['batch_id'],
                    profile=audit.PROFILE,
                    expected_batch_binding_hash=preview['batch_binding_hash'],
                    expected_capture_preview_hash=preview['capture_preview_hash'],
                )
                with patch.object(audit, 'enqueue'):
                    row = audit.capture(None, ctx, body, str(uuid4()))
                with patch.object(approved, 'api_queue', return_value=MagicMock()):
                    audit.enqueue(str(row.pk))
                row.refresh_from_db()
                audit.generate(str(row.pk), str(row.token))
                row = AuditPackage.objects.get(pk=row.pk)
                if row.state != 'ready':
                    raise CommandError('Synthetic audit publication failed')
                ready[locale] = str(row.pk)
        with output.open('x', encoding='utf-8') as handle:
            json.dump(
                dict(schema='review-audit-e2e/v1', supervision=value, settings=configured, ready=ready),
                handle,
                indent=2,
            )
        self.stdout.write('Fresh owned audit fixture prepared; queue transport is synthetic.')
