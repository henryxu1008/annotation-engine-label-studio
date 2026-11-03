"""CI-only second-review fixture validation; never a product settings loader."""
import re
from copy import deepcopy

from ui_common import integer, require
from ui_server import uuid_string

DOMAIN = 'second-review84.example.test'
PROVIDER = 'core.management.commands.seed_second_review_e2e.SecondReviewE2EPolicy'
SETTING_KEYS = {
    'ANNOTATION_SECOND_REVIEW_ENABLED',
    'ANNOTATION_SECOND_REVIEW_INSTANCE_ID',
    'ANNOTATION_SECOND_REVIEW_MODES',
    'ANNOTATION_REVIEW_BATCHES_ENABLED',
    'ANNOTATION_REVIEW_BATCH_MODES',
    'ANNOTATION_PROJECT_OPERATION_PROVIDER',
}


def organization(configured, project_ids):
    modes = configured['ANNOTATION_SECOND_REVIEW_MODES']
    require(isinstance(modes, dict) and len(modes) == 2, 'Synthetic mode count invalid')
    keys = [re.fullmatch(r'([1-9][0-9]*):([1-9][0-9]*)', key) for key in modes if isinstance(key, str)]
    require(len(keys) == 2 and all(keys), 'Synthetic mode keys invalid')
    orgs = {int(key[1]) for key in keys}
    require(len(orgs) == 1 and all(integer(org, 1) for org in orgs), 'Synthetic organization invalid')
    org = next(iter(orgs))
    expected = {f'{org}:{project}': 'fork_review' for project in project_ids}
    require(modes == configured['ANNOTATION_REVIEW_BATCH_MODES'] == expected, 'Synthetic project modes differ')
    return org


def validate_fixture(value):
    require(
        isinstance(value, dict) and set(value) == {'schema', 'password', 'users', 'projects', 'settings'},
        'Synthetic supervision fixture shape invalid',
    )
    require(value['schema'] == 'second-review-e2e/v1', 'Synthetic supervision schema invalid')
    require(
        isinstance(value['password'], str) and re.fullmatch(r'[A-Za-z0-9_-]{20,128}', value['password']),
        'Synthetic password format invalid',
    )
    users, projects, configured = value['users'], value['projects'], value['settings']
    require(
        isinstance(users, dict) and set(users) == {'manager', 'initial', 'supervisor', 'a', 'b'},
        'Synthetic users invalid',
    )
    require(
        all(
            isinstance(user, dict)
            and set(user) == {'id', 'email'}
            and integer(user['id'], 1)
            and user['email'] == name + '@' + DOMAIN
            for name, user in users.items()
        ),
        'Synthetic user scope invalid',
    )
    require(len({user['id'] for user in users.values()}) == 5, 'Synthetic humans must be independent')
    require(isinstance(projects, dict) and set(projects) == {'en-US', 'zh-CN'}, 'Synthetic language scope invalid')
    require(
        all(
            isinstance(project, dict)
            and set(project) == {'id', 'batch_id', 'sources'}
            and integer(project['id'], 1)
            and integer(project['batch_id'], 1)
            and isinstance(project['sources'], list)
            and len(project['sources']) == 4
            for project in projects.values()
        ),
        'Synthetic projects invalid',
    )
    project_ids = {project['id'] for project in projects.values()}
    require(
        len(project_ids) == len({project['batch_id'] for project in projects.values()}) == 2,
        'Duplicate synthetic project/batch',
    )
    require(
        isinstance(configured, dict)
        and set(configured) in (SETTING_KEYS, SETTING_KEYS | {'ANNOTATION_SECOND_REVIEW_FOLLOWUP_ENABLED'}),
        'Settings allowlist violation',
    )
    if 'ANNOTATION_SECOND_REVIEW_FOLLOWUP_ENABLED' in configured:
        require(configured['ANNOTATION_SECOND_REVIEW_FOLLOWUP_ENABLED'] is True, 'Synthetic follow-up flag invalid')
    require(
        configured['ANNOTATION_SECOND_REVIEW_ENABLED'] is True
        and configured['ANNOTATION_REVIEW_BATCHES_ENABLED'] is True,
        'Synthetic flags must be booleans',
    )
    require(configured['ANNOTATION_PROJECT_OPERATION_PROVIDER'] == PROVIDER, 'Arbitrary policy import forbidden')
    require(uuid_string(configured['ANNOTATION_SECOND_REVIEW_INSTANCE_ID']), 'Synthetic instance invalid')
    organization(configured, project_ids)
    seen = {key: set() for key in ('id', 'assignment_id', 'task_id', 'annotation_id', 'second_task_id')}
    for project in projects.values():
        for i, source in enumerate(project['sources']):
            keys = {'id', 'assignment_id', 'task_id', 'annotation_id', 'owner'} | (
                {'second_task_id'} if i == 3 else set()
            )
            require(isinstance(source, dict) and set(source) == keys, 'Synthetic source shape invalid')
            require(source['owner'] == ('a' if i % 2 == 0 else 'b'), 'Synthetic submitter invalid')
            for key in ('id', 'assignment_id', 'task_id', 'annotation_id'):
                require(integer(source[key], 1) and source[key] not in seen[key], 'Synthetic source IDs invalid')
                seen[key].add(source[key])
            if i == 3:
                identifier = source['second_task_id']
                require(
                    uuid_string(identifier) and identifier not in seen['second_task_id'], 'Synthetic history invalid'
                )
                seen['second_task_id'].add(identifier)
    result = deepcopy(configured)
    # A historical six-key fixture must not inherit an ambient operator opt-in.
    result.setdefault('ANNOTATION_SECOND_REVIEW_FOLLOWUP_ENABLED', False)
    return result


