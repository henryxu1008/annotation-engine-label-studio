"""Fresh synthetic-only NER fixtures and tightly scoped CI fixture transitions."""
import json
import secrets
from pathlib import Path

from classification_disagreements.ner import MAX_SNAPSHOT_BYTES
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from organizations.models import Organization
from projects.models import Project, ProjectMember
from tasks.models import Annotation, Submission, Task, TaskAssignment
from tasks.submissions import _canonical_snapshot, _snapshot_hash
from users.models import User

DOMAIN = 'ner81.example.test'
XML = '<View><Text name="text" value="$text" granularity="symbol"/><Labels name="ner" toName="text"><Label value="PER"/><Label value="ORG"/><Label value="人"/></Labels></View>'
TEXT = 'Alice joined Acme 😀 李'
UNICODE = 'A😀李 e\u0301\r\n尾'


def source(project, task, user, spans, *, assignment=None, revision=1, oversized=False):
    assignment = assignment or TaskAssignment.objects.create(project=project, task=task, assignee=user)
    text = task.data['text']
    result = [{'id': f'entity-{i}', 'from_name': 'ner', 'to_name': 'text', 'type': 'labels',
               'value': {'start': a, 'end': b, 'text': text[a:b], 'labels': [label]}}
              for i, (a, b, label) in enumerate(spans)]
    annotation = Annotation.objects.create(task=task, completed_by=user, result=result)
    snapshot = _canonical_snapshot(annotation, assignment)
    if oversized:
        snapshot['synthetic_padding'] = 'x' * MAX_SNAPSHOT_BYTES
    return Submission.objects.create(assignment=assignment, annotation=annotation, submitted_by=user,
                                     revision=revision, result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--output', default='web/apps/labelstudio-e2e/.ner-e2e.json')
        parser.add_argument('--operation', choices=['seed', 'delete_source', 'new_revision', 'revoke_reviewer'], default='seed')
        parser.add_argument('--locale', choices=['en-US', 'zh-CN'], default='en-US')

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.TEST_ENVIRONMENT:
            raise CommandError('Synthetic TEST_ENVIRONMENT required')
        output = Path(options['output'])
        if options['operation'] != 'seed':
            return self.transition(output, options['locale'], options['operation'])
        if output.exists() or User.objects.filter(email__endswith='@' + DOMAIN).exists():
            raise CommandError('Fresh synthetic output and database required')
        password = secrets.token_urlsafe(24)
        manager = User.objects.create_user('manager@' + DOMAIN, password, username='ner-manager')
        org = Organization.create_organization(created_by=manager, title='NER synthetic scope')
        manager.active_organization = org
        manager.save(update_fields=['active_organization'])
        users = {'manager': manager}
        for role in ('reviewer', 'a', 'b', 'c'):
            user = User.objects.create_user(role + '@' + DOMAIN, password, username='ner-' + role, active_organization=org)
            org.add_user(user)
            users[role] = user
        projects, modes = {}, {}
        for locale in ('en-US', 'zh-CN'):
            project = Project.objects.create(title='NER ' + locale, created_by=manager, organization=org,
                                             label_config=XML, is_published=True, enable_empty_annotation=True)
            for role, user in users.items():
                ProjectMember.objects.create(project=project, user=user,
                    role=role if role in ('manager', 'reviewer') else 'annotator')
            cases = {}
            for case in ('differences', 'empty', 'source_loss', 'new_revision', 'budget', 'self', 'producer', 'interpolated'):
                data = {'text': UNICODE if case == 'producer' else TEXT}
                if case == 'interpolated':
                    project.label_config = XML.replace('value="$text"', 'value="$text suffix"')
                    project.save()
                    data = {'text': 'Alice', 'text suffix': 'Alice hidden context'}
                task = Task.objects.create(project=project, data=data)
                owners = ('b',) if case == 'producer' else ('a', 'b') if case == 'empty' else ('a', 'b', 'c')
                cases[case] = {'task': task.pk, 'sources': {}}
                if case == 'self':
                    owners = ('a', 'b', 'reviewer')
                for owner in owners:
                    spans = [] if case == 'empty' else [(0, 5, 'PER'), (13, 17 if owner == 'a' else 16, 'ORG')]
                    if owner == 'c':
                        spans = [(0, 5, 'PER')]
                    if case == 'producer':
                        spans = [(1, 2, 'PER'), (8, 9, 'PER')]
                    if case == 'interpolated':
                        spans = [(0, 5, 'PER')]
                    row = source(project, task, users[owner], spans, oversized=case == 'budget' and owner == 'a')
                    cases[case]['sources'][owner] = row.pk
                if case == 'producer':
                    assignment = TaskAssignment.objects.create(project=project, task=task, assignee=users['a'])
                    cases[case]['assignment_a'] = assignment.pk
                if case == 'interpolated':
                    project.label_config = XML
                    project.save()
            projects[locale] = {'id': project.pk, 'cases': cases}
            modes[f'{org.pk}:{project.pk}'] = 'fork_review'
        value = {'schema': 'ner-e2e/v1', 'password': password, 'organization': org.pk,
                 'users': {k: {'id': v.pk, 'email': v.email} for k, v in users.items()}, 'projects': projects, 'modes': modes}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        self.stdout.write('Synthetic NER fixture written: ' + str(output))

    def transition(self, output, locale, operation):
        if not output.is_file():
            raise CommandError('Owned NER fixture required')
        value = json.loads(output.read_text(encoding='utf-8'))
        if value.get('schema') != 'ner-e2e/v1':
            raise CommandError('Unexpected fixture schema')
        scope = value['projects'][locale]
        project = Project.objects.get(pk=scope['id'], organization_id=value['organization'],
                                      organization__title='NER synthetic scope', created_by__email='manager@' + DOMAIN)
        if operation == 'revoke_reviewer':
            ProjectMember.objects.filter(project=project, user_id=value['users']['reviewer']['id']).update(enabled=False)
        else:
            case = scope['cases']['source_loss' if operation == 'delete_source' else 'new_revision']
            row = Submission.objects.get(pk=case['sources']['a'], assignment__project=project,
                                         assignment__task_id=case['task'], submitted_by__email='a@' + DOMAIN)
            if operation == 'delete_source':
                row.delete()
            else:
                source(project, row.assignment.task, row.submitted_by, [], assignment=row.assignment, revision=row.revision + 1)
        self.stdout.write(json.dumps({'ok': True, 'operation': operation, 'project': project.pk}))
