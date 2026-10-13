from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tasks.models import TaskAssignment
from tasks.submissions import create_submission
from users.models import User


ACTORS = {
    'annotator_a': 'e2e-annotator-a@example.com',
    'annotator_b': 'e2e-annotator-b@example.com',
}


class Command(BaseCommand):
    help = 'Create the next deterministic enterprise E2E submission revision.'

    def add_arguments(self, parser):
        parser.add_argument('task_id', type=int)
        parser.add_argument('actor', choices=ACTORS.keys())

    @transaction.atomic
    def handle(self, *args, **options):
        actor = User.objects.get(email=ACTORS[options['actor']])
        assignment = (
            TaskAssignment.objects.select_for_update()
            .filter(
                task_id=options['task_id'],
                assignee=actor,
                status__in=TaskAssignment.ACTIVE_STATUSES,
            )
            .order_by('-assigned_at', '-id')
            .first()
        )
        if assignment is None or assignment.annotation_id is None:
            raise CommandError('Active assignment with annotation was not found.')

        annotation = assignment.annotation
        next_revision = assignment.submissions.count() + 1
        annotation.result = [
            {
                'from_name': 'sentiment',
                'to_name': 'text',
                'type': 'choices',
                'value': {'choices': ['Negative' if next_revision % 2 == 0 else 'Positive']},
            }
        ]
        annotation.updated_by = actor
        annotation.save(update_fields=['result', 'updated_by', 'updated_at'])

        submission = create_submission(
            assignment=assignment,
            annotation=annotation,
            actor=actor,
        )
        self.stdout.write(str(submission.id))
