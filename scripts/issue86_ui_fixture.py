"""Seed only a fresh disposable local #86 browser-test database."""
import json
import os

if (os.environ.get('ISSUE86_DISPOSABLE_UI_FIXTURE') != '1'
        or os.environ.get('TEST_ENVIRONMENT') != '1'
        or os.environ.get('POSTGRE_HOST') != '127.0.0.1'
        or os.environ.get('POSTGRE_NAME') != 'issue86_ui_test'):
    raise SystemExit('A dedicated loopback-only #86 browser fixture is required.')

import django

django.setup()

from access_control.project_operations import local_principal
from organizations.tests.factories import OrganizationFactory
from projects.lifecycle import transition_project
from projects.models import Project, ProjectMember
from projects.tests.factories import ProjectFactory
from tasks.tests.factories import TaskFactory
from users.tests.factories import UserFactory

if Project.objects.exists():
    raise SystemExit('The disposable browser fixture database is not empty.')

password = 'synthetic-issue86-browser-password'
organization = OrganizationFactory(title='Synthetic issue 86 organization')
manager = organization.created_by
manager.email = 'issue86-manager@example.test'
manager.username = 'issue86-manager'
manager.set_password(password)
manager.save(update_fields=['email', 'username', 'password'])
annotator = UserFactory(email='issue86-annotator@example.test', active_organization=organization)
annotator.set_password(password)
annotator.save(update_fields=['password'])
project = ProjectFactory(organization=organization, title='Synthetic issue 86 lifecycle')
ProjectMember.objects.create(project=project, user=manager, role=ProjectMember.Role.MANAGER)
ProjectMember.objects.create(project=project, user=annotator, role=ProjectMember.Role.ANNOTATOR)
TaskFactory(project=project, data={'text': 'Synthetic lifecycle task'})
transition_project(project.pk, local_principal(manager), expected_revision=0, target='active', reason='synthetic setup')
print('ISSUE86_FIXTURE=' + json.dumps({'project_id': project.pk, 'manager': manager.email,
                                        'annotator': annotator.email, 'password': password}))
