"""CI-only synthetic settings bootstrap. Never imported by the product entrypoint."""
import argparse
import os
import re
import sys
from copy import deepcopy
from pathlib import Path
from uuid import UUID

from ui_common import ROOT, digest, integer, read_json, require

DOMAIN = 'review-confidence84.example.test'
PROVIDER = 'core.management.commands.seed_review_confidence_e2e.ConfidenceE2EPolicy'
SETTING_KEYS = {
    'ANNOTATION_REVIEW_BATCHES_ENABLED',
    'ANNOTATION_REVIEW_CONFIDENCE_ENABLED',
    'ANNOTATION_REVIEW_CONFIDENCE_INSTANCE_ID',
    'ANNOTATION_REVIEW_CONFIDENCE_PROFILES',
    'ANNOTATION_REVIEW_CONFIDENCE_GRANTS',
    'ANNOTATION_REVIEW_BATCH_MODES',
    'ANNOTATION_PROJECT_OPERATION_PROVIDER',
}


def uuid_string(v):
    try:
        return isinstance(v, str) and str(UUID(v)) == v
    except (ValueError, TypeError, AttributeError):
        return False


def validate_fixture(value):
    require(
        isinstance(value, dict)
        and set(value) == {'schema', 'password', 'users', 'projects', 'settings', 'profile_id', 'profile_digest'},
        'Synthetic fixture shape invalid',
    )
    require(value['schema'] == 'review-confidence-e2e/v1', 'Synthetic fixture schema invalid')
    require(
        isinstance(value['password'], str) and re.fullmatch(r'[A-Za-z0-9_-]{20,128}', value['password']),
        'Synthetic password format invalid',
    )
    users = value['users']
    projects = value['projects']
    configured = value['settings']
    require(
        isinstance(users, dict) and set(users) == {'manager', 'producer', 'reviewer', 'a', 'b'},
        'Synthetic users invalid',
    )
    require(
        all(
            set(v) == {'id', 'email'} and integer(v['id'], 1) and v['email'] == k + '@' + DOMAIN
            for k, v in users.items()
        ),
        'Synthetic user scope invalid',
    )
    require(len({v['id'] for v in users.values()}) == 5, 'Duplicate synthetic users')
    require(isinstance(projects, dict) and set(projects) == {'en-US', 'zh-CN'}, 'Synthetic language scope invalid')
    require(
        all(
            set(v) == {'id', 'sources'}
            and integer(v['id'], 1)
            and isinstance(v['sources'], list)
            and len(v['sources']) == 5
            for v in projects.values()
        ),
        'Synthetic projects invalid',
    )
    project_ids = {v['id'] for v in projects.values()}
    require(len(project_ids) == 2, 'Duplicate synthetic projects')
    require(isinstance(configured, dict) and set(configured) == SETTING_KEYS, 'Settings allowlist violation')
    require(
        configured['ANNOTATION_REVIEW_BATCHES_ENABLED'] is True
        and configured['ANNOTATION_REVIEW_CONFIDENCE_ENABLED'] is True,
        'Synthetic flags must be explicit booleans',
    )
    require(configured['ANNOTATION_PROJECT_OPERATION_PROVIDER'] == PROVIDER, 'Arbitrary policy import forbidden')
    require(
        uuid_string(configured['ANNOTATION_REVIEW_CONFIDENCE_INSTANCE_ID']) and uuid_string(value['profile_id']),
        'Synthetic namespace invalid',
    )
    profiles = configured['ANNOTATION_REVIEW_CONFIDENCE_PROFILES']
    grants = configured['ANNOTATION_REVIEW_CONFIDENCE_GRANTS']
    modes = configured['ANNOTATION_REVIEW_BATCH_MODES']
    require(
        isinstance(profiles, list) and isinstance(grants, list) and len(profiles) == len(grants) == 2,
        'Synthetic profile/grant count invalid',
    )
    require(
        {p.get('project_id') for p in profiles} == {g.get('project_id') for g in grants} == project_ids,
        'Synthetic project binding invalid',
    )
    require(
        all(
            set(p) == {'organization_id', 'project_id', 'descriptor', 'digest', 'fresh'}
            and integer(p['organization_id'], 1)
            and integer(p['project_id'], 1)
            for p in profiles
        ),
        'Synthetic profile shape invalid',
    )
    orgs = {p['organization_id'] for p in profiles}
    require(len(orgs) == 1 and all(integer(p, 1) for p in orgs), 'Synthetic organization invalid')
    org = next(iter(orgs))
    namespace = None
    for p in profiles:
        d = p['descriptor']
        require(
            p['fresh'] is True
            and isinstance(d, dict)
            and d.get('profile_id') == value['profile_id']
            and uuid_string(d.get('producer_namespace'))
            and p['digest'] == value['profile_digest'] == digest(d),
            'Synthetic descriptor/digest mismatch',
        )
        namespace = namespace or d['producer_namespace']
        require(d['producer_namespace'] == namespace, 'Synthetic namespace mismatch')
    require(
        isinstance(modes, dict) and modes == {f'{org}:{p}': 'fork_review' for p in project_ids},
        'Synthetic mode scope invalid',
    )
    for g in grants:
        require(
            set(g)
            == {
                'organization_id',
                'project_id',
                'principal_id',
                'source',
                'actor_id',
                'producer_namespace',
                'profile_ids',
            },
            'Synthetic grant shape invalid',
        )
        require(
            all(integer(g[k], 1) for k in ('organization_id', 'project_id', 'actor_id')),
            'Synthetic grant ID type invalid',
        )
        require(
            g['organization_id'] == org
            and g['actor_id'] == users['producer']['id']
            and g['principal_id'] == f"label_studio_local:{users['producer']['id']}"
            and g['source'] == 'label_studio_local'
            and g['producer_namespace'] == namespace
            and g['profile_ids'] == [value['profile_id']],
            'Synthetic grant ownership mismatch',
        )
    for p in projects.values():
        require(len({s.get('id') for s in p['sources']}) == 5, 'Synthetic source IDs invalid')
        for s in p['sources']:
            require(
                s.get('owner') in users
                and all(integer(s.get(k), 1) for k in ('id', 'assignment_id', 'annotation_id', 'task_id')),
                'Synthetic source binding invalid',
            )
    # Runtime-only overrides must never mutate the validated frozen fixture.
    return deepcopy(configured)


