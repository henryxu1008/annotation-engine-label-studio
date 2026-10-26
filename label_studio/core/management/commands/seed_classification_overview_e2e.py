"""Synthetic-only old-format batches for the independently gated overview."""
import copy
import json
from pathlib import Path
from uuid import uuid4

from access_control.project_operations import local_principal
from classification_disagreements import batches
from classification_disagreements.models import ClassificationBatch
from django.db import transaction
from django.utils import timezone
from projects.models import Project
from review_batches.service import digest
from tasks.models import Submission
from tasks.submissions import _snapshot_hash

from .seed_classification_e2e import Command as Fixture


class Command(Fixture):
    domain = 'classification81-overview.example.test'
    prefix = 'class81-overview'
    fixture_schema = 'classification-overview-e2e/v1'
    default_output = 'web/apps/labelstudio-e2e/.classification-overview-e2e.json'
    extra_cases = ('empty', 'self', 'unsubmitted', 'budget')

    @transaction.atomic
    def handle(self, *args, **options):
        super().handle(*args, **options)
        output = Path(options['output'])
        data = json.loads(output.read_text(encoding='utf-8'))
        config = ('<View><Text name="text" value="$text"/><Choices name="sentiment" toName="text" choice="multiple">'
                  '<Choice value="A"/><Choice value="B"/><Choice value="C"/></Choices></View>')
        for scope in data['projects'].values():
            project = Project.objects.get(pk=scope['id'])
            for case in ('agreement', 'split', 'empty', 'self', 'unsubmitted', 'budget'):
                ids = scope['cases'][case]['sources']
                if case == 'agreement':
                    Submission.objects.get(pk=ids.pop()).assignment.delete()
                for index, source_id in enumerate(ids):
                    source = Submission.objects.get(pk=source_id)
                    snapshot = copy.deepcopy(source.result_snapshot)
                    snapshot['project']['label_config'] = config
                    labels = (['A', 'B'], ['B', 'C'], ['B'])[index] if case == 'split' else ['A']
                    snapshot['annotation']['result'][0]['value']['choices'] = [] if case == 'empty' else labels
                    Submission.objects.filter(pk=source_id).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
            def batch(name, cases):
                now = timezone.now().isoformat()
                ids = sorted(scope['cases'][case]['task'] for case in cases)
                manifest = {**batches.build_plan(project, ids), 'schema': batches.SCHEMA, 'project_id': project.pk,
                            'organization_id': project.organization_id, 'creator_principal': local_principal(project.created_by).principal_id,
                            'actor_id_frozen': project.created_by_id, 'name': name, 'scope_enumerated_at': now, 'calculated_at': now}
                row = ClassificationBatch.objects.create(project=project, created_by=project.created_by,
                    creator_principal=manifest['creator_principal'], actor_id_frozen=project.created_by_id, name=name,
                    idempotency_key=uuid4(), request_hash=digest({'synthetic': name}), manifest=manifest, manifest_hash=digest(manifest))
                return {'id': row.pk, 'name': name, 'manifest_hash': row.manifest_hash}
            scope['batch'] = batch('Frozen mixed overview', ('agreement', 'split', 'unsubmitted'))
            scope['self_batch'] = batch('Reviewer subset overview', ('agreement', 'split', 'self'))
            scope['empty_batch'] = batch('No comparable overview', ('empty',))
            scope['budget_batch'] = batch('Synthetic oversized source', ('budget',))
            source = Submission.objects.get(pk=scope['cases']['split']['sources'][0])
            snapshot = copy.deepcopy(source.result_snapshot)
            snapshot['annotation']['result'][0]['value']['choices'] = ['C']
            newer = Submission.objects.create(assignment=source.assignment, annotation=source.annotation,
                revision=2, submitted_by=source.submitted_by, result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
            scope['newer_revision'] = newer.pk
            # Intentional corruption only in this disposable CI fixture: demonstrate the
            # shared snapshot-byte guard before an over-budget live source is retained.
            source = Submission.objects.get(pk=scope['cases']['budget']['sources'][0])
            snapshot = copy.deepcopy(source.result_snapshot)
            snapshot['task']['meta']['padding'] = '界' * (batches.MAX_BYTES // 3 + 1)
            Submission.objects.filter(pk=source.pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
        output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
