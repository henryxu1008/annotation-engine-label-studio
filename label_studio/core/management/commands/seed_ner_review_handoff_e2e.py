"""Synthetic-only adapter over the existing NER review fixture."""
import json
from pathlib import Path
from uuid import uuid4

from access_control.project_operations import local_principal
from classification_disagreements import ner, ner_batches
from classification_disagreements.models import NerBatch
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from projects.models import Project, ProjectMember
from tasks.models import Submission, Task
from tasks.submissions import _snapshot_hash
from users.models import User

from .seed_ner_e2e import TEXT, source

SCHEMA = 'ner-review-handoff-e2e/v1'


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--output', default='web/apps/labelstudio-e2e/.ner-review-handoff-e2e.json')
        parser.add_argument(
            '--operation', default='seed', choices=['seed', 'corrupt_source', 'restore_source', 'revoke_reviewer']
        )
        parser.add_argument('--locale', default='en-US', choices=['en-US', 'zh-CN'])

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.TEST_ENVIRONMENT:
            raise CommandError('Synthetic TEST_ENVIRONMENT required')
        output = Path(options['output'])
        if options['operation'] != 'seed':
            return self.transition(output, options['locale'], options['operation'])
        # Reuse the complete fresh fixture and its isolation guard.
        call_command('seed_ner_review_priority_e2e', output=str(output), stdout=self.stdout)
        fixture = json.loads(output.read_text(encoding='utf-8'))
        fixture['schema'] = SCHEMA
        for scope in fixture['projects'].values():
            project = Project.objects.get(
                pk=scope['id'],
                organization_id=fixture['organization'],
                organization__title='NER review priority synthetic scope',
            )
            manager = project.created_by
            task = Task.objects.create(project=project, data={'text': TEXT})
            rows = []
            for name, spans in (('a', [(0, 5, 'PER')]), ('b', [])):
                user = User.objects.get(pk=fixture['users'][name]['id'])
                row = source(project, task, user, spans)
                row.annotation.project = project
                row.annotation.save(update_fields=['project'])
                rows.append({'id': row.pk, 'assignment_id': row.assignment_id, 'owner': name, 'task_id': task.pk})
            scope['cases']['empty_winner'] = {'task': task.pk, 'sources': rows}
            plan, _ = ner_batches.build_plan(project, [c['task'] for c in scope['cases'].values()])
            principal, now = local_principal(manager), timezone.now().isoformat()
            manifest = {
                **plan,
                'schema': ner_batches.SCHEMA,
                'project_id': project.pk,
                'organization_id': project.organization_id,
                'creator_principal': principal.principal_id,
                'creator_source': principal.source,
                'actor_id_frozen': manager.pk,
                'name': 'NER handoff frozen inputs',
                'scope_enumerated_at': now,
                'calculated_at': now,
            }
            batch = NerBatch.objects.create(
                project=project,
                created_by=manager,
                actor_id_frozen=manager.pk,
                creator_principal=principal.principal_id,
                name=manifest['name'],
                idempotency_key=uuid4(),
                request_hash=ner.digest({'synthetic_handoff': project.pk}),
                manifest=manifest,
                manifest_hash=ner.digest(manifest),
            )
            scope['analysis'] = {'id': batch.pk, 'manifest_hash': batch.manifest_hash}
        output.write_text(json.dumps(fixture, ensure_ascii=False), encoding='utf-8')
        self.stdout.write('Synthetic NER handoff fixture written')

    def transition(self, output, locale, operation):
        fixture = json.loads(output.read_text(encoding='utf-8'))
        if fixture.get('schema') != SCHEMA:
            raise CommandError('Unexpected synthetic fixture')
        project = Project.objects.get(
            pk=fixture['projects'][locale]['id'],
            organization_id=fixture['organization'],
            organization__title='NER review priority synthetic scope',
            created_by__email='manager@ner-review-priority81.example.test',
        )
        if operation == 'revoke_reviewer':
            ProjectMember.objects.filter(project=project, user_id=fixture['users']['reviewer']['id']).update(
                enabled=False
            )
        else:
            row = Submission.objects.get(
                pk=fixture['projects'][locale]['cases']['conflict']['sources'][0]['id'], assignment__project=project
            )
            Submission.objects.filter(pk=row.pk).update(
                result_hash='0' * 64 if operation == 'corrupt_source' else _snapshot_hash(row.result_snapshot)
            )
        self.stdout.write(json.dumps({'operation': operation, 'project_id': project.pk, 'synthetic': True}))
