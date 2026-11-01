"""Fresh synthetic producer protocol fixture; never invokes an actual model."""
import json
import secrets
from pathlib import Path
from uuid import uuid4

from access_control.project_operations import (
    PROVIDE_REVIEW_CONFIDENCE,
    LegacyProjectOperationProvider,
    ProjectOperationPolicy,
)
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test import override_settings
from organizations.models import Organization
from projects.models import Project, ProjectMember
from rest_framework.test import APIClient
from review_batches import confidence_contract as c
from tasks.models import Annotation, Task, TaskAssignment
from tasks.submissions import create_submission
from users.models import User

DOMAIN = 'review-confidence84.example.test'
XML = '<View><Text name="text" value="$text"/><Choices name="label" toName="text"><Choice value="A"/><Choice value="B"/></Choices></View>'


class ConfidenceE2EPolicy:
    def evaluate(self, project):
        base = LegacyProjectOperationProvider().evaluate(project)
        if settings.TEST_ENVIRONMENT and project.created_by.email.endswith('@' + DOMAIN):
            return ProjectOperationPolicy(
                'synthetic-confidence/v1', 'active', base.allowed_operations | {PROVIDE_REVIEW_CONFIDENCE}
            )
        return base


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--output', default='web/apps/labelstudio-e2e/.review-confidence-e2e.json')

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.TEST_ENVIRONMENT:
            raise CommandError('Synthetic TEST_ENVIRONMENT required')
        output = Path(options['output'])
        if output.exists() or User.objects.filter(email__endswith='@' + DOMAIN).exists():
            raise CommandError('Fresh synthetic database and output required')
        password = secrets.token_urlsafe(24)
        manager = User.objects.create_user('manager@' + DOMAIN, password, username='confidence-manager')
        org = Organization.create_organization(created_by=manager, title='Synthetic confidence protocol scope')
        manager.active_organization = org
        manager.save(update_fields=['active_organization'])
        users = {'manager': manager}
        for name in ('producer', 'reviewer', 'a', 'b'):
            user = User.objects.create_user(
                name + '@' + DOMAIN, password, username='confidence-' + name, active_organization=org
            )
            org.add_user(user)
            users[name] = user
        instance, namespace, profile_id = str(uuid4()), str(uuid4()), str(uuid4())
        descriptor = {
            'schema': c.PROFILE,
            'profile_id': profile_id,
            'producer_namespace': namespace,
            'label': 'Synthetic 修订置信',
            'version': 'synthetic-v1',
            'evaluator_id': 'synthetic-offline',
            'evaluator_version': '1',
            'model_id': 'no-model-executed',
            'model_artifact_sha256': 'a' * 64,
            'score_definition': c.SCORE_DEFINITION,
            'calibration_version': 'synthetic-v1',
            'integer_rule': c.INTEGER_RULE,
            'input_contract': c.INPUT_CONTRACT,
        }
        digest = c.digest(descriptor)
        projects, modes, profiles, grants = {}, {}, [], []
        for locale in ('en-US', 'zh-CN'):
            project = Project.objects.create(
                title='Confidence protocol ' + locale,
                organization=org,
                created_by=manager,
                label_config=XML,
                is_published=True,
                enable_empty_annotation=True,
            )
            for name, user in users.items():
                ProjectMember.objects.create(
                    project=project,
                    user=user,
                    role='manager' if name in ('manager', 'producer') else 'annotator',
                )
            modes[f'{org.pk}:{project.pk}'] = 'fork_review'
            profiles.append(
                {
                    'project_id': project.pk,
                    'organization_id': org.pk,
                    'descriptor': descriptor,
                    'digest': digest,
                    'fresh': True,
                }
            )
            grants.append(
                {
                    'project_id': project.pk,
                    'organization_id': org.pk,
                    'principal_id': f'label_studio_local:{users["producer"].pk}',
                    'source': 'label_studio_local',
                    'actor_id': users['producer'].pk,
                    'producer_namespace': namespace,
                    'profile_ids': [profile_id],
                }
            )
            sources = []
            for owner, score in (('a', 0), ('b', 100), ('a', 900000), ('b', None), ('reviewer', 20)):
                task = Task.objects.create(project=project, data={'text': 'Synthetic 冻结文本 <b>literal</b>\n第二行'})
                assignment = TaskAssignment.objects.create(
                    project=project, task=task, assignee=users[owner], assigned_by=manager
                )
                annotation = Annotation.objects.create(
                    project=project,
                    task=task,
                    completed_by=users[owner],
                    result=[
                        {
                            'id': f'confidence-{task.pk}',
                            'origin': 'manual',
                            'from_name': 'label',
                            'to_name': 'text',
                            'type': 'choices',
                            'value': {'choices': ['A']},
                        }
                    ],
                )
                assignment.annotation = annotation
                assignment.save(update_fields=['annotation'])
                row = create_submission(assignment=assignment, annotation=annotation, actor=users[owner])
                sources.append(
                    {
                        'id': row.pk,
                        'assignment_id': assignment.pk,
                        'annotation_id': annotation.pk,
                        'task_id': task.pk,
                        'owner': owner,
                        'score': score,
                    }
                )
            ProjectMember.objects.filter(project=project, user=users['reviewer']).update(role='reviewer')
            project.summary.update_data_columns(list(Task.objects.filter(project=project)))
            projects[locale] = {'id': project.pk, 'sources': sources}
        configured = dict(
            ANNOTATION_REVIEW_BATCHES_ENABLED=True,
            ANNOTATION_REVIEW_CONFIDENCE_ENABLED=True,
            ANNOTATION_REVIEW_CONFIDENCE_INSTANCE_ID=instance,
            ANNOTATION_REVIEW_CONFIDENCE_PROFILES=profiles,
            ANNOTATION_REVIEW_CONFIDENCE_GRANTS=grants,
            ANNOTATION_REVIEW_BATCH_MODES=modes,
            ANNOTATION_PROJECT_OPERATION_PROVIDER='core.management.commands.seed_review_confidence_e2e.ConfidenceE2EPolicy',
        )
        client = APIClient()
        client.force_authenticate(users['producer'])
        with override_settings(**configured):
            for scope in projects.values():
                for source in scope['sources']:
                    if source['score'] is None:
                        continue
                    base = f'/api/projects/{scope["id"]}/'
                    result = client.get(
                        base + f'review-confidence-inputs/{source["id"]}/',
                        {'profile_id': profile_id, 'profile_digest': digest},
                    )
                    if result.status_code != 200:
                        raise CommandError(f'Synthetic input rejected: {result.status_code}')
                    body = {
                        'submission_id': source['id'],
                        'profile_id': profile_id,
                        'profile_digest': digest,
                        'evaluated_input_hash': result.data['input_hash'],
                        'score_units': source['score'],
                        'evaluated_at': '2026-11-01T00:00:00Z',
                    }
                    key = str(uuid4())
                    result = client.post(
                        base + 'review-confidence-observations/', body, format='json', HTTP_IDEMPOTENCY_KEY=key
                    )
                    if result.status_code != 201:
                        raise CommandError(f'Synthetic observation rejected: {result.status_code}')
                    source['event'], source['body'] = key, body
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as destination:
            destination.write(
                json.dumps(
                    {
                        'schema': 'review-confidence-e2e/v1',
                        'password': password,
                        'users': {name: {'id': u.pk, 'email': u.email} for name, u in users.items()},
                        'projects': projects,
                        'settings': configured,
                        'profile_id': profile_id,
                        'profile_digest': digest,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        self.stdout.write('Fresh synthetic protocol fixture written; no model was invoked.')
