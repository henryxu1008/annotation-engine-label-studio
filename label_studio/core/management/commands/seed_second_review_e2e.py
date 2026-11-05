"""Fresh local synthetic supervision acceptance; never enables a retained instance."""
import json
import os
import secrets
from pathlib import Path
from uuid import uuid4

from access_control.project_operations import (
    ORGANIZE_SECOND_REVIEW,
    RECORD_SECOND_REVIEW_FOLLOWUP,
    SECOND_REVIEW,
    LegacyProjectOperationProvider,
    ProjectOperationPolicy,
)
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.test import override_settings
from organizations.models import Organization
from projects.models import Project, ProjectMember
from rest_framework.test import APIClient
from tasks.models import Annotation, Task, TaskAssignment
from tasks.submissions import create_submission, review_submission
from users.models import User

DOMAIN = 'second-review84.example.test'
XML = '<View><Text name="text" value="$text"/><Choices name="label" toName="text"><Choice value="A"/><Choice value="B"/></Choices></View>'


def require_owned_empty_database():
    """A namespace is not ownership. Verify the bootstrap's original cluster."""
    raw = os.environ.get('SECOND_REVIEW_E2E_OWNERSHIP_FILE', '')
    proof_file = Path(raw)
    try:
        if (
            not raw
            or proof_file.is_symlink()
            or not proof_file.is_file()
            or proof_file.stat().st_size > 4096
            or proof_file.resolve().parent != Path(settings.BASE_DATA_DIR).resolve().parent
        ):
            raise ValueError()
        proof = json.loads(proof_file.read_text(encoding='utf-8'))
        if (
            set(proof) != {'schema', 'database', 'port', 'cluster', 'system_identifier', 'fresh_public_tables'}
            or proof['schema'] != 'second-review-owned-native/v1'
            or type(proof['fresh_public_tables']) is not int
            or proof['fresh_public_tables'] != 0
            or type(proof['port']) is not int
        ):
            raise ValueError()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_database(), current_setting('data_directory'), current_setting('port'), system_identifier FROM pg_control_system()"
            )
            database, directory, port, identifier = cursor.fetchone()
        if (
            database != proof['database']
            or database != connection.settings_dict['NAME']
            or int(port) != proof['port']
            or Path(directory).resolve() != Path(proof['cluster']).resolve()
            or str(identifier) != proof['system_identifier']
        ):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise CommandError('A matching owned native bootstrap proof is required')
    if User.objects.exists() or Organization.objects.exists() or Project.objects.exists() or Task.objects.exists():
        raise CommandError('Fresh empty synthetic business scope required')


class SecondReviewE2EPolicy:
    def evaluate(self, project):
        base = LegacyProjectOperationProvider().evaluate(project)
        if (
            settings.TEST_ENVIRONMENT
            and project.created_by.email.endswith('@' + DOMAIN)
            and project.title.startswith('Synthetic supervision ')
        ):
            return ProjectOperationPolicy(
                'synthetic-supervision/v1', 'active', base.allowed_operations | {ORGANIZE_SECOND_REVIEW, SECOND_REVIEW, RECORD_SECOND_REVIEW_FOLLOWUP}
            )
        return base


