"""Owned synthetic overview fixture, separate from existing field batch journeys."""
import copy
import json

from django.core.management.base import CommandError
from projects.models import ProjectMember
from tasks.models import Submission, Task, TaskAssignment
from tasks.submissions import _snapshot_hash
from users.models import User

from .seed_field_batch_e2e import Command as FieldCommand
from .seed_field_batch_e2e import source


class Command(FieldCommand):
    domain = 'field-overview.synthetic.invalid'
    fixture_schema = 'field-overview-e2e/v1'
    fixture_output = 'web/apps/labelstudio-e2e/.field-overview-e2e.json'
    organization_title = 'Field overview synthetic scope'
    username_prefix = 'field-overview'
    operations = (*FieldCommand.operations, 'grow_source', 'restore_grown_source')

    def extra_scope(self, project, users):
        tasks, actors = [], []
        for i in range(26):
            task = Task.objects.create(project=project, data={'text': 'Synthetic overview 合成 document'})
            tasks.append(task.pk)
            for side in range(2):
                user = User.objects.create_user(
                    f'person-{project.pk}-{2*i+side}@{self.domain}',
                    None,
                    username=f'overview-{project.pk}-{2*i+side}',
                    active_organization=project.organization,
                )
                project.organization.add_user(user)
                ProjectMember.objects.create(project=project, user=user, role='annotator')
                source(project, task, user, ('X', 'X', 'X'))
                actors.append(user.pk)
        return {'overview_tasks': tasks, 'population_actor_ids': actors}

    def transition(self, output, locale, operation):
        if operation not in ('grow_source', 'restore_grown_source'):
            return super().transition(output, locale, operation)
        _, scope, project = self.owned_scope(output, locale)
        case = scope['cases']['source_loss']
        assignment = TaskAssignment.objects.get(
            project=project, task_id=case['task'], assignee__email='a@' + self.domain
        )
        row = Submission.objects.get(pk=case['sources']['a'], assignment=assignment)
        original = case['restore']
        if (
            original['id'] != row.pk
            or original['assignment_id'] != assignment.pk
            or original['submitted_by_id'] != assignment.assignee_id
            or _snapshot_hash(original['result_snapshot']) != original['result_hash']
        ):
            raise CommandError('Invalid synthetic restoration')
        value = copy.deepcopy(original['result_snapshot'])
        if operation == 'grow_source':
            value['_synthetic_overview_budget_padding'] = 'x' * (65 * 1024 * 1024)
        Submission.objects.filter(pk=row.pk, assignment=assignment).update(
            result_snapshot=value, result_hash=_snapshot_hash(value)
        )
        self.stdout.write(json.dumps({'ok': True, 'operation': operation, 'project': project.pk}))
