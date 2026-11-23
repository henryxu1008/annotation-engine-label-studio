"""Measure paired blind reads against the explicitly isolated synthetic fixture."""

import cProfile
import json
import os
import pstats
import statistics
import subprocess
import sys
import time
from argparse import SUPPRESS
from bisect import bisect_left, bisect_right
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
        parser.add_argument('--samples', type=int, default=14)
        parser.add_argument('--cold-processes', type=int, default=0)
        parser.add_argument('--profile-samples', type=int, default=0)
        parser.add_argument('--_cold-kind', choices=('ordinary', 'quality'), help=SUPPRESS)

    def handle(self, *args, **options):
        from classification_disagreements.models import BlindQualityAssignment, BlindTaskHandle
        from users.models import User

        # The fixture command uses the same guard. This tool enables feature
        # settings and may issue handles, so it is never a production probe.
        if (not settings.TEST_ENVIRONMENT or os.environ.get('LS82_SYNTHETIC_BLIND_FIXTURE') != '1'
                or connection.settings_dict['NAME'] != 'classification_gold'):
            raise CommandError('An explicitly marked isolated synthetic database is required.')
        if not 1 <= options['samples'] <= 1000:
            raise CommandError('Sample count must be between 1 and 1000.')
        if not 0 <= options['cold_processes'] <= 10 or (options['_cold_kind'] and options['cold_processes']):
            raise CommandError('Cold process count must be between 0 and 10.')
        if not 0 <= options['profile_samples'] <= 100 or (options['_cold_kind'] and options['profile_samples']):
            raise CommandError('Profile count must be between 0 and 100.')
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
                'ANNOTATION_CLASSIFICATION_GOLD_ENABLED': True, 'ANNOTATION_CLASSIFICATION_GOLD_PROJECTS': enabled,
                'LATEST_VERSION_CHECK': False}
        with override_settings(**mode):
            if options['_cold_kind']:
                kind = options['_cold_kind']
                task_id = scope['quality_task'] if kind == 'quality' else scope['ordinary_task']
                candidates = list(BlindTaskHandle.objects.filter(actor_id_frozen=actor.pk,
                    project_id_frozen=scope['id'], task_id_frozen=task_id)[:2])
                if (len(candidates) != 1 or BlindQualityAssignment.objects.filter(
                        assignment_id=candidates[0].assignment_id_frozen).exists() != (kind == 'quality')):
                    raise CommandError('Synthetic paired work is unavailable.')
                start = time.perf_counter()
                with CaptureQueriesContext(connection) as queries:
                    response = client.get(f'/api/tasks/blind/{candidates[0].task_handle}/')
                elapsed = (time.perf_counter() - start) * 1000
                if response.status_code != 200:
                    raise CommandError('Synthetic paired read failed.')
                self.stdout.write(json.dumps({'cold_sample': kind, 'ms': elapsed, 'queries': len(queries)}))
                return
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
            for iteration in range(options['samples'] + 2):
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
            # A descriptive rank statistic only: values near 1 indicate that
            # quality samples are usually slower. It is not a privacy gate.
            ordinary = sorted(value[0] for value in measurements['ordinary'][2:])
            quality = [value[0] for value in measurements['quality'][2:]]
            rank = sum((bisect_left(ordinary, value) + bisect_right(ordinary, value)) / 2
                       for value in quality) / (len(ordinary) * len(quality))
            self.stdout.write(json.dumps({'comparison': 'quality_vs_ordinary', 'rank_probability': round(rank, 4),
                                          'samples_per_kind': len(quality), 'diagnostic_only': True}))
            if options['profile_samples']:
                profiles = {kind: cProfile.Profile() for kind in handles}
                for iteration in range(options['profile_samples']):
                    for kind in (('quality', 'ordinary') if iteration % 2 else ('ordinary', 'quality')):
                        profile = profiles[kind]
                        profile.enable()
                        try:
                            response = client.get(f'/api/tasks/blind/{handles[kind]}/')
                        finally:
                            profile.disable()
                        if response.status_code != 200:
                            raise CommandError('Synthetic profiled read failed.')
                for kind, profile in profiles.items():
                    stats = pstats.Stats(profile)
                    top = sorted(stats.stats.items(), key=lambda item: item[1][2], reverse=True)[:15]
                    self.stdout.write(json.dumps({'kind': kind, 'mode': 'cpu-profile',
                        'samples': options['profile_samples'], 'diagnostic_only': True,
                        'functions': [{'file': Path(key[0]).name, 'function': key[2],
                                       'calls': value[1], 'own_ms': round(value[2] * 1000, 1)}
                                      for key, value in top]}))
            cold = {'ordinary': [], 'quality': []}
            manage = Path(__file__).resolve().parents[3] / 'manage.py'
            for iteration in range(options['cold_processes']):
                for kind in (('quality', 'ordinary') if iteration % 2 else ('ordinary', 'quality')):
                    try:
                        result = subprocess.run([
                            sys.executable, str(manage), 'measure_classification_blind_e2e',
                            '--source', options['source'], '--fixture', options['fixture'], '--_cold-kind', kind,
                        ], capture_output=True, text=True, encoding='utf-8', timeout=60, check=True,
                            env={**os.environ, 'LATEST_VERSION_CHECK': 'false', 'PYTHONIOENCODING': 'utf-8'})
                        samples = []
                        for line in result.stdout.splitlines():
                            try:
                                value = json.loads(line)
                            except ValueError:
                                continue
                            if isinstance(value, dict) and value.get('cold_sample') == kind:
                                samples.append(value)
                        if (len(samples) != 1 or type(samples[0].get('ms')) not in (int, float)
                                or type(samples[0].get('queries')) is not int):
                            raise ValueError('Invalid child sample')
                    except (OSError, subprocess.SubprocessError, ValueError) as exc:
                        raise CommandError('Synthetic cold-process measurement failed.') from exc
                    cold[kind].append((samples[0]['ms'], samples[0]['queries']))
            if options['cold_processes']:
                for kind, samples in cold.items():
                    self.stdout.write(json.dumps({'kind': kind, 'mode': 'fresh-process', 'samples': len(samples),
                        'median_ms': round(statistics.median(sample[0] for sample in samples), 1),
                        'min_ms': round(min(sample[0] for sample in samples), 1),
                        'max_ms': round(max(sample[0] for sample in samples), 1),
                        'queries': sorted({sample[1] for sample in samples}), 'database_cache': 'shared',
                        'diagnostic_only': True}))
                ordinary = sorted(value[0] for value in cold['ordinary'])
                quality = [value[0] for value in cold['quality']]
                rank = sum((bisect_left(ordinary, value) + bisect_right(ordinary, value)) / 2
                           for value in quality) / (len(ordinary) * len(quality))
                self.stdout.write(json.dumps({'comparison': 'quality_vs_ordinary', 'mode': 'fresh-process',
                    'rank_probability': round(rank, 4), 'samples_per_kind': len(quality),
                    'database_cache': 'shared', 'diagnostic_only': True}))
