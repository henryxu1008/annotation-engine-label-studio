"""Fresh synthetic-only frozen analysis for optional priority sampling."""

import json
from pathlib import Path
from uuid import uuid4

from access_control.project_operations import local_principal
from classification_disagreements import batches
from classification_disagreements.models import ClassificationBatch
from django.db import transaction
from django.utils import timezone
from projects.models import Project, ProjectMember
from review_batches.service import digest
from tasks.models import Submission
from tasks.submissions import create_submission

from .seed_classification_e2e import Command as ClassificationFixture


class Command(ClassificationFixture):
    domain = 'review84-priority.example.test'
    prefix = 'review84-priority'
    fixture_schema = 'review-priority-e2e/v1'
    default_output = 'web/apps/labelstudio-e2e/.review-priority-e2e.json'

    @transaction.atomic
    def handle(self, *args, **options):
        super().handle(*args, **options)
        output = Path(options['output'])
        data = json.loads(output.read_text(encoding='utf-8'))
        for scope in data['projects'].values():
            project = Project.objects.get(pk=scope['id'])
            manager = project.created_by
            ids = [scope['cases'][case]['task'] for case in ('agreement', 'split', 'missing')]
            now = timezone.now().isoformat()
            manifest = {
                **batches.build_plan(project, ids),
                'schema': batches.SCHEMA,
                'project_id': project.pk,
                'organization_id': project.organization_id,
                'creator_principal': local_principal(manager).principal_id,
                'actor_id_frozen': manager.pk,
                'name': 'Frozen classification 分歧依据',
                'scope_enumerated_at': now,
                'calculated_at': now,
            }
            batch = ClassificationBatch.objects.create(
                project=project,
                created_by=manager,
                creator_principal=manifest['creator_principal'],
                actor_id_frozen=manager.pk,
                name=manifest['name'],
                idempotency_key=uuid4(),
                request_hash=digest({'synthetic': project.pk}),
                manifest=manifest,
                manifest_hash=digest(manifest),
            )
            scope['analysis'] = {'id': batch.pk, 'manifest_hash': batch.manifest_hash}
            # A legitimate synthetic resubmission after the analysis demonstrates
            # revision mismatch without modifying the historical snapshot/hash.
            source = Submission.objects.select_related('assignment', 'annotation', 'submitted_by').get(
                pk=scope['cases']['agreement']['sources'][0]
            )
            newer = create_submission(
                assignment=source.assignment, annotation=source.annotation, actor=source.submitted_by
            )
            scope['newer'] = newer.pk
            # A frozen participant promoted to Reviewer must not learn the
            # comparison through priority metadata or batch-context commands.
            ProjectMember.objects.filter(project=project, user_id=data['users']['c']['id']).update(role='reviewer')
        output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