class Command(BaseCommand):
    def on_project_ready(self, project):
        """Extension point for owned test tooling; default fixture behavior is unchanged."""

    def add_arguments(self, parser):
        parser.add_argument('--output', default='web/apps/labelstudio-e2e/.second-review-e2e.json')

    @transaction.atomic
    def handle(self, *args, **options):
        if (
            not settings.TEST_ENVIRONMENT
            or connection.vendor != 'postgresql'
            or not connection.settings_dict['NAME'].startswith('ui_ci_second_review_')
            or connection.settings_dict['HOST'] != '127.0.0.1'
        ):
            raise CommandError('Owned loopback PostgreSQL synthetic namespace required')
        require_owned_empty_database()
        output = Path(options['output'])
        if output.exists() or User.objects.filter(email__endswith='@' + DOMAIN).exists():
            raise CommandError('Fresh synthetic database and output required')
        password = secrets.token_urlsafe(24)
        manager = User.objects.create_user('manager@' + DOMAIN, password, username='supervision-manager')
        org = Organization.create_organization(created_by=manager, title='Synthetic independent supervision')
        manager.active_organization = org
        manager.save(update_fields=['active_organization'])
        users = {'manager': manager}
        for name in ('initial', 'supervisor', 'a', 'b'):
            user = User.objects.create_user(
                name + '@' + DOMAIN,
                password,
                username='supervision-' + name,
                first_name=name,
                last_name='独立复核' if name == 'supervisor' else 'Synthetic',
                active_organization=org,
            )
            org.add_user(user)
            users[name] = user
        projects, modes = {}, {}
        for locale in ('en-US', 'zh-CN'):
            project = Project.objects.create(
                title='Synthetic supervision ' + locale,
                organization=org,
                created_by=manager,
                label_config=XML,
                is_published=True,
                enable_empty_annotation=True,
            )
            modes[f'{org.pk}:{project.pk}'] = 'fork_review'
            for name, user in users.items():
                ProjectMember.objects.create(
                    project=project,
                    user=user,
                    role='manager'
                    if name == 'manager'
                    else 'reviewer'
                    if name in ('initial', 'supervisor')
                    else 'annotator',
                )
            self.on_project_ready(project)
            sources = []
            for i in range(4):
                owner = users['a' if i % 2 == 0 else 'b']
                task = Task.objects.create(
                    project=project, data={'text': 'Frozen 静态文本 <b>literal</b>\nExact revision ' + str(i)}
                )
                assignment = TaskAssignment.objects.create(
                    project=project, task=task, assignee=owner, assigned_by=manager
                )
                annotation = Annotation.objects.create(
                    project=project,
                    task=task,
                    completed_by=owner,
                    lead_time=1.0,
                    result=[
                        dict(
                            id='supervision-' + str(task.pk),
                            from_name='label',
                            to_name='text',
                            type='choices',
                            value={'choices': ['A']},
                        )
                    ],
                )
                assignment.annotation = annotation
                assignment.save(update_fields=['annotation'])
                row = create_submission(assignment=assignment, annotation=annotation, actor=owner)
                sources.append(
                    dict(
                        id=row.pk,
                        assignment_id=assignment.pk,
                        task_id=task.pk,
                        annotation_id=annotation.pk,
                        owner='a' if i % 2 == 0 else 'b',
                    )
                )
            project.summary.update_data_columns(list(Task.objects.filter(project=project)))
            projects[locale] = dict(id=project.pk, sources=sources)
        configured = dict(
            ANNOTATION_SECOND_REVIEW_ENABLED=True,
            ANNOTATION_SECOND_REVIEW_FOLLOWUP_ENABLED=True,
            ANNOTATION_SECOND_REVIEW_INSTANCE_ID=str(uuid4()),
            ANNOTATION_SECOND_REVIEW_MODES=modes,
            ANNOTATION_REVIEW_BATCHES_ENABLED=True,
            ANNOTATION_REVIEW_BATCH_MODES=modes,
            ANNOTATION_PROJECT_OPERATION_PROVIDER='core.management.commands.seed_second_review_e2e.SecondReviewE2EPolicy',
        )
        with override_settings(**configured):
            for locale, project in projects.items():
                client = APIClient()
                client.force_authenticate(manager)
                base = f'/api/projects/{project["id"]}/review-batches/'
                args = dict(name='Supervision sample ' + locale, rate=100, seed='synthetic')
                preview = client.post(base + 'preview/', args, format='json')
                if preview.status_code != 200:
                    raise CommandError('Synthetic batch preview failed')
                saved = client.post(
                    base,
                    {**args, 'expected_plan_hash': preview.data['plan_hash']},
                    format='json',
                    HTTP_IDEMPOTENCY_KEY=str(uuid4()),
                )
                if saved.status_code != 201:
                    raise CommandError('Synthetic batch creation failed')
                project['batch_id'] = saved.data['id']
                from tasks.models import Submission

                for i, entry in enumerate(project['sources']):
                    row = Submission.objects.get(pk=entry['id'])
                    review_submission(
                        submission=row,
                        reviewer=users['initial'],
                        decision='rejected' if i == 1 else 'approved',
                        reason='Initial 初审 ' + str(i),
                    )
                historical = project['sources'][3]
                context = client.get(base + f'{project["batch_id"]}/sources/{historical["id"]}/second-review-context/')
                body = dict(
                    batch_id=project['batch_id'],
                    submission_id=historical['id'],
                    expected_plan_hash=context.data['plan_hash'],
                    expected_source_hash=context.data['source_hash'],
                    assignee_id=users['supervisor'].pk,
                    expected_viewer_hash=context.data['viewer_hash'],
                )
                result = client.post(
                    f'/api/projects/{project["id"]}/second-reviews/',
                    body,
                    format='json',
                    HTTP_IDEMPOTENCY_KEY=str(uuid4()),
                )
                if result.status_code != 201:
                    raise CommandError('Synthetic historical dispatch failed')
                historical['second_task_id'] = result.data['receipt']['task_id']
                assignment = TaskAssignment.objects.get(pk=historical['assignment_id'])
                create_submission(assignment=assignment, annotation=assignment.annotation, actor=users['b'])
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as out:
            json.dump(
                dict(
                    schema='second-review-e2e/v1',
                    password=password,
                    users={k: dict(id=v.pk, email=v.email) for k, v in users.items()},
                    projects=projects,
                    settings=configured,
                ),
                out,
                ensure_ascii=False,
                indent=2,
            )
        self.stdout.write('Fresh synthetic supervision fixture written; no model or retained environment used.')
