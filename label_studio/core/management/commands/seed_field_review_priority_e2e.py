"""Fresh synthetic fixture for the existing review workflow's optional field ranking."""

import json
import secrets
from pathlib import Path
from uuid import uuid4

from access_control.project_operations import local_principal
from classification_disagreements import field_batches, fields
from classification_disagreements.models import FieldBatch
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone
from organizations.models import Organization
from projects.models import Project, ProjectMember
from tasks.models import Task
from users.models import User

from .seed_field_e2e import XML, source

XML = XML.replace('maxSubmissions="1"', 'maxSubmissions="1" editable="true"')
TEXT = 'Synthetic 合成 document\r\n第二行'


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--operation', default='seed', choices=['seed', 'delete_source', 'restore_source'])
        parser.add_argument('--locale', default='en-US', choices=['en-US', 'zh-CN'])
        parser.add_argument('--output', default='web/apps/labelstudio-e2e/.field-review-priority-e2e.json')

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.TEST_ENVIRONMENT:
            raise CommandError('Synthetic TEST_ENVIRONMENT required')
        output = Path(options['output'])
        domain = 'field-review-priority84.example.test'
        if options['operation'] != 'seed':
            return self.transition(output, options, domain)
        if output.exists() or User.objects.filter(email__endswith='@' + domain).exists():
            raise CommandError('Fresh synthetic database and output required')
        password = secrets.token_urlsafe(24)
        manager = User.objects.create_user('manager@' + domain, password, username='field-review-priority-manager')
        org = Organization.create_organization(created_by=manager, title='field review priority synthetic scope')
        manager.active_organization = org
        manager.save(update_fields=['active_organization'])
        users = {'manager': manager}
        for name in ('reviewer', 'a', 'b', 'self'):
            user = User.objects.create_user(
                name + '@' + domain, password, username='field-review-priority-' + name, active_organization=org
            )
            org.add_user(user)
            users[name] = user
        projects, modes = {}, {}
        for locale in ('en-US', 'zh-CN'):
            project = Project.objects.create(
                title='field review priority ' + locale,
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
            for kind in ('conflict', 'agreement', 'empty', 'missing', 'stale'):
                task = Task.objects.create(project=project, data={'text': TEXT})
                members = ('a', 'b', 'self') if kind == 'conflict' else ('a', 'b')
                rows = []
                for name in members:
                    spans = (
                        ('', '', '')
                        if kind == 'empty'
                        else (
                            ('张三', '上海\n浦东', '001')
                            if kind == 'conflict' and name == 'b'
                            else (' 张三 ', '上海\r\n浦东', '001')
                        )
                    )
                    row = source(project, task, users[name], spans)
                    row.annotation.project = project
                    row.annotation.save(update_fields=['project'])
                    row.assignment.annotation = row.annotation
                    row.assignment.save(update_fields=['annotation'])
                    rows.append(
                        {
                            'id': row.pk,
                            'annotation_id': row.annotation_id,
                            'assignment_id': row.assignment_id,
                            'task_id': task.pk,
                            'owner': name,
                        }
                    )
                if kind == 'missing':
                    from tasks.models import TaskAssignment

                    TaskAssignment.objects.create(project=project, task=task, assignee=users['self'])
                cases[kind] = {'task': task.pk, 'sources': rows}
            plan, _ = field_batches.build_plan(project, [c['task'] for c in cases.values()])
            principal = local_principal(manager)
            now = timezone.now().isoformat()
            manifest = {
                **plan,
                'schema': field_batches.SCHEMA,
                'project_id': project.pk,
                'organization_id': org.pk,
                'creator_principal': principal.principal_id,
                'creator_source': principal.source,
                'actor_id_frozen': manager.pk,
                'name': 'Frozen field 分歧依据',
                'scope_enumerated_at': now,
                'calculated_at': now,
            }
            batch = FieldBatch.objects.create(
                project=project,
                created_by=manager,
                creator_principal=principal.principal_id,
                actor_id_frozen=manager.pk,
                name=manifest['name'],
                idempotency_key=uuid4(),
                request_hash=fields.digest({'synthetic': project.pk}),
                manifest=manifest,
                manifest_hash=fields.digest(manifest),
            )
            from tasks.models import Submission

            old = Submission.objects.select_related('assignment__task', 'submitted_by').get(
                pk=cases['stale']['sources'][0]['id']
            )
            newer = source(project, old.assignment.task, old.submitted_by, [], assignment=old.assignment, revision=2)
            ProjectMember.objects.filter(project=project, user=users['self']).update(role='reviewer')
            projects[locale] = {
                'id': project.pk,
                'cases': cases,
                'newer': newer.pk,
                'analysis': {'id': batch.pk, 'manifest_hash': batch.manifest_hash},
            }
            projects[locale]['saved_source'] = (
                Submission.objects.filter(pk=cases['empty']['sources'][0]['id']).values().get()
            )
            modes[f'{org.pk}:{project.pk}'] = 'fork_review'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    'schema': 'field-review-priority-e2e/v1',
                    'password': password,
                    'organization': org.pk,
                    'users': {k: {'id': u.pk, 'email': u.email} for k, u in users.items()},
                    'projects': projects,
                    'modes': modes,
                },
                ensure_ascii=False,
                cls=DjangoJSONEncoder,
            ),
            encoding='utf-8',
        )
        self.stdout.write('Synthetic field review priority fixture written')

    def transition(self, output, options, domain):
        from tasks.models import Submission

        data = json.loads(output.read_text(encoding='utf-8'))
        if data.get('schema') != 'field-review-priority-e2e/v1':
            raise CommandError('Field priority synthetic fixture required')
        scope = data['projects'][options['locale']]
        project = Project.objects.get(pk=scope['id'], organization_id=data['organization'])
        if not project.created_by.email.endswith('@' + domain):
            raise CommandError('Synthetic owner required')
        record = scope['saved_source']
        from tasks.models import TaskAssignment

        if not TaskAssignment.objects.filter(pk=record['assignment_id'], project=project).exists():
            raise CommandError('Synthetic source scope missing')
        if options['operation'] == 'delete_source':
            Submission.objects.filter(pk=record['id'], assignment__project=project).delete()
        else:
            if Submission.objects.filter(pk=record['id']).exists():
                raise CommandError('Never replace an existing source')
            Submission.objects.create(**record)
        self.stdout.write('Synthetic field priority source transition complete')
