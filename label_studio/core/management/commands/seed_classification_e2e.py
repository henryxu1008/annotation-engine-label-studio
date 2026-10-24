"""Create one synthetic-only classification fixture without changing existing data."""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from organizations.models import Organization
from projects.models import Project, ProjectMember
from tasks.models import Annotation, Submission, Task, TaskAssignment
from tasks.submissions import _canonical_snapshot, _snapshot_hash
from users.models import User


class Command(BaseCommand):
    domain = 'classification81.example.test'
    prefix = 'class81'
    fixture_schema = 'classification-disagreements-e2e/v1'
    default_output = 'web/apps/labelstudio-e2e/.classification81-e2e.json'
    extra_cases = ()

    def add_arguments(self, parser):
        parser.add_argument('--output', default=self.default_output)

    @transaction.atomic
    def handle(self, *args, **options):
        output = Path(options['output'])
        if (
            not settings.TEST_ENVIRONMENT
            or output.exists()
            or User.objects.filter(email__endswith=f'@{self.domain}').exists()
        ):
            raise CommandError('Fresh synthetic database/output and TEST_ENVIRONMENT required')
        password = 'SyntheticClassification81!123'
        manager = User.objects.create_user(
            f'manager@{self.domain}', password, username=f'{self.prefix}-manager'
        )
        org = Organization.create_organization(created_by=manager, title='Synthetic classification disagreement')
        manager.active_organization = org
        manager.save(update_fields=['active_organization'])
        users = {'manager': manager}
        for role in ('reviewer', 'a', 'b', 'c'):
            user = User.objects.create_user(
                f'{role}@{self.domain}', password, username=f'{self.prefix}-{role}', active_organization=org
            )
            org.add_user(user)
            users[role] = user
        config = (
            '<View><Text name="text" value="$text"/><Choices name="sentiment" toName="text">'
            '<Choice value="Positive"/><Choice value="Negative"/></Choices></View>'
        )
        projects, modes = {}, {}
        for locale in ('en-US', 'zh-CN'):
            project = Project.objects.create(
                title=f'Classification {locale}',
                organization=org,
                created_by=manager,
                label_config=config,
                is_published=True,
                enable_empty_annotation=True,
            )
            for role, user in users.items():
                ProjectMember.objects.create(
                    project=project, user=user, role=role if role in ('manager', 'reviewer') else 'annotator'
                )
            cases, tasks = {}, []
            for case in ('agreement', 'split', 'missing', 'config', 'remote', 'resolver', 'dynamic', *self.extra_cases):
                text = (
                    'https://synthetic.invalid/mutable.txt'
                    if case in ('remote', 'resolver')
                    else f'Synthetic 分类 {case}'
                )
                task = Task.objects.create(project=project, data={'text': text})
                tasks.append(task)
                sources = []
                # The self case represents a historical annotator now promoted
                # to Reviewer. It must not expose that person's own comparison.
                third = users['reviewer'] if case == 'self' else users['c']
                for index, actor in enumerate((users['a'], users['b'], third)):
                    assignment = TaskAssignment.objects.create(
                        project=project, task=task, assignee=actor, assigned_by=manager
                    )
                    if case == 'unsubmitted' and index == 2:
                        continue
                    choices = ['Negative' if case == 'split' and index == 1 else 'Positive']
                    result = (
                        []
                        if case == 'missing' and index == 1
                        else [
                            {
                                'id': f'class81-{index}',
                                'origin': 'manual',
                                'from_name': 'sentiment',
                                'to_name': 'text',
                                'type': 'choices',
                                'value': {'choices': choices},
                            }
                        ]
                    )
                    annotation = Annotation.objects.create(
                        project=project, task=task, completed_by=actor, result=result
                    )
                    assignment.annotation = annotation
                    assignment.save()
                    snapshot = _canonical_snapshot(annotation, assignment)
                    if case == 'config' and index == 1:
                        snapshot['project']['label_config'] = config.replace('Negative', 'Other')
                    if case == 'remote':
                        snapshot['project']['label_config'] = config.replace('<Text ', '<Text valueType="url" ')
                    if case == 'resolver':
                        snapshot['project']['label_config'] = config.replace(
                            '<Text ', '<Text valueType="text" resolver="csv" '
                        )
                    if case == 'dynamic':
                        snapshot['project']['label_config'] = config.replace('<Choices ', '<Choices value="$options" ')
                        snapshot['task']['data']['options'] = [{'value': 'Positive'}, {'value': 'Negative'}]
                    source = Submission.objects.create(
                        assignment=assignment,
                        annotation=annotation,
                        revision=1,
                        submitted_by=actor,
                        result_snapshot=snapshot,
                        result_hash=_snapshot_hash(snapshot),
                    )
                    sources.append(source.pk)
                cases[case] = {'task': task.pk, 'sources': sources}
            project.summary.update_data_columns(tasks)
            projects[locale] = {'id': project.pk, 'cases': cases}
            modes[f'{org.pk}:{project.pk}'] = 'fork_review'
        data = {
            'synthetic': self.fixture_schema,
            'password': password,
            'modes': modes,
            'users': {role: {'id': user.pk, 'email': user.email} for role, user in users.items()},
            'projects': projects,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2)
        self.stdout.write('Created isolated classification fixtures.')
