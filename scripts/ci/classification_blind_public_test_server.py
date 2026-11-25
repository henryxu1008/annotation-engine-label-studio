"""Enable the unfinished public-mix gate only inside the isolated CI fixture.

This runner must never be used as a deployment entry point. It exists so the
actual HTTP command and participant UI can be exercised before release.
"""

import os
import sys
from pathlib import Path

import django
from django.conf import settings
from django.core.management import execute_from_command_line
from django.db import connection


def main():
    if os.environ.get('LS82_SYNTHETIC_BLIND_FIXTURE') != '1':
        raise SystemExit('An isolated synthetic PostgreSQL server is required.')
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'label_studio'))
    django.setup()
    if (not settings.TEST_ENVIRONMENT or not settings.DEBUG
            or connection.vendor != 'postgresql' or connection.settings_dict['NAME'] != 'classification_gold'
            or connection.settings_dict['HOST'] not in ('127.0.0.1', 'localhost')
            or sys.argv[1:] != ['runserver', '127.0.0.1:8081', '--noreload']):
        raise SystemExit('An isolated synthetic PostgreSQL server is required.')
    from organizations.models import Organization
    from users.models import User

    if (Organization.objects.count() != 1
            or not Organization.objects.filter(title='Synthetic classification disagreement').exists()
            or not User.objects.filter(email='manager@classification81.example.test').exists()
            or User.objects.exclude(email__endswith='@classification81.example.test').exists()):
        raise SystemExit('An isolated synthetic PostgreSQL server is required.')
    from classification_disagreements import blind_dispatch_api

    # This is a process-local test patch, not a configurable release switch.
    blind_dispatch_api._PUBLIC_MIX_READY = True
    settings.ANNOTATION_CLASSIFICATION_BLIND_DISPATCH_ENABLED = True
    execute_from_command_line([sys.argv[0], *sys.argv[1:]])


if __name__ == '__main__':
    main()
