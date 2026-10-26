"""Fresh synthetic projections, including an unchanged exact-set v1 batch."""
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
from tasks.models import Annotation, Submission, Task, TaskAssignment
from tasks.submissions import _canonical_snapshot, _snapshot_hash
from users.models import User

from .seed_classification_e2e import Command as ClassificationFixture


class Command(ClassificationFixture):
    domain = 'classification81-partial.example.test'
    prefix = 'class81-partial'
    fixture_schema = 'classification-partial-e2e/v1'
    default_output = 'web/apps/labelstudio-e2e/.classification-partial-e2e.json'
    extra_cases = ('empty', 'self', 'unsubmitted')

    @transaction.atomic
    def handle(self, *args, **options):
        super().handle(*args, **options)
        output = Path(options['output'])
        data = json.loads(output.read_text(encoding='utf-8'))
        config = ('<View><Text name="text" value="$text"/><Choices name="sentiment" toName="text" choice="multiple">'
                  '<Choice value="A"/><Choice value="B"/><Choice value="C"/></Choices></View>')
        for scope in data['projects'].values():
            project = Project.objects.get(pk=scope['id'])
            for case in ('agreement', 'split', 'empty', 'self', 'unsubmitted'):
                for index, pk in enumerate(scope['cases'][case]['sources']):
                    source = Submission.objects.get(pk=pk)
                    snapshot = source.result_snapshot
                    snapshot['project']['label_config'] = config
                    labels = (['A', 'B'], ['B', 'C'], ['B'])[index] if case == 'split' else ['A']
                    snapshot['annotation']['result'][0]['value']['choices'] = [] if case == 'empty' else labels
                    Submission.objects.filter(pk=pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
            manager = project.created_by
            task = Task.objects.create(project=project, data={'text': 'Synthetic budget boundary'})
            labels = [f'Budget-{i}-' + 'x' * 500 for i in range(100)]
            budget_config = ('<View><Text name="text" value="$text"/><Choices name="sentiment" toName="text" choice="multiple">'
                             + ''.join(f'<Choice value="{label}"/>' for label in labels) + '</Choices></View>')
            source_ids = []
            for index in range(20):
                actor = User.objects.create_user(f'budget-{project.pk}-{index}@{self.domain}', data['password'],
                                                username=f'partial-budget-{project.pk}-{index}', active_organization=project.organization)
                project.organization.add_user(actor)
                ProjectMember.objects.create(project=project, user=actor, role='annotator')
                assignment = TaskAssignment.objects.create(project=project, task=task, assignee=actor, assigned_by=manager)
                annotation = Annotation.objects.create(project=project, task=task, completed_by=actor, result=[{
                    'from_name': 'sentiment', 'to_name': 'text', 'type': 'choices', 'value': {'choices': labels[index:index + 50]}}])
                assignment.annotation = annotation
                assignment.save()
                snapshot = _canonical_snapshot(annotation, assignment)
                snapshot['project']['label_config'] = budget_config
                source = Submission.objects.create(assignment=assignment, annotation=annotation, revision=1,
                    submitted_by=actor, result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
                source_ids.append(source.pk)
            scope['cases']['budget'] = {'task': task.pk, 'sources': source_ids}
            now = timezone.now().isoformat()
            ids = sorted(scope['cases'][name]['task'] for name in ('agreement', 'split', 'empty', 'self', 'unsubmitted', 'budget'))
            manifest = {**batches.build_plan(project, ids), 'schema': batches.SCHEMA, 'project_id': project.pk,
                        'organization_id': project.organization_id, 'creator_principal': local_principal(manager).principal_id,
                        'actor_id_frozen': manager.pk, 'name': 'Existing exact-set v1 batch',
                        'scope_enumerated_at': now, 'calculated_at': now}
            batch = ClassificationBatch.objects.create(project=project, created_by=manager,
                creator_principal=manifest['creator_principal'], actor_id_frozen=manager.pk, name=manifest['name'],
                idempotency_key=uuid4(), request_hash=digest({'synthetic': project.pk}),
                manifest=manifest, manifest_hash=digest(manifest))
            scope['batch'] = {'id': batch.pk, 'name': batch.name, 'manifest_hash': batch.manifest_hash}
        output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
