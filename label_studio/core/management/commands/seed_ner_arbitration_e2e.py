"""Fresh synthetic NER arbitration fixture, never a retained runtime tool."""

import json
import secrets
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from organizations.models import Organization
from projects.models import Project, ProjectMember
from tasks.models import Submission, Task
from tasks.submissions import _snapshot_hash
from users.models import User

from .seed_ner_e2e import TEXT, XML, source

DOMAIN = 'ner-arbitration81.example.test'
SCHEMA = 'ner-arbitration-e2e/v1'


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--output', default='web/apps/labelstudio-e2e/.ner-arbitration-e2e.json')
        parser.add_argument(
            '--operation',
            default='seed',
            choices=['seed', 'new_revision', 'corrupt_source', 'restore_source', 'revoke_reviewer'],
        )
        parser.add_argument('--locale', choices=['en-US', 'zh-CN'], default='en-US')

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.TEST_ENVIRONMENT:
            raise CommandError('Synthetic TEST_ENVIRONMENT required')
        output = Path(options['output'])
        if options['operation'] != 'seed':
            return self.transition(output, options['locale'], options['operation'])
        if output.exists() or User.objects.filter(email__endswith='@' + DOMAIN).exists():
            raise CommandError('Fresh synthetic database and output required')
        password = secrets.token_urlsafe(24)
        manager = User.objects.create_user('manager@' + DOMAIN, password, username='NER-arbitration-manager')
        org = Organization.create_organization(created_by=manager, title='NER arbitration synthetic scope')
        manager.active_organization = org
        manager.save(update_fields=['active_organization'])
        users = {'manager': manager}
        for name in ('reviewer', 'a', 'b'):
            user = User.objects.create_user(
                name + '@' + DOMAIN, password, username='NER-arbitration-' + name, active_organization=org
            )
            org.add_user(user)
            users[name] = user
        projects, modes = {}, {}
        for locale in ('en-US', 'zh-CN'):
            project = Project.objects.create(
                title='NER arbitration ' + locale,
                organization=org,
                created_by=manager,
                label_config=XML,
                is_published=True,
                enable_empty_annotation=True,
            )
            for name, user in users.items():
                ProjectMember.objects.create(
                    project=project, user=user, role=name if name in ('manager', 'reviewer') else 'annotator'
                )
            cases = {}
            for kind in ('empty_winner', 'source', 'revocation'):
                task = Task.objects.create(project=project, data={'text': TEXT})
                left = source(project, task, users['a'], [(0, 5, 'PER')])
                right = source(project, task, users['b'], [] if kind == 'empty_winner' else [(0, 4, 'PER')])
                cases[kind] = {'task': task.pk, 'left': left.pk, 'right': right.pk}
            projects[locale] = {'id': project.pk, 'cases': cases}
            modes[f'{org.pk}:{project.pk}'] = 'fork_review'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    'schema': SCHEMA,
                    'password': password,
                    'organization': org.pk,
                    'users': {k: {'id': u.pk, 'email': u.email} for k, u in users.items()},
                    'projects': projects,
                    'modes': modes,
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        self.stdout.write('Synthetic NER arbitration fixture written')

    def transition(self, output, locale, operation):
        fixture = json.loads(output.read_text(encoding='utf-8'))
        if fixture.get('schema') != SCHEMA:
            raise CommandError('Unexpected fixture schema')
        scope = fixture['projects'][locale]
        project = Project.objects.get(
            pk=scope['id'],
            organization_id=fixture['organization'],
            organization__title='NER arbitration synthetic scope',
            created_by__email='manager@' + DOMAIN,
        )
        if operation == 'revoke_reviewer':
            ProjectMember.objects.filter(project=project, user_id=fixture['users']['reviewer']['id']).update(
                enabled=False
            )
        else:
            case = scope['cases']['source']
            row = Submission.objects.get(
                pk=case['left'],
                assignment__project=project,
                assignment__task_id=case['task'],
                submitted_by__email='a@' + DOMAIN,
            )
            if operation == 'new_revision':
                source(
                    project,
                    row.assignment.task,
                    row.submitted_by,
                    [],
                    assignment=row.assignment,
                    revision=row.revision + 1,
                )
            elif operation == 'corrupt_source':
                Submission.objects.filter(pk=row.pk).update(result_hash='0' * 64)
            elif operation == 'restore_source':
                if row.result_hash != '0' * 64:
                    raise CommandError('Expected owned source corruption')
                Submission.objects.filter(pk=row.pk).update(result_hash=_snapshot_hash(row.result_snapshot))
        self.stdout.write(json.dumps({'ok': True, 'project': project.pk, 'operation': operation}))
