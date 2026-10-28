"""Owned, synthetic-only NER overview fixtures; never a retained runtime command."""
import json
import secrets
from pathlib import Path

from classification_disagreements.ner import MAX_SNAPSHOT_BYTES
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from organizations.models import Organization
from projects.models import Project, ProjectMember
from tasks.models import Submission, Task, TaskAssignment
from tasks.submissions import _snapshot_hash
from users.models import User

from .seed_ner_e2e import TEXT, XML, source

DOMAIN = 'ner-overview81.example.test'
SCHEMA = 'ner-overview-e2e/v1'


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--output', default='web/apps/labelstudio-e2e/.ner-overview-e2e.json')
        parser.add_argument('--operation', default='seed', choices=['seed', 'new_revision', 'complete_missing',
            'corrupt_source', 'restore_source', 'grow_source', 'shrink_source', 'revoke_reviewer'])
        parser.add_argument('--locale', choices=['en-US', 'zh-CN'], default='en-US')

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.TEST_ENVIRONMENT:
            raise CommandError('Synthetic TEST_ENVIRONMENT required')
        output = Path(options['output'])
        if options['operation'] != 'seed':
            return self.transition(output, options['locale'], options['operation'])
        if output.exists() or User.objects.filter(email__endswith='@'+DOMAIN).exists():
            raise CommandError('Fresh synthetic database and output required')
        password = secrets.token_urlsafe(24)
        manager = User.objects.create_user('manager@'+DOMAIN, password, username='ner-quality-manager')
        org = Organization.create_organization(created_by=manager, title='NER overview synthetic scope')
        manager.active_organization = org
        manager.save(update_fields=['active_organization'])
        users = {'manager': manager}
        for role in ('reviewer', 'a', 'b', 'c'):
            user = User.objects.create_user(role+'@'+DOMAIN, password, username='ner-quality-'+role, active_organization=org)
            org.add_user(user)
            users[role] = user
        pagers = []
        for index in range(52):
            user = User.objects.create_user(f'pager{index}@{DOMAIN}', None, username=f'ner-quality-pager{index}', active_organization=org)
            org.add_user(user)
            pagers.append(user)
        projects, modes = {}, {}
        for locale in ('en-US', 'zh-CN'):
            project = Project.objects.create(title='NER overview '+locale, created_by=manager, organization=org,
                                             label_config=XML, is_published=True, enable_empty_annotation=True)
            for role, user in users.items():
                ProjectMember.objects.create(project=project, user=user,
                    role=role if role in ('manager', 'reviewer') else 'annotator')
            for user in pagers:
                ProjectMember.objects.create(project=project, user=user, role='annotator')
            cases = {}
            for kind in ('equal', 'mixed', 'empty', 'missing', 'self'):
                task = Task.objects.create(project=project, data={'text': TEXT})
                owners = ('a', 'b', 'c') if kind == 'mixed' else ('a', 'reviewer') if kind == 'self' else ('a', 'b')
                case = {'task': task.pk, 'sources': {}}
                for index, owner in enumerate(owners):
                    if kind == 'missing' and index == 1:
                        assignment = TaskAssignment.objects.create(project=project, task=task, assignee=users[owner])
                        case['missing_assignment'] = assignment.pk
                        continue
                    spans = [(0, 5, 'PER')]
                    if kind == 'empty' or kind == 'mixed' and index == 2:
                        spans = []
                    elif kind == 'mixed':
                        spans += [(13, 17, 'ORG')] if index == 0 else [(19, 20, '人')]
                    row = source(project, task, users[owner], spans)
                    case['sources'][owner] = row.pk
                cases[kind] = case
            pager_tasks = []
            for index in range(0, len(pagers), 2):
                task = Task.objects.create(project=project, data={'text': TEXT})
                for user in pagers[index:index+2]:
                    source(project, task, user, [(0, 5, 'PER')])
                pager_tasks.append(task.pk)
            projects[locale] = {'id': project.pk, 'cases': cases, 'pager_tasks': pager_tasks}
            modes[f'{org.pk}:{project.pk}'] = 'fork_review'
        fixture = {'schema': SCHEMA, 'password': password, 'organization': org.pk,
                   'users': {k: {'id': u.pk, 'email': u.email} for k, u in users.items()},
                   'projects': projects, 'modes': modes}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(fixture, ensure_ascii=False), encoding='utf-8')
        self.stdout.write('Synthetic NER overview fixture written')

    def transition(self, output, locale, operation):
        value = json.loads(output.read_text(encoding='utf-8'))
        if value.get('schema') != SCHEMA:
            raise CommandError('Unexpected fixture schema')
        scope = value['projects'][locale]
        project = Project.objects.get(pk=scope['id'], organization_id=value['organization'],
            organization__title='NER overview synthetic scope', created_by__email='manager@'+DOMAIN)
        if operation == 'revoke_reviewer':
            ProjectMember.objects.filter(project=project, user_id=value['users']['reviewer']['id']).update(enabled=False)
        elif operation == 'complete_missing':
            case = scope['cases']['missing']
            assignment = TaskAssignment.objects.get(pk=case['missing_assignment'], project=project,
                task_id=case['task'], assignee_id=value['users']['b']['id'])
            source(project, assignment.task, assignment.assignee, [], assignment=assignment)
        else:
            case = scope['cases']['equal']
            row = Submission.objects.get(pk=case['sources']['a'], assignment__project=project,
                assignment__task_id=case['task'], submitted_by__email='a@'+DOMAIN)
            if operation == 'new_revision':
                source(project, row.assignment.task, row.submitted_by, [], assignment=row.assignment, revision=row.revision+1)
            elif operation in ('grow_source', 'shrink_source'):
                snapshot = row.result_snapshot
                if operation == 'grow_source':
                    snapshot['synthetic_overview_padding'] = 'x'*MAX_SNAPSHOT_BYTES
                else:
                    snapshot.pop('synthetic_overview_padding')
                Submission.objects.filter(pk=row.pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
            else:
                expected = _snapshot_hash(row.result_snapshot)
                if operation == 'restore_source' and row.result_hash != '0'*64:
                    raise CommandError('Expected owned corruption before restoration')
                Submission.objects.filter(pk=row.pk).update(result_hash='0'*64 if operation == 'corrupt_source' else expected)
        self.stdout.write(json.dumps({'ok': True, 'operation': operation, 'project': project.pk}))