def validate_database(value):
    from organizations.models import Organization
    from projects.models import Project, ProjectMember
    from rest_framework.test import APIClient
    from review_batches.models import ReviewBatch
    from tasks.models import ReviewDecision, Submission, TaskAssignment
    from tasks.my_work import work_projects
    from users.models import User

    configured = validate_fixture(value)
    org = organization(configured, {project['id'] for project in value['projects'].values()})
    users = value['users']
    manager = users['manager']['id']
    client = APIClient()
    client.force_authenticate(User.objects.get(pk=manager))
    require(
        Organization.objects.filter(pk=org, title='Synthetic independent supervision').exists(),
        'Synthetic organization row mismatch',
    )
    for name, user in users.items():
        require(
            User.objects.filter(
                pk=user['id'], email=user['email'], is_active=True, active_organization_id=org
            ).exists(),
            'Synthetic user row mismatch',
        )
    for locale, project in value['projects'].items():
        require(
            Project.objects.filter(
                pk=project['id'], organization_id=org, created_by_id=manager, title='Synthetic supervision ' + locale
            ).exists(),
            'Synthetic project row mismatch',
        )
        for name, user in users.items():
            role = 'manager' if name == 'manager' else 'reviewer' if name in ('initial', 'supervisor') else 'annotator'
            require(
                ProjectMember.objects.filter(
                    project_id=project['id'], user_id=user['id'], role=role, enabled=True
                ).exists(),
                'Synthetic membership row mismatch',
            )
            # Reuse the authoritative account/org/project predicates too.
            actor = User(pk=user['id'])
            require(
                work_projects(actor, (role,)).filter(pk=project['id']).exists(),
                'Synthetic current organization membership mismatch',
            )
        require(
            ReviewBatch.objects.filter(
                pk=project['batch_id'], project_id=project['id'], created_by_id=manager
            ).exists(),
            'Synthetic batch row mismatch',
        )
        for i, source in enumerate(project['sources']):
            owner = users[source['owner']]['id']
            require(
                TaskAssignment.objects.filter(
                    pk=source['assignment_id'],
                    task_id=source['task_id'],
                    project_id=project['id'],
                    annotation_id=source['annotation_id'],
                    assignee_id=owner,
                ).exists(),
                'Synthetic assignment row mismatch',
            )
            require(
                Submission.objects.filter(
                    pk=source['id'],
                    assignment_id=source['assignment_id'],
                    annotation_id=source['annotation_id'],
                    submitted_by_id=owner,
                    revision=1,
                    status='rejected' if i == 1 else 'approved',
                ).exists(),
                'Synthetic source row mismatch',
            )
            require(
                ReviewDecision.objects.filter(
                    submission_id=source['id'],
                    reviewer_id=users['initial']['id'],
                    decision='rejected' if i == 1 else 'approved',
                ).exists(),
                'Synthetic initial review row mismatch',
            )
            if i == 3:
                # The product read performs bounded proof/receipt validation,
                # nullable-link checks and both frozen-source passes. A new
                # same-ID initial review cannot repair a deleted original link.
                response = client.get(
                    f'/api/projects/{project["id"]}/second-reviews/{source["second_task_id"]}/source/'
                )
                require(
                    response.status_code == 200 and response.data.get('historical_revision') is True,
                    'Synthetic frozen supervision row mismatch',
                )
                expected = dict(
                    id=source['second_task_id'],
                    project_id=project['id'],
                    batch_id=project['batch_id'],
                    submission_id=source['id'],
                    revision=1,
                    assignee_id=users['supervisor']['id'],
                    status='pending',
                    version=1,
                    effect='supervisory_only',
                )
                require(
                    all(response.data['task'].get(key) == item for key, item in expected.items())
                    and response.data['first_review']['reviewer_id'] == users['initial']['id'],
                    'Synthetic historical task binding mismatch',
                )
                require(
                    Submission.objects.filter(
                        assignment_id=source['assignment_id'], revision=2, status='pending', submitted_by_id=owner
                    ).exists(),
                    'Synthetic newer revision missing',
                )
    return configured