def validate_database(value):
    from organizations.models import Organization
    from projects.models import Project, ProjectMember
    from review_batches import confidence_contract as c
    from tasks.models import Submission
    from users.models import User

    clean = validate_fixture(value)
    org_id = clean['ANNOTATION_REVIEW_CONFIDENCE_PROFILES'][0]['organization_id']
    require(
        Organization.objects.filter(pk=org_id, title='Synthetic confidence protocol scope').exists(),
        'Synthetic organization not found',
    )
    manager = value['users']['manager']['id']
    for name, u in value['users'].items():
        require(
            User.objects.filter(pk=u['id'], email=u['email'], is_active=True, active_organization_id=org_id).exists(),
            'Synthetic user row mismatch',
        )
    for locale, p in value['projects'].items():
        require(
            Project.objects.filter(
                pk=p['id'], organization_id=org_id, created_by_id=manager, title='Confidence protocol ' + locale
            ).exists(),
            'Synthetic project row mismatch',
        )
        for name, u in value['users'].items():
            role = (
                'manager' if name in ('manager', 'producer') else ('reviewer' if name == 'reviewer' else 'annotator')
            )
            require(
                ProjectMember.objects.filter(project_id=p['id'], user_id=u['id'], role=role, enabled=True).exists(),
                'Synthetic role row mismatch',
            )
        for s in p['sources']:
            require(
                Submission.objects.filter(
                    pk=s['id'],
                    assignment_id=s['assignment_id'],
                    assignment__project_id=p['id'],
                    assignment__task_id=s['task_id'],
                    annotation_id=s['annotation_id'],
                    submitted_by_id=value['users'][s['owner']]['id'],
                ).exists(),
                'Synthetic source row mismatch',
            )
    for p in clean['ANNOTATION_REVIEW_CONFIDENCE_PROFILES']:
        require(c.descriptor(p['descriptor']) == p['descriptor'], 'Synthetic descriptor contract invalid')
    return clean


