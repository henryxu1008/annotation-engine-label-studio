"""Dispatch-only project dependency cache isolation; never deletes or claims a fresh OS."""
import os
from pathlib import Path

from ui_common import ROOT, canonical, read_json, require, same_source, write_json

LAYERS = {
    'POETRY_CACHE_DIR': 'poetry-cache',
    'POETRY_VIRTUALENVS_PATH': 'poetry-venvs',
    'PIP_CACHE_DIR': 'pip-cache',
    'YARN_CACHE_FOLDER': 'yarn-cache',
    'CYPRESS_CACHE_FOLDER': 'cypress-cache',
    'XDG_CACHE_HOME': 'xdg-cache',
}


def expected_evidence(mode):
    require(mode in ('cold', 'warm', 'local_existing'), 'Dependency cache mode invalid')
    return {
        'requested': mode,
        'project_cache_isolated': mode == 'cold',
        'initially_empty': mode == 'cold',
        'layers': sorted(LAYERS) if mode == 'cold' else [],
    }


def configure(ctx):
    require(
        ctx['environment'] == 'github'
        and os.environ.get('GITHUB_ACTIONS') == 'true'
        and ctx['cache_mode'] == os.environ.get('UI_DEPENDENCY_CACHE')
        and (ctx['cache_mode'] == 'warm' or ctx['event'] == 'workflow_dispatch'),
        'Cache setup must match the authenticated workflow event',
    )
    temporary = Path(os.environ['RUNNER_TEMP'])
    require(
        temporary.is_absolute()
        and temporary.resolve() == temporary.absolute()
        and not temporary.resolve().is_relative_to(ROOT.resolve()),
        'Job-private dependency cache parent required',
    )
    proof = temporary / 'ui-dependency-cache-proof.json'
    require(not proof.exists() and not proof.is_symlink(), 'Never reuse a dependency cache proof')
    evidence = expected_evidence(ctx['cache_mode'])
    if ctx['cache_mode'] == 'cold':
        base = temporary / 'ui-cold-dependencies'
        require(not base.exists() and not base.is_symlink(), 'Cold cache directory already exists')
        base.mkdir()
        updates = {}
        for key, name in LAYERS.items():
            target = base / name
            target.mkdir()
            require(not any(target.iterdir()), 'Cold project cache must initially be empty')
            updates[key] = str(target)
        updates['POETRY_VIRTUALENVS_IN_PROJECT'] = 'false'
        require(all('\n' not in v and '\r' not in v for v in updates.values()), 'Unsafe cache environment path')
        with Path(os.environ['GITHUB_ENV']).open('a', encoding='utf-8') as output:
            for key, value in updates.items():
                output.write(key + '=' + value + '\n')
    write_json(proof, {'schema': 'ui-dependency-cache/v1', 'context': ctx, 'evidence': evidence})
    print('UI_DEPENDENCY_CACHE', ctx['cache_mode'], flush=True)


def evidence(ctx):
    if ctx['environment'] == 'local':
        return expected_evidence('local_existing')
    temporary = Path(os.environ['RUNNER_TEMP'])
    value = read_json(temporary / 'ui-dependency-cache-proof.json')
    require(value.get('schema') == 'ui-dependency-cache/v1', 'Dependency cache proof missing')
    same_source(ctx, value['context'])
    require(value['context']['attempt'] == ctx['attempt'], 'Dependency cache proof attempt mismatch')
    expected = expected_evidence(ctx['cache_mode'])
    require(canonical(value['evidence']) == canonical(expected), 'Dependency cache proof invalid')
    if ctx['cache_mode'] == 'cold':
        base = temporary / 'ui-cold-dependencies'
        require(
            all(os.environ.get(key) == str(base / name) for key, name in LAYERS.items())
            and os.environ.get('POETRY_VIRTUALENVS_IN_PROJECT') == 'false',
            'Cold dependency environment changed',
        )
    return expected
