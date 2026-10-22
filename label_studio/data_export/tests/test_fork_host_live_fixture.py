"""Security boundaries for the opt-in synthetic authorization control command."""
import copy
import json
import os
import stat
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.management.commands import seed_fork_host_live as command
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from organizations.models import Organization, OrganizationMember
from projects.models import Project, ProjectMember
from users.models import User


def guarded_settings(policy=None):
    return SimpleNamespace(
        DATABASES={'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': 'hfraw_fixture_review'}},
        APPROVED_EXPORT_POLICY=policy or {},
    )


class TestFixtureControlScope(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'fixture.json'
        owner = User.objects.create_user(email='owner@hfraw.example.test', username='hfraw-owner')
        self.org = Organization.create_organization(created_by=owner, title='Synthetic Fork Host RAW')
        owner.active_organization = self.org
        owner.save(update_fields=['active_organization'])
        self.org.jwt.legacy_api_tokens_enabled = True
        self.org.jwt.save()
        self.project = Project.objects.create(title='Synthetic Fork Host text only', organization=self.org, created_by=owner)
        self.users = {}
        for role in ('manager', 'snapshot', 'fetch', 'annotator', 'reviewer'):
            user = User.objects.create_user(email=f'{role}@hfraw.example.test', username=f'hfraw-{role}', active_organization=self.org)
            OrganizationMember.objects.create(user=user, organization=self.org)
            ProjectMember.objects.create(project=self.project, user=user, role=role if role in ('annotator', 'reviewer') else 'manager')
            self.users[role] = user
        namespace = str(uuid.uuid4())
        self.fixture = {'synthetic': True, 'namespace': namespace, 'organization': str(self.org.pk), 'project': str(self.project.pk), 'users': {r: {'id': u.pk, 'token': 'synthetic-unused'} for r, u in self.users.items()}}
        self.policy = {'source_namespace': namespace, 'projects': {f'{self.org.pk}:{self.project.pk}': {'mode': 'fork_approved', 'version': 'fork-host-live/v1'}}}
        foreign_owner = User.objects.create_user(email='foreign@example.test', username='foreign')
        self.foreign_org = Organization.create_organization(created_by=foreign_owner, title='Unrelated organization')
        self.foreign_org.jwt.legacy_api_tokens_enabled = True
        self.foreign_org.jwt.save()
        self.foreign_project = Project.objects.create(organization=self.foreign_org, created_by=foreign_owner, title='Unrelated project')
        ProjectMember.objects.create(project=self.foreign_project, user=self.users['fetch'], role='manager')

    def invoke(self, fixture=None, action='revoke', policy=None):
        self.path.write_text(json.dumps(self.fixture if fixture is None else fixture))
        with patch.object(command, 'settings', guarded_settings(self.policy if policy is None else policy)), patch.dict(os.environ, FORK_HOST_LIVE_SYNTHETIC='1'):
            call_command('seed_fork_host_live', output=str(self.path), action=action, role='fetch')

    def assert_roles_intact(self):
        for project in (self.project, self.foreign_project):
            self.assertEqual(ProjectMember.objects.get(project=project, user=self.users['fetch']).role, 'manager')

    def test_valid_revoke_restore_and_organization_token_toggle(self):
        self.invoke()
        self.assertEqual(ProjectMember.objects.get(project=self.project, user=self.users['fetch']).role, 'annotator')
        self.invoke(action='restore')
        self.assert_roles_intact()
        self.invoke(action='token-off')
        self.org.jwt.refresh_from_db()
        self.assertFalse(self.org.jwt.legacy_api_tokens_enabled)
        self.invoke(action='token-on')
        self.org.jwt.refresh_from_db()
        self.assertTrue(self.org.jwt.legacy_api_tokens_enabled)

    def test_foreign_project_cannot_redirect_membership_update(self):
        fixture = copy.deepcopy(self.fixture)
        fixture['project'] = str(self.foreign_project.pk)
        with self.assertRaises(CommandError):
            self.invoke(fixture)
        self.assert_roles_intact()

    def test_foreign_organization_cannot_toggle_tokens(self):
        fixture = copy.deepcopy(self.fixture)
        fixture['organization'] = str(self.foreign_org.pk)
        with self.assertRaises(CommandError):
            self.invoke(fixture, action='token-off')
        self.foreign_org.jwt.refresh_from_db()
        self.assertTrue(self.foreign_org.jwt.legacy_api_tokens_enabled)

    def test_marker_and_namespace_are_bound_to_operator_policy(self):
        for field, value in [('synthetic', False), ('namespace', str(uuid.uuid4())), ('namespace', 'invalid')]:
            with self.subTest(field=field, value=value):
                fixture = copy.deepcopy(self.fixture)
                fixture[field] = value
                with self.assertRaises(CommandError):
                    self.invoke(fixture)
        self.assert_roles_intact()

    def test_foreign_user_and_malformed_file_fail_closed(self):
        fixture = copy.deepcopy(self.fixture)
        fixture['users']['fetch']['id'] = self.foreign_org.created_by_id
        with self.assertRaises(CommandError):
            self.invoke(fixture)
        with self.assertRaises(CommandError):
            self.invoke({'synthetic': True})
        self.assert_roles_intact()

    def test_missing_or_deleted_organization_membership_denies_change(self):
        from django.utils import timezone
        OrganizationMember.objects.filter(organization=self.org, user=self.users['fetch']).update(deleted_at=timezone.now())
        with self.assertRaises(CommandError):
            self.invoke()
        self.assert_roles_intact()

    def test_missing_project_membership_is_not_silent_success(self):
        ProjectMember.objects.filter(project=self.project, user=self.users['fetch']).delete()
        with self.assertRaises(CommandError):
            self.invoke()

    def test_zero_row_update_is_not_reported_as_success(self):
        with patch('django.db.models.query.QuerySet.update', return_value=0):
            with self.assertRaises(CommandError):
                self.invoke()
        self.assert_roles_intact()

    def test_disabled_user_denies_even_token_action(self):
        admin = User.objects.create_user(email='admin@example.test', username='fixture-admin', is_staff=True, is_superuser=True)
        self.users['fetch'].is_active = False
        self.users['fetch'].save(update_fields=['is_active'], session_actor=admin)
        with self.assertRaises(CommandError):
            self.invoke(action='token-off')
        self.org.jwt.refresh_from_db()
        self.assertTrue(self.org.jwt.legacy_api_tokens_enabled)


class TestFixtureCredentialCreation(TestCase):
    def test_file_is_private_before_any_credential_write(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'fixture.json'
            observed = []
            original_dump = json.dump

            def observe(value, handle):
                observed.append(stat.S_IMODE(os.fstat(handle.fileno()).st_mode))
                original_dump(value, handle)

            old_umask = os.umask(0o022)
            try:
                with patch.object(command.json, 'dump', side_effect=observe):
                    command.write_private_json(output, {'token': 'synthetic-only'})
                self.assertEqual(observed, [0o600])
            finally:
                os.umask(old_umask)

    def test_existing_file_and_symlink_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'existing'
            target.write_text('retained')
            link = Path(directory) / 'link'
            link.symlink_to(target)
            for path in (target, link):
                with self.assertRaises(FileExistsError):
                    command.write_private_json(path, {'token': 'synthetic-only'})
            self.assertEqual(target.read_text(), 'retained')

    def test_interrupted_write_leaves_only_a_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'fixture.json'

            def interrupt(value, handle):
                handle.write('synthetic-partial')
                raise OSError('simulated write failure')

            with patch.object(command.json, 'dump', side_effect=interrupt):
                with self.assertRaises(OSError):
                    command.write_private_json(output, {'token': 'synthetic-only'})
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_credentials_never_depend_on_a_later_chmod(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'fixture.json'
            old_umask = os.umask(0o022)
            try:
                with patch.object(command, 'settings', guarded_settings()), patch.dict(os.environ, FORK_HOST_LIVE_SYNTHETIC='1'), patch.object(Path, 'chmod', side_effect=OSError('simulated chmod failure')):
                    try:
                        call_command('seed_fork_host_live', output=str(output), action='seed', role='fetch')
                    except OSError:
                        pass
                self.assertTrue(output.exists())
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            finally:
                os.umask(old_umask)
