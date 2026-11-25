from classification_disagreements.blind_score_outbox import reconcile
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Retry a bounded page of pending or stale blind-score jobs.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=50)

    def handle(self, *args, **options):
        try:
            count = reconcile(limit=options['limit'])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f'checked={count}')
