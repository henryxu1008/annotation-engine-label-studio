"""Explicit synthetic fixtures for package discovery; never reset existing data."""

import json
import os
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from organizations.models import Organization
from projects.models import Project, ProjectMember
from rest_framework.test import APIClient
from tasks.models import Annotation, ReviewDecision, Task, TaskAssignment
from tasks.submissions import create_submission, review_submission
from users.models import User


class Command(BaseCommand):
    help = 'Create separate synthetic package-discovery browser fixtures without deleting existing data.'

    def add_arguments(self, parser):
        parser.add_argument('--output', required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        output = Path(options['output'])
        if not settings.TEST_ENVIRONMENT or not settings.ANNOTATION_FIXED_PACKAGES_ENABLED:
            raise CommandError('Explicit synthetic test settings and fixed packages are required.')
        if output.exists() or User.objects.filter(email__endswith='@package83.example.test').exists():
            raise CommandError('Fixture identity/output already exists; existing data is preserved.')
        password = 'Package83Synthetic!20261024'
        manager = User.objects.create_user('manager@package83.example.test', password, username='package83-manager')
        organization = Organization.create_organization(created_by=manager, title='Package83 synthetic discovery')
        manager.active_organization = organization
        manager.save(update_fields=['active_organization'])
        users = {'manager': manager}
        for role in ['annotator', 'other', 'reviewer']:
            user = User.objects.create_user(
                f'{role}@package83.example.test',
                password,
                username=f'package83-{role}',
                active_organization=organization,
            )
            organization.add_user(user)
            users[role] = user
        config = (
            '<View><Text name="text" value="$text"/><Choices name="sentiment" toName="text">'
            '<Choice value="Positive"/><Choice value="Negative"/></Choices></View>'
        )
        result = [
            {
                'id': 'package83-choice',
                'from_name': 'sentiment',
                'to_name': 'text',
                'type': 'choices',
                'value': {'choices': ['Positive']},
            }
        ]
        projects = {}
        client = APIClient()
        client.force_authenticate(manager)
        for locale in ['en-US', 'zh-CN']:
            project = Project.objects.create(
                title=f'Package discovery {locale}',
                organization=organization,
                created_by=manager,
                label_config=config,
                is_published=True,
                enable_empty_annotation=True,
            )
            for key, user in users.items():
                ProjectMember.objects.create(project=project, user=user, role='annotator' if key == 'other' else key)
            tasks, assignments, submissions = {}, {}, {}
            for key in ['assigned', 'shared_rework', 'pending', 'private_b']:
                task = Task.objects.create(project=project, data={'text': f'Synthetic 样本 {locale} {key}'})
                owner = users['other'] if key == 'private_b' else users['annotator']
                assignment = TaskAssignment.objects.create(
                    project=project, task=task, assignee=owner, assigned_by=manager
                )
                tasks[key], assignments[key] = task, assignment
                if key in ['shared_rework', 'pending']:
                    annotation = Annotation.objects.create(
                        task=task, project=project, completed_by=owner, result=result
                    )
                    assignment.annotation = annotation
                    assignment.save(update_fields=['annotation', 'updated_at'])
                    submission = create_submission(assignment=assignment, annotation=annotation, actor=owner)
                    submissions[key] = submission
                    if key == 'shared_rework':
                        review_submission(
                            submission=submission,
                            reviewer=users['reviewer'],
                            decision=ReviewDecision.Decision.REJECTED,
                            reason='Synthetic correction 合成返工理由',
                        )
            TaskAssignment.objects.create(
                project=project, task=tasks['shared_rework'], assignee=users['other'], assigned_by=manager
            )
            packages = {}
            for key, members in [
                ('A', ['assigned', 'shared_rework', 'pending']),
                ('B', ['shared_rework', 'pending', 'private_b']),
            ]:
                response = client.post(
                    f'/api/projects/{project.pk}/task-packages/',
                    {
                        'name': f'{locale} Package {key}',
                        'instructions': '建议规则 v0.1 · synthetic only',
                        'source_note': 'Synthetic source; managers only',
                        'task_ids': [tasks[member].pk for member in members],
                    },
                    format='json',
                    HTTP_IDEMPOTENCY_KEY=str(uuid4()),
                )
                if response.status_code != 201:
                    raise CommandError(f'Existing package creation failed: {response.status_code}')
                packages[key] = response.json()['id']
            project.summary.update_data_columns(list(tasks.values()))
            projects[locale] = {
                'id': project.pk,
                'packages': packages,
                'tasks': {key: task.pk for key, task in tasks.items()},
                'assignments': {key: row.pk for key, row in assignments.items()},
                'submissions': {key: row.pk for key, row in submissions.items()},
            }
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as destination:
            json.dump(
                {
                    'synthetic': 'package-discovery/v1',
                    'password': password,
                    'users': {key: {'id': user.pk, 'email': user.email} for key, user in users.items()},
                    'projects': projects,
                },
                destination,
                indent=2,
            )
        self.stdout.write('Created separate synthetic package-discovery fixtures; no existing data deleted.')
