"""Measure paired blind reads against the explicitly isolated synthetic fixture."""

import json
import os
import statistics
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext, override_settings
from rest_framework.test import APIClient


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--source', required=True)
        parser.add_argument('--fixture', required=True)

    def handle(self, *args, **options):
        from classification_disagreements.models import BlindQualityAssignment, BlindTaskHandle
        from users.models import User

        # The fixture command uses the same guard. This tool enables feature
        # settings and may issue handles, so it is never a production probe.
        if (not settings.TEST_ENVIRONMENT or os.environ.get('LS82_SYNTHETIC_BLIND_FIXTURE') != '1'
                or connection.settings_dict['NAME'] != 'classification_gold'):
            raise CommandError('An explicitly marked isolated synthetic database is required.')
        try:
            source = json.loads(Path(options['source']).read_text(encoding='utf-8'))
            fixture = json.loads(Path(options['fixture']).read_text(encoding='utf-8'))
            if (source['synthetic'] != 'classification-disagreements-e2e/v1'
                    or fixture['synthetic'] != 'classification-blind-e2e/v1'):
                raise ValueError('Unexpected fixture marker')
            scope = fixture['projects']['en-US']
            actor = User.objects.get(email=scope['email'])
            enabled = {key: True for key in source['modes']}
        except (KeyError, TypeError, ValueError, OSError, User.DoesNotExist) as exc:
            raise CommandError('Synthetic fixture is unavailable.') from exc

        client = APIClient()
        client.force_authenticate(actor)
        mode = {'ANNOTATION_CLASSIFICATION_BLIND_ENABLED': True, 'ANNOTATION_CLASSIFICATION_BLIND_PROJECTS': enabled,
                'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True, 'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': enabled}
        with override_settings(**mode):
            listing = client.get(f"/api/projects/{scope['id']}/blind-work/")
            if listing.status_code != 200:
                raise CommandError('Synthetic work list is unavailable.')
            # Establish which handle belongs to which fixture from server-side
            # bindings, not from content or participant-visible identifiers.
            handles = {}
            for row in listing.data['results']:
                binding = BlindTaskHandle.objects.filter(task_handle=row['id'], actor_id_frozen=actor.pk).first()
                if binding is None or binding.project_id_frozen != scope['id']:
                    raise CommandError('Synthetic handle binding is unavailable.')
                if binding.task_id_frozen == scope['quality_task']:
                    kind = 'quality'
                elif binding.task_id_frozen == scope['ordinary_task']:
                    kind = 'ordinary'
                else:
                    continue
                marked = BlindQualityAssignment.objects.filter(assignment_id=binding.assignment_id_frozen).exists()
                if kind in handles or marked != (kind == 'quality'):
                    raise CommandError('Synthetic assignment purpose is inconsistent.')
                handles[kind] = row['id']
            if set(handles) != {'quality', 'ordinary'}:
                raise CommandError('Synthetic paired work is unavailable.')
            measurements = {kind: [] for kind in handles}
            for iteration in range(16):
                for kind in (('quality', 'ordinary') if iteration % 2 else ('ordinary', 'quality')):
                    start = time.perf_counter()
                    with CaptureQueriesContext(connection) as queries:
                        response = client.get(f'/api/tasks/blind/{handles[kind]}/')
                    elapsed = (time.perf_counter() - start) * 1000
                    if response.status_code != 200:
                        raise CommandError('Synthetic paired read failed.')
                    measurements[kind].append((elapsed, len(queries)))
            for kind, values in measurements.items():
                samples = values[2:]  # Paired warm-up observations.
                result = {'kind': kind, 'samples': len(samples),
                          'median_ms': round(statistics.median(v[0] for v in samples), 1),
                          'min_ms': round(min(v[0] for v in samples), 1),
                          'max_ms': round(max(v[0] for v in samples), 1),
                          'queries': sorted({v[1] for v in samples})}
                self.stdout.write(json.dumps(result))
