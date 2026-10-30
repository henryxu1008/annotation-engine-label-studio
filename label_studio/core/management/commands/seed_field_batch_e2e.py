"""Fresh synthetic-only field comparison fixtures; never retained project data."""
import json
import secrets
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from organizations.models import Organization
from projects.models import Project, ProjectMember
from tasks.models import Annotation, Submission, Task, TaskAssignment
from tasks.submissions import _canonical_snapshot, _snapshot_hash
from users.models import User

DOMAIN = 'field-batches81.example.test'
XML = '<View><Text name="doc" value="$text"/><TextArea name="name" toName="doc" maxSubmissions="1"/><TextArea name="city" toName="doc" maxSubmissions="1"/><TextArea name="code" toName="doc" maxSubmissions="1"/></View>'


def source(project, task, user, values, *, assignment=None, revision=1):
    assignment = assignment or TaskAssignment.objects.create(project=project, task=task, assignee=user)
    result = [
        {'id': f'field-{i}', 'from_name': name, 'to_name': 'doc', 'type': 'textarea', 'value': {'text': [v]}}
        for i, (name, v) in enumerate(zip(('name', 'city', 'code'), values))
    ]
    annotation = Annotation.objects.create(task=task, completed_by=user, result=result)
    snapshot = _canonical_snapshot(annotation, assignment)
    return Submission.objects.create(
        assignment=assignment,
        annotation=annotation,
        submitted_by=user,
        revision=revision,
        result_snapshot=snapshot,
        result_hash=_snapshot_hash(snapshot),
    )


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--output', default='web/apps/labelstudio-e2e/.field-batches-e2e.json')
        parser.add_argument(
            '--operation',
            default='seed',
            choices=['seed', 'delete_source', 'restore_source', 'new_revision', 'revoke_reviewer'],
        )
        parser.add_argument('--locale', default='en-US', choices=['en-US', 'zh-CN'])

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
        manager = User.objects.create_user('manager@' + DOMAIN, password, username='field-manager')
        org = Organization.create_organization(created_by=manager, title='Field batches synthetic scope')
        manager.active_organization = org
        manager.save(update_fields=['active_organization'])
        users = {'manager': manager}
        for role in ('reviewer', 'a', 'b', 'c'):
            user = User.objects.create_user(
                role + '@' + DOMAIN, password, username='field-' + role, active_organization=org
            )
            org.add_user(user)
            users[role] = user
        projects, modes = {}, {}
        for locale in ('en-US', 'zh-CN'):
            project = Project.objects.create(
                title='Fields ' + locale,
                created_by=manager,
                organization=org,
                label_config=XML,
                is_published=True,
                enable_empty_annotation=True,
            )
            for role, user in users.items():
                ProjectMember.objects.create(
                    project=project, user=user, role=role if role in ('manager', 'reviewer') else 'annotator'
                )
            cases = {}
            for case in (
                'differences',
                'agreement',
                'format_only',
                'empty',
                'missing',
                'budget',
                'self',
                'source_loss',
                'new_revision',
                'unsupported',
            ):
                if case == 'unsupported':
                    project.label_config = XML.replace('name="name"', 'name="name" perRegion="true"')
                    project.save()
                task = Task.objects.create(project=project, data={'text': 'Synthetic 合成 😀 document\r\n第二行'})
                cases[case] = {'task': task.pk, 'sources': {}}
                for role in ('a', 'b', 'reviewer') if case == 'self' else ('a', 'b', 'c'):
                    values = (' 张三 ', '上海\r\n浦东', '001') if role != 'b' else ('张三', '上海\n浦东', '1')
                    if case == 'agreement':
                        values = ('张三', '上海\n浦东', '001')
                    if case == 'format_only' and role == 'b':
                        values = ('张三', '上海\n浦东', '001')
                    if case == 'empty':
                        values = ('', ' \t\r\n', '')
                    if case == 'missing' and role == 'a':
                        values = ('missing field',)
                    if case == 'budget' and role == 'a':
                        values = ('x' * 4097, 'b', 'c')
                    row = source(project, task, users[role], values)
                    cases[case]['sources'][role] = row.pk
                    if case == 'source_loss' and role == 'a':
                        cases[case]['restore'] = {
                            'id': row.pk,
                            'assignment_id': row.assignment_id,
                            'annotation_id': row.annotation_id,
                            'submitted_by_id': row.submitted_by_id,
                            'revision': row.revision,
                            'submitted_at': row.submitted_at.isoformat(),
                            'status': row.status,
                            'result_hash': row.result_hash,
                            'result_snapshot': row.result_snapshot,
                        }
                project.label_config = XML
                project.save()
            projects[locale] = {'id': project.pk, 'cases': cases}
            modes[f'{org.pk}:{project.pk}'] = 'fork_review'
        value = {
            'schema': 'field-batches-e2e/v1',
            'password': password,
            'organization': org.pk,
            'users': {k: {'id': v.pk, 'email': v.email} for k, v in users.items()},
            'projects': projects,
            'modes': modes,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        self.stdout.write('Synthetic field fixture written')

    def transition(self, output, locale, operation):
        value = json.loads(output.read_text(encoding='utf-8'))
        if value.get('schema') != 'field-batches-e2e/v1':
            raise CommandError('Owned synthetic field fixture required')
        scope = value['projects'][locale]
        project = Project.objects.get(
            pk=scope['id'],
            organization_id=value['organization'],
            organization__title='Field batches synthetic scope',
            created_by__email='manager@' + DOMAIN,
        )
        if operation == 'revoke_reviewer':
            ProjectMember.objects.filter(project=project, user_id=value['users']['reviewer']['id']).update(
                enabled=False
            )
        else:
            case = scope['cases']['new_revision' if operation == 'new_revision' else 'source_loss']
            assignment = TaskAssignment.objects.get(
                project=project, task_id=case['task'], assignee__email='a@' + DOMAIN
            )
            if operation == 'restore_source':
                record = case['restore']
                if (
                    record['id'] != case['sources']['a']
                    or record['assignment_id'] != assignment.pk
                    or record['submitted_by_id'] != assignment.assignee_id
                    or _snapshot_hash(record['result_snapshot']) != record['result_hash']
                ):
                    raise CommandError('Invalid synthetic restoration')
                Submission.objects.create(**record)
            else:
                row = Submission.objects.get(pk=case['sources']['a'], assignment=assignment)
                if operation == 'delete_source':
                    row.delete()
                else:
                    source(
                        project,
                        assignment.task,
                        assignment.assignee,
                        ('new', 'b', 'c'),
                        assignment=assignment,
                        revision=row.revision + 1,
                    )
        self.stdout.write(json.dumps({'ok': True, 'operation': operation, 'project': project.pk}))
