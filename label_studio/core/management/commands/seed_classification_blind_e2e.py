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
        from classification_disagreements.blind_mix import create_mixed_assignment, reassign_mixed_assignment
        from classification_disagreements.blind_task_handles import issue_handle
        from classification_disagreements.gold_api import GoldAPI
        from classification_disagreements.models import BlindCaptureEpoch, BlindMixDecision
        from projects.models import Project, ProjectMember
        from rest_framework.authtoken.models import Token
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
        if manager.active_organization is None or manager.active_organization.title != 'Synthetic classification disagreement':
            raise CommandError('The manager is not in the synthetic fixture organization.')
        # Only this guarded synthetic organization needs a second manager
        # session while a participant browser session remains active.
        jwt_settings = manager.active_organization.jwt
        jwt_settings.legacy_api_tokens_enabled = True
        jwt_settings.save(update_fields=['legacy_api_tokens_enabled'])
        manager_token, _ = Token.objects.get_or_create(user=manager)
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
            principal = local_principal(manager)

            def seed_reassignment(project, locale, kind):
                members = []
                for suffix in ('original', 'next'):
                    email = f'blind-e2e-reassign-{kind}-{suffix}-{locale.lower()}@classification81.example.test'
                    user = User.objects.create_user(email, source['password'], username=email,
                                                    active_organization=project.organization)
                    project.organization.add_user(user)
                    ProjectMember.objects.create(project=project, user=user, role='annotator')
                    members.append(user)
                original_owner, new_owner = members
                selected = kind == 'quality'
                text = f'Synthetic reassignment {kind} for {locale}'
                original_task = Task.objects.create(project=project, data={'text': text})
                original_key = uuid4()
                original = create_mixed_assignment(task=original_task, assignee=original_owner,
                    references=[references[locale]], ratio_basis_points=10000 if selected else 0,
                    principal=principal, key=original_key)
                previous = BlindMixDecision.objects.get(project=project, key=original_key)
                if previous.selected != selected:
                    raise CommandError('The synthetic reassignment has the wrong purpose.')
                old_handle = issue_handle(principal=local_principal(original_owner), assignment_id=original.pk)
                original.cancel()
                task = original_task
                if selected:
                    task = Task.objects.create(project=project, data={'text': f'{text} replacement carrier'})
                replacement_key = uuid4()
                replacement = reassign_mixed_assignment(previous_id=previous.pk, task=task,
                    assignee=new_owner, principal=principal, key=replacement_key)
                successor = BlindMixDecision.objects.get(project=project, key=replacement_key)
                if (replacement.pk == original.pk or successor.predecessor_id_frozen != previous.pk
                        or successor.selected != selected or successor.reference_id_frozen != previous.reference_id_frozen
                        or (not selected and replacement.task_id != original_task.pk)):
                    raise CommandError('The synthetic reassignment did not preserve the intended work.')
                return {'original_email': original_owner.email, 'replacement_email': new_owner.email,
                        'previous_handle': old_handle.task_handle, 'task_id': replacement.task_id,
                        'text': replacement.task.data['text']}

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
                revocations = {}
                for kind in ('ordinary', 'quality'):
                    revoke_email = f'blind-e2e-revoke-{kind}-{locale.lower()}@classification81.example.test'
                    revoke_member = User.objects.create_user(revoke_email, source['password'], username=revoke_email,
                                                             active_organization=project.organization)
                    project.organization.add_user(revoke_member)
                    membership = ProjectMember.objects.create(project=project, user=revoke_member, role='annotator')
                    text = f'Synthetic revoke {kind} carrier for {locale} qqqqqqqqqqq'
                    revoke_task = Task.objects.create(project=project, data={'text': text})
                    chosen = create_mixed_assignment(task=revoke_task, assignee=revoke_member,
                        references=[references[locale]], ratio_basis_points=10000 if kind == 'quality' else 0,
                        principal=local_principal(manager), key=uuid4())
                    if (chosen.task_id != revoke_task.pk) != (kind == 'quality'):
                        raise CommandError('The synthetic revocation work has the wrong purpose.')
                    revocations[kind] = {'email': revoke_email, 'membership': membership.pk,
                                         'text': 'Synthetic 分类 split' if kind == 'quality' else text}
                projects[locale] = {'id': project.pk, 'email': email, 'quality_task': quality.task_id,
                                    'ordinary_task': ordinary.pk, 'reference': references[locale]['id'],
                                    'draft_email': draft_email, 'draft_quality_task': draft_quality.task_id,
                                    'revocations': revocations,
                                    'reassignments': {kind: seed_reassignment(project, locale, kind)
                                                      for kind in ('ordinary', 'quality')}}
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as stream:
            json.dump({'synthetic': 'classification-blind-e2e/v1', 'password': source['password'],
                       'manager_token': manager_token.key,
                       'projects': projects}, stream, indent=2)
        self.stdout.write('Created isolated synthetic blind-mix fixtures.')