def isolation():
    require(
        os.environ.get('TEST_ENVIRONMENT') == '1' and not os.environ.get('ENTERPRISE_E2E_CONTAINER'),
        'Only native synthetic TEST_ENVIRONMENT is supported',
    )
    require(
        os.environ.get('DJANGO_DB') == 'postgresql' and os.environ.get('POSTGRE_HOST') in ('localhost', '127.0.0.1'),
        'Only isolated loopback PostgreSQL supported',
    )
    require(
        re.fullmatch(r'ui_ci_[a-z0-9_]+', os.environ.get('POSTGRE_NAME', '')), 'Synthetic database namespace required'
    )
    scope = read_json(os.environ['UI_SCOPE_FILE'])
    require(
        scope['workspace'] == str(ROOT.resolve())
        and scope['database'] == os.environ['POSTGRE_NAME']
        and scope['port'] == os.environ['POSTGRE_PORT']
        and scope['data_dir'] == os.environ['BASE_DATA_DIR'],
        'Synthetic process scope mismatch',
    )
    return scope


def fixture_mode(scope, *, confidence=False, second_review=False):
    from ui_inventory import SHARDS

    shard = scope.get('shard')
    require(shard in SHARDS and not (confidence and second_review), 'Unknown synthetic bootstrap mode')
    mode = 'second-review' if second_review else 'confidence' if confidence else 'legacy'
    expected = shard if shard in ('confidence', 'second-review') else 'legacy'
    require(mode == expected, 'Synthetic CLI mode does not match the scoped shard')
    return mode


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--confidence', action='store_true')
    modes.add_argument('--second-review', action='store_true')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    scope = isolation()
    mode = fixture_mode(scope, confidence=args.confidence, second_review=args.second_review)
    from ui_common import verify_context
    from ui_run import validate_database_owner

    verify_context(scope['context'])
    validate_database_owner(scope['context'], Path(os.environ['UI_SCOPE_FILE']).parent, fresh=False)
    from ui_catalogs import validate_catalogs

    locale_path = validate_catalogs(Path(os.environ['UI_SCOPE_FILE']).parent)
    require(integer(args.port, 1024, 65535), 'Loopback port invalid')
    sys.path.insert(0, str(ROOT / 'label_studio'))
    os.environ['DJANGO_SETTINGS_MODULE'] = 'core.settings.label_studio'
    import django
    from django.conf import settings
    from django.core.management import call_command
    from django.test import override_settings

    fixture_root = ROOT / 'web/apps/labelstudio-e2e'
    configured = {}
    value = None
    if mode == 'second-review':
        from ui_second_review import validate_fixture as validate_second_review

        value = read_json(fixture_root / '.second-review-e2e.json')
        configured = validate_second_review(value)
    elif mode == 'confidence':
        value = read_json(fixture_root / '.review-confidence-e2e.json')
        configured = validate_fixture(value)
    else:
        review = read_json(fixture_root / '.review84-e2e.json')
        classification = read_json(fixture_root / '.classification81-e2e.json')
        batches = read_json(fixture_root / '.classification-batches81-e2e.json')
        configured = {
            'ANNOTATION_FIXED_PACKAGES_ENABLED': True,
            'ANNOTATION_REVIEW_BATCHES_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED': True,
            'ANNOTATION_CLASSIFICATION_BATCHES_ENABLED': True,
            'ANNOTATION_REVIEW_BATCH_MODES': review['modes'],
            'ANNOTATION_CLASSIFICATION_DISAGREEMENT_MODES': {**classification['modes'], **batches['modes']},
        }
    require(settings.TEST_ENVIRONMENT, 'Django synthetic environment required')
    configured['LOCALE_PATHS'] = [str(locale_path)]
    with override_settings(**configured):
        django.setup()
        if mode == 'second-review':
            from ui_second_review import validate_database as validate_second_review_database

            validate_second_review_database(value)
        elif value:
            validate_database(value)
        # Autoreload would spawn a child without these in-memory settings.
        call_command('runserver', f'127.0.0.1:{args.port}', use_reloader=False)


if __name__ == '__main__':
    main()
