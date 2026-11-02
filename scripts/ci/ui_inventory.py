"""Closed migration inventory. No discovery, changed-file selection, or cloud scheduler."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJECT = 'apps/labelstudio-e2e'
SPEC_DIR = PROJECT + '/src/e2e/'
BASE_CONFIG = 'cypress.config.ts'
SHARDS = {
    'review-classification': ['review-batches', 'classification-disagreement', 'classification-batches'],
    'enterprise-ui': [
        'enterprise-current-ui',
        'project-members',
        'task-assignments',
        'reviewer-workspace',
        'submission-release',
    ],
    'locale-a': ['dm-entity-boundary', 'auth-tips-timeout', 'app-locale', 'dm-editor-locale'],
    'locale-b': ['dm-task-switch', 'dm-keyboard-ime', 'dm-entity-draft', 'dm-image-draft'],
    'locale-c': ['annotator-label-stream', 'dm-export-locale', 'collaboration-locale'],
    'full-en': ['full-enterprise-workflow'],
    'full-zh': ['full-enterprise-workflow'],
    'packages-import': ['fixed-task-packages', 'import-initialization'],
    'confidence': ['review-confidence'],
    'second-review': ['second-review'],
}
JOB_NAMES = {'prepare': 'Prepare enterprise UI', **{s: 'Enterprise UI / ' + s for s in SHARDS}}
JEST_UI_PATHS = [
    'libs/ui/src/lib/dropdown/dropdown.test.tsx',
    'libs/ui/src/lib/dropdown/dropdown-transition.test.tsx',
    'libs/ui/src/lib/toast/toast.test.tsx',
]
PREPARE = [
    ('catalogs', ['yarn', 'i18n:catalogs'], 'validation'),
    ('literals', ['yarn', 'i18n:literals'], 'node'),
    ('diagnostics', ['node', '--test', 'apps/labelstudio-e2e/src/support/failure-diagnostics.test.cjs'], 'node'),
    ('i18n', ['yarn', 'nx', 'run', 'i18n:unit'], 'jest'),
    ('labelstudio', ['yarn', 'ls:unit'], 'jest'),
    ('app-common', ['yarn', 'nx', 'run', 'app-common:unit'], 'jest'),
    (
        'ui',
        ['yarn', 'jest', '--config', 'libs/ui/jest.config.ts', '--runInBand', '--runTestsByPath', *JEST_UI_PATHS],
        'jest',
    ),
    ('datamanager', ['yarn', 'dm:unit'], 'jest'),
    ('editor', ['yarn', 'lsf:unit'], 'jest'),
    ('build', ['yarn', 'ls:build'], 'build'),
]


def calls(shard):
    if shard not in SHARDS:
        raise ValueError('Unknown UI shard')
    result = []
    for name in SHARDS[shard]:
        env = {}
        if shard == 'enterprise-ui':
            env = {'loginLocale': 'en-US'}
        if shard in ('full-en', 'full-zh'):
            env = {'fullFlowLocale': 'en-US' if shard == 'full-en' else 'zh-CN'}
        result.append(
            {
                'id': name + ('-' + env['fullFlowLocale'] if 'fullFlowLocale' in env else ''),
                'spec': SPEC_DIR + name + '.cy.ts',
                'config_file': 'cypress.second-review.config.ts'
                if shard == 'second-review'
                else 'cypress.review-confidence.config.ts'
                if shard == 'confidence'
                else BASE_CONFIG,
                'env': env,
                'browser': 'electron',
                'video': True,
                'reset': shard.startswith('locale-') or shard in ('full-en', 'full-zh', 'packages-import'),
                'timeout_seconds': 300 if name in ('dm-entity-boundary', 'second-review') else None,
                'kill_grace_seconds': 15 if name in ('dm-entity-boundary', 'second-review') else None,
                'languages': [env['fullFlowLocale']]
                if 'fullFlowLocale' in env
                else (['en-US'] if shard == 'enterprise-ui' else ['en-US', 'zh-CN']),
                'language_evidence': 'in-spec-switch'
                if name == 'collaboration-locale'
                else ('configured' if env else 'test-title'),
            }
        )
    return result


def inventory():
    result = {
        'schema': 'ui-journeys/v1',
        'shards': {s: calls(s) for s in SHARDS},
        'prepare': [{'id': k, 'command': v, 'kind': t} for k, v, t in PREPARE],
    }
    flat = [c['id'] for group in result['shards'].values() for c in group]
    if len(result['shards']) != 10 or len(flat) != 25 or len(set(flat)) != 25:
        raise ValueError('Incomplete UI inventory')
    return result


def prepare_commands(item, private='@private@'):
    """The only permitted reporter extensions to the original command inventory."""
    private = str(private).replace('\\', '/').rstrip('/')
    name = item['id']
    argv = list(item['command'])
    if item['kind'] == 'jest':
        argv += ['--json', '--outputFile', private + '/' + name + '.json']
        if '--runInBand' not in argv:
            argv.append('--runInBand')
    if name == 'diagnostics':
        argv = [
            'node',
            '--test',
            '--test-reporter=junit',
            '--test-reporter-destination=' + private + '/' + name + '.xml',
            *item['command'][2:],
        ]
    if name == 'literals':
        return [
            ['node', 'libs/i18n/scripts/check-migrated-literals.cjs'],
            [
                'node',
                '--test',
                '--test-reporter=junit',
                '--test-reporter-destination=' + private + '/literals.xml',
                'libs/i18n/scripts/check-migrated-literals.test.cjs',
            ],
        ]
    return [argv]


def normalize_commands(commands, private):
    prefix = str(private).replace('\\', '/').rstrip('/')
    return [[v.replace('\\', '/').replace(prefix + '/', '@private@/') for v in cmd] for cmd in commands]
