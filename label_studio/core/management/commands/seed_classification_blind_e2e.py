"""Create isolated synthetic blind-mix browser fixtures; never a rollout switch."""
import json
import os
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import F
from django.test.utils import override_settings


def _lock_fixture_projects(project_ids):
    """Lock every fixture project before any capture-epoch operation."""
    from projects.models import Project

    if not connection.in_atomic_block:
        raise RuntimeError('Synthetic project locks require an atomic transaction.')
    ids = sorted(set(project_ids))
    projects = list(Project.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
    if [project.pk for project in projects] != ids:
        raise CommandError('Synthetic fixture projects are unavailable.')
    return projects


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--source', required=True)
        parser.add_argument('--output', required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        from access_control.project_operations import local_principal
        from classification_disagreements.blind_mix import create_mixed_assignment
        from classification_disagreements.gold_api import GoldAPI
        from classification_disagreements.models import BlindCaptureEpoch, BlindMixDecision
        from projects.models import Project, ProjectMember
        from rest_framework.test import APIRequestFactory, force_authenticate
        from tasks.models import Submission, Task
        from users.models import User

        output = Path(options['output'])
        if (not settings.TEST_ENVIRONMENT or os.environ.get('LS82_SYNTHETIC_BLIND_FIXTURE') != '1'
                or connection.settings_dict['NAME'] != 'classification_gold' or output.exists()):
            raise CommandError('A fresh, explicitly marked synthetic classification database is required.')
        source = json.loads(Path(options['source']).read_text(encoding='utf-8'))
        if source.get('synthetic') != 'classification-disagreements-e2e/v1':
            raise CommandError('The source fixture is not the isolated classification fixture.')
        manager = User.objects.get(pk=source['users']['manager']['id'],
                                   email='manager@classification81.example.test')
        if User.objects.filter(email__startswith='blind-e2e-', email__endswith='@classification81.example.test').exists():
            raise CommandError('The synthetic blind fixture already exists.')
        # This command spans projects in one transaction. Taking an epoch lock
        # after the first project and only then acquiring the next project can
        # deadlock a participant read, so lock the complete set up front.
        _lock_fixture_projects(scope['id'] for scope in source['projects'].values())
        enabled = {key: True for key in source['modes']}
        config = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True, 'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': enabled,
                  'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True, 'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': enabled}
        references = {}
        factory = APIRequestFactory()
        with override_settings(**config):
            for locale, scope in source['projects'].items():
                project = Project.objects.get(pk=scope['id'], created_by=manager,
                                              organization__title='Synthetic classification disagreement')
                submission = Submission.objects.get(pk=scope['cases']['split']['sources'][0],
                                                    assignment__project=project)
                request = factory.post('/', {'kind': 'reference', 'submission_id': submission.pk,
                                             'expected_result_hash': submission.result_hash}, format='json',
                                       HTTP_IDEMPOTENCY_KEY=str(uuid4()))
                force_authenticate(request, manager)
                result = GoldAPI.as_view()(request, pk=project.pk)
                if result.status_code != 201:
                    raise CommandError('Could not create the synthetic frozen reference.')
                references[locale] = {'id': result.data['id'], 'hash': result.data['manifest_hash']}

            BlindCaptureEpoch.objects.get_or_create(pk=1)
            BlindCaptureEpoch.objects.filter(pk=1).update(generation=F('generation') + 1, enabled=True)
            projects = {}
            for locale, scope in source['projects'].items():
                project = Project.objects.get(pk=scope['id'], created_by=manager)
                email = f'blind-e2e-{locale.lower()}@classification81.example.test'
                member = User.objects.create_user(email, source['password'], username=email,
                                                  active_organization=project.organization)
                project.organization.add_user(member)
                ProjectMember.objects.create(project=project, user=member, role='annotator')
                carrier = Task.objects.create(project=project, data={'text': f'Synthetic unseen carrier for {locale} xxxxxxxxx'})
                ordinary = Task.objects.create(project=project, data={'text': f'Synthetic ordinary item for {locale} yyyyyyyyyyy'})
                quality_key = uuid4()
                quality = create_mixed_assignment(task=carrier, assignee=member, references=[references[locale]],
                    ratio_basis_points=10000, principal=local_principal(manager), key=quality_key)
                decision = BlindMixDecision.objects.get(project=project, key=quality_key)
                if not decision.selected or quality.task_id == carrier.pk:
                    raise CommandError('The synthetic quality assignment was not selected.')
                normal = create_mixed_assignment(task=ordinary, assignee=member, references=[references[locale]],
                    ratio_basis_points=0, principal=local_principal(manager), key=uuid4())
                if normal.task_id != ordinary.pk:
                    raise CommandError('The synthetic ordinary assignment changed identity.')
                draft_email = f'blind-e2e-draft-{locale.lower()}@classification81.example.test'
                draft_member = User.objects.create_user(draft_email, source['password'], username=draft_email,
                                                        active_organization=project.organization)
                project.organization.add_user(draft_member)
                ProjectMember.objects.create(project=project, user=draft_member, role='annotator')
                draft_carrier = Task.objects.create(project=project, data={
                    'text': f'Synthetic unseen draft carrier for {locale} zzzzzzzzz'})
                draft_quality = create_mixed_assignment(task=draft_carrier, assignee=draft_member,
                    references=[references[locale]], ratio_basis_points=10000, principal=local_principal(manager),
                    key=uuid4())
                if draft_quality.task_id == draft_carrier.pk:
                    raise CommandError('The synthetic draft quality assignment was not selected.')
                projects[locale] = {'id': project.pk, 'email': email, 'quality_task': quality.task_id,
                                    'ordinary_task': ordinary.pk, 'reference': references[locale]['id'],
                                    'draft_email': draft_email, 'draft_quality_task': draft_quality.task_id}
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as stream:
            json.dump({'synthetic': 'classification-blind-e2e/v1', 'password': source['password'],
                       'projects': projects}, stream, indent=2)
        self.stdout.write('Created isolated synthetic blind-mix fixtures.')
