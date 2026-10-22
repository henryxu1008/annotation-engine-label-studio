"""Synthetic real-service trial. Never provision or mutate a retained instance."""
import json
import os
import secrets
import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, MultipleObjectsReturned
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from organizations.models import Organization, OrganizationMember
from projects.models import Project, ProjectMember
from rest_framework.authtoken.models import Token
from tasks.models import Annotation, Task, TaskAssignment
from tasks.submissions import create_submission, review_submission
from users.models import User


def write_private_json(path, value):
    # Exclusive creation prevents clobbering an existing file/symlink. Mode is
    # private before the first credential byte; a failed write stays private.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())


def fixture_scope(output):
    """Bind mutable controls to the operator policy and exact synthetic cohort."""
    try:
        fixture = json.loads(output.read_text(encoding='utf-8'))
        namespace = str(uuid.UUID(fixture['namespace']))
        if fixture['synthetic'] is not True or namespace != fixture['namespace']:
            raise ValueError()
        policy = settings.APPROVED_EXPORT_POLICY
        if policy['source_namespace'] != namespace or policy['projects'][f"{fixture['organization']}:{fixture['project']}"] != {'mode': 'fork_approved', 'version': 'fork-host-live/v1'}:
            raise ValueError()
        org = Organization.objects.select_for_update().get(pk=fixture['organization'], title='Synthetic Fork Host RAW')
        User.objects.select_for_update().get(pk=org.created_by_id, email='owner@hfraw.example.test', username='hfraw-owner', is_active=True, active_organization=org)
        project = Project.objects.select_for_update().get(pk=fixture['project'], organization=org, created_by_id=org.created_by_id, title='Synthetic Fork Host text only', deleted_at__isnull=True)
        roles = ('manager', 'snapshot', 'fetch', 'annotator', 'reviewer')
        if set(fixture['users']) != set(roles):
            raise ValueError()
        members = {}
        for role in roles:
            user = User.objects.select_for_update().get(pk=fixture['users'][role]['id'], email=f'{role}@hfraw.example.test', username=f'hfraw-{role}', is_active=True, active_organization=org)
            OrganizationMember.objects.select_for_update().get(user=user, organization=org, deleted_at__isnull=True)
            members[role] = ProjectMember.objects.select_for_update().get(project=project, user=user, enabled=True)
        return org, members
    except (OSError, KeyError, TypeError, ValueError, AttributeError, ObjectDoesNotExist, MultipleObjectsReturned):
        raise CommandError('Synthetic fixture identity, policy or membership mismatch') from None


class Command(BaseCommand):
    help = "Provision synthetic Fork→Host fixtures in a NEW hfraw_ PostgreSQL database only."

    def add_arguments(self, parser):
        parser.add_argument('--output', required=True)
        parser.add_argument('--action', default='seed', choices=['seed', 'revoke', 'restore', 'token-off', 'token-on'])
        parser.add_argument('--role', default='fetch', choices=['manager', 'snapshot', 'fetch'])

    @transaction.atomic
    def handle(self, *args, **options):
        db = settings.DATABASES['default']
        if os.getenv('FORK_HOST_LIVE_SYNTHETIC') != '1' or db['ENGINE'] != 'django.db.backends.postgresql' or not db['NAME'].startswith('hfraw_'):
            raise CommandError('Dedicated hfraw_ PostgreSQL database and explicit synthetic gate required')
        output = Path(options['output'])
        if options['action'] not in ('seed', 'revoke', 'restore', 'token-off', 'token-on') or options['role'] not in ('manager', 'snapshot', 'fetch'):
            raise CommandError('Unsupported synthetic control action or role')
        if options['action'] != 'seed':
            org, members = fixture_scope(output)
            if options['action'] in ('token-off', 'token-on'):
                org.jwt.legacy_api_tokens_enabled = options['action'] == 'token-on'
                org.jwt.save()
            else:
                changed = ProjectMember.objects.filter(pk=members[options['role']].pk).update(
                    role='annotator' if options['action'] == 'revoke' else 'manager')
                if changed != 1:
                    raise CommandError('Synthetic membership update did not affect exactly one row')
            self.stdout.write('Synthetic fixture policy updated')
            return
        if output.exists() or User.objects.filter(email__endswith='@hfraw.example.test').exists():
            raise CommandError('Refusing to reseed/adopt an existing fixture')
        users = {}
        owner = User.objects.create_user(email='owner@hfraw.example.test', username='hfraw-owner', password=secrets.token_urlsafe(32))
        org = Organization.create_organization(created_by=owner, title='Synthetic Fork Host RAW')
        owner.active_organization = org
        owner.save(update_fields=['active_organization'])
        # Explicit policy is scoped to this new synthetic organization only.
        org.jwt.legacy_api_tokens_enabled = True
        org.jwt.save()
        for name in ('manager', 'snapshot', 'fetch', 'annotator', 'reviewer'):
            u = User.objects.create_user(email=f'{name}@hfraw.example.test', username=f'hfraw-{name}', password=secrets.token_urlsafe(32), active_organization=org)
            OrganizationMember.objects.create(user=u, organization=org)
            users[name] = u
        xml = '<View><Labels name="entity" toName="text"><Label value="Entity"/></Labels><Text name="text" value="$text"/></View>'
        project = Project.objects.create(title='Synthetic Fork Host text only', organization=org, created_by=owner, label_config=xml, is_published=True)
        for name, u in users.items():
            ProjectMember.objects.create(project=project, user=u, role=name if name in ('annotator', 'reviewer') else 'manager')
        sources = []
        texts = ['Alpha synthetic sample', '中文合成样本 🧪', 'Precision synthetic sample', 'Budget ' + ('x' * (17 * 1024 * 1024))]
        for index, text in enumerate(texts):
            task = Task.objects.create(project=project, data={'text': text, 'large_integer': 9007199254740993}, meta={'synthetic': True})
            a = Annotation.objects.create(project=project, task=task, completed_by=users['annotator'], result=[{'id': f'e{index}', 'from_name': 'entity', 'to_name': 'text', 'type': 'labels', 'value': {'start': 0, 'end': 2, 'text': text[:2], 'labels': ['Entity']}}])
            assignment = TaskAssignment.objects.create(project=project, task=task, assignee=users['annotator'], assigned_by=users['manager'], annotation=a)
            source = create_submission(assignment=assignment, annotation=a, actor=users['annotator'])
            review_submission(submission=source, reviewer=users['reviewer'], decision='approved')
            sources.append({'submission_id': source.pk, 'revision': source.revision, 'result_hash': source.result_hash})
        namespace = str(uuid.uuid4())
        fixture = {'synthetic': True, 'namespace': namespace, 'organization': str(org.pk), 'project': str(project.pk), 'approved': sources[:3], 'budget': sources[3:], 'users': {name: {'id': u.pk, 'token': Token.objects.get_or_create(user=u)[0].key} for name, u in users.items()}}
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private_json(output, fixture)
        policy = {'source_namespace': namespace, 'projects': {f'{org.pk}:{project.pk}': {'mode': 'fork_approved', 'version': 'fork-host-live/v1'}}}
        output.with_name('fork-policy.json').write_text(json.dumps(policy), encoding='utf-8')
        self.stdout.write('New synthetic fixture ready; credentials kept in private output only')
