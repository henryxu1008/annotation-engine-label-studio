"""Add a separate synthetic fixture; never delete existing users or projects."""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from organizations.models import Organization
from projects.models import Project, ProjectMember
from tasks.models import Annotation, Task, TaskAssignment
from tasks.submissions import create_submission
from users.models import User


class Command(BaseCommand):
    help = 'Create isolated synthetic review sampling browser fixtures once.'

    def add_arguments(self, parser):
        parser.add_argument('--output', default='web/apps/labelstudio-e2e/.review84-e2e.json')

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.TEST_ENVIRONMENT:
            raise CommandError('TEST_ENVIRONMENT is required')
        output = Path(options['output'])
        if output.exists() or User.objects.filter(email__endswith='@review84.example.test').exists():
            raise CommandError('Existing fixture refused; use a fresh isolated database and output')
        password = 'SyntheticReview84!12345'
        manager = User.objects.create_user('manager@review84.example.test', password, username='review84-manager')
        org = Organization.create_organization(created_by=manager, title='Synthetic review sampling')
        manager.active_organization = org
        manager.save(update_fields=['active_organization'])
        users = {'manager': manager}
        for role in ('annotator', 'reviewer'):
            user = User.objects.create_user(f'{role}@review84.example.test', password,
                username=f'review84-{role}', active_organization=org)
            org.add_user(user)
            users[role] = user
        config = ('<View><Text name="text" value="$text"/><Choices name="sentiment" toName="text">'
                  '<Choice value="Positive"/><Choice value="Negative"/></Choices></View>')
        result = [{'id': 'review84-synthetic-choice', 'origin': 'manual', 'from_name': 'sentiment',
                   'to_name': 'text', 'type': 'choices',
                   'value': {'choices': ['Positive']}}]
        projects, modes = {}, {}
        for locale in ('en-US', 'zh-CN'):
            project = Project.objects.create(title=f'Review sampling {locale}', organization=org,
                created_by=manager, label_config=config, is_published=True, enable_empty_annotation=True)
            for role, user in users.items():
                ProjectMember.objects.create(project=project, user=user, role=role)
            sources = []
            tasks = []
            for index in range(4):
                task = Task.objects.create(project=project, data={'text': f'Synthetic sample 样本 {index + 1}'})
                tasks.append(task)
                assignment = TaskAssignment.objects.create(project=project, task=task,
                    assignee=users['annotator'], assigned_by=manager)
                annotation = Annotation.objects.create(task=task, project=project,
                    completed_by=users['annotator'], result=result)
                assignment.annotation = annotation
                assignment.save()
                submission = create_submission(assignment=assignment, annotation=annotation, actor=users['annotator'])
                sources.append({'id': submission.pk, 'task_id': task.pk, 'assignment_id': assignment.pk,
                    'annotation_id': annotation.pk, 'result_hash': submission.result_hash})
            project.summary.update_data_columns(tasks)
            projects[locale] = {'id': project.pk, 'sources': sources}
            modes[f'{org.pk}:{project.pk}'] = 'fork_review'
        data = {'synthetic': 'review-batches-e2e/v1', 'password': password,
                'users': {key: {'id': user.pk, 'email': user.email} for key, user in users.items()},
                'projects': projects, 'modes': modes}
        output.parent.mkdir(parents=True, exist_ok=True)
        # Refuse overwrite, including a racing writer or a symlink.
        with output.open('x', encoding='utf-8') as destination:
            json.dump(data, destination, indent=2)
        self.stdout.write('Created separate synthetic review sampling fixtures.')
