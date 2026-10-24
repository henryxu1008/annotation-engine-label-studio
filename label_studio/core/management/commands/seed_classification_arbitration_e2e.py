"""Fresh synthetic arbitration cohort; never seeds a retained deployment."""

import json
from pathlib import Path

from django.db import transaction
from projects.models import Project, ProjectMember
from users.models import User

from .seed_classification_e2e import Command as ClassificationFixture


class Command(ClassificationFixture):
    domain = 'classification81-arbitration.example.test'
    prefix = 'class81-arbitration'
    fixture_schema = 'classification-arbitrations-e2e/v1'
    default_output = 'web/apps/labelstudio-e2e/.classification-arbitrations81-e2e.json'
    extra_cases = ('self', 'unsubmitted')

    @transaction.atomic
    def handle(self, *args, **options):
        super().handle(*args, **options)
        output = Path(options['output'])
        data = json.loads(output.read_text(encoding='utf-8'))
        projects = list(Project.objects.filter(pk__in=[p['id'] for p in data['projects'].values()]))
        org = projects[0].organization
        user = User.objects.create_user(
            f'reviewer2@{self.domain}', data['password'], username=f'{self.prefix}-reviewer2', active_organization=org
        )
        org.add_user(user)
        for project in projects:
            ProjectMember.objects.create(project=project, user=user, role='reviewer')
        data['users']['reviewer2'] = {'id': user.pk, 'email': user.email}
        output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
