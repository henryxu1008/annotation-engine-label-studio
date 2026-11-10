"""Describe the exact source identity an operator must verify before capture."""
import json

from classification_disagreements import bbox, bbox_prepare
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from rest_framework.exceptions import APIException
from tasks.models import Task


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--task', type=int, required=True)

    def handle(self, *args, **options):
        task = Task.objects.select_related('project').filter(pk=options['task'], project__deleted_at__isnull=True).first()
        if task is None:
            raise CommandError('Task unavailable')
        try:
            field = bbox.controls(task.project.label_config)['field']
            if not isinstance(task.data, dict) or field in task.data and settings.DATA_UNDEFINED_NAME in task.data:
                raise ValueError('ambiguous source')
            reference = task.data.get(field, task.data.get(settings.DATA_UNDEFINED_NAME))
            if not isinstance(reference, str):
                raise ValueError('missing source')
            claim = bbox_prepare.exclusive_source_claim(task, reference)
        except (bbox.Incomparable, bbox.Error, APIException, KeyError, TypeError, ValueError) as exc:
            raise CommandError('Unsupported bbox source') from exc
        self.stdout.write(json.dumps({'project_key': f'{task.project.organization_id}:{task.project_id}',
                                      'task_id': task.pk, 'exclusive_source_claim': claim}))
