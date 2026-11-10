"""Create fresh synthetic-only frozen bbox evidence for hosted browser tests."""
import json
import secrets
from io import BytesIO
from pathlib import Path

from access_control.project_operations import local_principal
from classification_disagreements import bbox_prepare
from data_import.models import FileUpload
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.test import override_settings
from organizations.models import Organization
from PIL import Image
from projects.models import Project, ProjectMember
from tasks.models import Annotation, Task, TaskAssignment
from tasks.submissions import create_submission
from users.models import User

DOMAIN = 'bbox81.example.test'
XML = '<View><Image name="img" value="$image"/><RectangleLabels name="box" toName="img"><Label value="Car"/></RectangleLabels></View>'


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--output', default='web/apps/labelstudio-e2e/.bbox-e2e.json')
        parser.add_argument('--operation', choices=['seed', 'revoke_reviewer'], default='seed')
        parser.add_argument('--locale', choices=['en-US', 'zh-CN'], default='en-US')

    def handle(self, *args, **options):
        if not settings.TEST_ENVIRONMENT:
            raise CommandError('Synthetic TEST_ENVIRONMENT required')
        output = Path(options['output'])
        if options['operation'] == 'revoke_reviewer':
            if not output.is_file():
                raise CommandError('Owned bbox fixture required')
            fixture = json.loads(output.read_text(encoding='utf-8'))
            if fixture.get('schema') != 'bbox-e2e/v1':
                raise CommandError('Unexpected fixture schema')
            project = Project.objects.get(pk=fixture['projects'][options['locale']]['id'],
                organization_id=fixture['organization'], organization__title='BBox synthetic scope',
                created_by__email='manager@' + DOMAIN)
            changed = ProjectMember.objects.filter(project=project, user_id=fixture['users']['reviewer']['id'],
                user__email='reviewer@' + DOMAIN, role='reviewer', enabled=True).update(enabled=False)
            if changed != 1:
                raise CommandError('Synthetic reviewer membership unavailable')
            self.stdout.write(json.dumps({'ok': True, 'project': project.pk}))
            return
        if output.exists() or User.objects.filter(email__endswith='@' + DOMAIN).exists():
            raise CommandError('Fresh synthetic output and database required')
        password = secrets.token_urlsafe(24)
        manager = User.objects.create_user('manager@' + DOMAIN, password, username='bbox-manager')
        org = Organization.create_organization(created_by=manager, title='BBox synthetic scope')
        manager.active_organization = org
        manager.save(update_fields=['active_organization'])
        users = {'manager': manager}
        for role in ('reviewer', 'a', 'b'):
            user = User.objects.create_user(role + '@' + DOMAIN, password, username='bbox-' + role, active_organization=org)
            org.add_user(user)
            users[role] = user
        projects, media_modes, quality_modes = {}, {}, {}
        for locale in ('en-US', 'zh-CN'):
            project = Project.objects.create(title='BBox ' + locale, created_by=manager, organization=org,
                                             label_config=XML, is_published=True)
            for role, user in users.items():
                ProjectMember.objects.create(project=project, user=user,
                    role=role if role in ('manager', 'reviewer') else 'annotator')
            key = f'{org.pk}:{project.pk}'
            media_modes[key], quality_modes[key] = 'managed', 'fork_review'
            image = BytesIO()
            Image.new('RGB', (100, 100), 'white').save(image, format='PNG')
            upload = FileUpload.objects.create(project=project, user=manager,
                                               file=ContentFile(image.getvalue(), name='bbox.png'))
            task = Task.objects.create(project=project, data={'image': upload.url})
            claims = {key: {str(task.pk): bbox_prepare.exclusive_source_claim(task, upload.url)}}
            with override_settings(ANNOTATION_BBOX_MEDIA_ENABLED=True, ANNOTATION_BBOX_MEDIA_PROJECTS=media_modes,
                                   ANNOTATION_BBOX_EXCLUSIVE_SOURCE_ATTESTATIONS=claims):
                bbox_prepare.prepare_managed(task, local_principal(manager))
                task.refresh_from_db()
                sources, assignments = {}, {}
                for role, x in (('a', 0), ('b', 60)):
                    region = {'id': 'box', 'type': 'rectanglelabels', 'from_name': 'box', 'to_name': 'img',
                              'original_width': 100, 'original_height': 100, 'image_rotation': 0,
                              'value': {'x': x, 'y': 0, 'width': 20, 'height': 20, 'rotation': 0,
                                        'rectanglelabels': ['Car']}}
                    annotation = Annotation.objects.create(task=task, project=project, completed_by=users[role], result=[region])
                    assignment = TaskAssignment.objects.create(task=task, project=project, assignee=users[role], annotation=annotation)
                    assignments[role] = assignment.pk
                    sources[role] = create_submission(assignment=assignment, annotation=annotation, actor=users[role]).pk
            projects[locale] = {'id': project.pk, 'task': task.pk, 'sources': sources, 'assignments': assignments}
        value = {'schema': 'bbox-e2e/v1', 'password': password, 'organization': org.pk,
                 'users': {key: {'id': value.pk, 'email': value.email} for key, value in users.items()},
                 'projects': projects, 'media_modes': media_modes, 'quality_modes': quality_modes}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(value), encoding='utf-8')
        self.stdout.write('Synthetic bbox fixture written: ' + str(output))
