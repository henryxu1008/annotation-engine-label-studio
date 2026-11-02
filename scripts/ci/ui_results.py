"""Validate structured test outcomes, including explicit existing unit skips."""
import math
from pathlib import Path
from xml.etree import ElementTree

from ui_common import REPORT_LIMIT, ROOT, integer, read_json, require


def unit_skips():
    value = read_json(ROOT / 'scripts/ci/ui_unit_skips.json')
    require(
        value.get('schema') == 'ui-unit-skips/v1' and isinstance(value.get('commands'), dict), 'Skip allowlist invalid'
    )
    return value['commands']


def normalize_jest(value):
    require(isinstance(value, dict) and isinstance(value.get('testResults'), list), 'Jest result invalid')
    tests = []
    for suite in value['testResults']:
        path = Path(suite['name']).resolve().relative_to((ROOT / 'web').resolve()).as_posix()
        for test in suite['assertionResults']:
            tests.append({'title': path + '::' + test['fullName'], 'state': test['status']})
    require(integer(value.get('numTotalTests'), 1) and len(tests) == value['numTotalTests'], 'Jest zero/missing tests')
    require(
        value.get('success') is True
        and type(value.get('numFailedTests')) is int
        and value['numFailedTests'] == 0
        and type(value.get('numRuntimeErrorTestSuites', 0)) is int
        and value.get('numRuntimeErrorTestSuites', 0) == 0,
        'Jest failures/runtime error',
    )
    return tests


def normalize_node_xml(raw):
    require(
        len(raw) <= REPORT_LIMIT and b'<!DOCTYPE' not in raw.upper() and b'<!ENTITY' not in raw.upper(),
        'Unsafe JUnit report',
    )
    root = ElementTree.fromstring(raw)
    tests = []
    for test in root.iter('testcase'):
        state = (
            'failed'
            if test.find('failure') is not None or test.find('error') is not None
            else ('pending' if test.find('skipped') is not None else 'passed')
        )
        tests.append({'title': test.attrib.get('classname', '') + '::' + test.attrib['name'], 'state': state})
    require(tests, 'Node zero/missing tests')
    return tests


def validate_unit(tests, command):
    require(isinstance(tests, list) and 0 < len(tests) <= 50000, 'Unit test count invalid')
    allowed = unit_skips().get(command, {})
    require(
        isinstance(allowed, dict) and all(isinstance(v, str) and v.strip() for v in allowed.values()),
        'Skip reason missing',
    )
    skipped = []
    for t in tests:
        require(
            set(t) == {'title', 'state'} and isinstance(t['title'], str) and 0 < len(t['title'].encode()) <= 8192,
            'Unit test shape invalid',
        )
        require(t['state'] in ('passed', 'pending', 'todo'), 'Unit failed or unknown')
        if t['state'] != 'passed':
            require(t['title'] in allowed, 'Unexpected unit skip: ' + t['title'])
            skipped.append(t['title'])
    require(
        set(skipped) == set(allowed) and len(skipped) < len(tests), 'Skip allowlist changed or entire suite skipped'
    )
    return {'passed': len(tests) - len(skipped), 'skipped': skipped}


def validate_report(report, expected):
    require(isinstance(report, dict) and report.get('schema') == 'ui-test-report/v1', 'Test report schema invalid')
    require(report.get('call') == expected, 'Actual invocation differs from closed inventory')
    kind = report.get('kind')
    if 'spec' not in expected:
        require(kind == expected['kind'], 'Prepare report kind mismatch')
        if kind in ('node', 'jest'):
            validate_unit(report.get('tests'), expected['id'])
        else:
            require(
                kind in ('validation', 'build') and report.get('exit_code') == 0, 'Prepare validation/build failed'
            )
        require(type(report.get('exit_code')) is int and report['exit_code'] == 0, 'Prepare exit failure')
        return
    require(
        kind == 'cypress'
        and report.get('actual_spec') == expected['spec']
        and report.get('actual_config') == expected['config_file']
        and report.get('browser') == expected['browser'],
        'Cypress actual source/config/browser mismatch',
    )
    tests = report.get('tests')
    require(isinstance(tests, list) and 0 < len(tests) <= 10000, 'Browser zero/missing tests')
    require(
        integer(report.get('passed'), 1)
        and report['passed'] == len(tests)
        and all(type(report.get(k)) is int and report[k] == 0 for k in ('failed', 'pending', 'skipped')),
        'Browser stats mismatch',
    )
    require(report.get('languages') == expected['languages'], 'Browser language evidence mismatch')
    for test in tests:
        require(
            set(test) == {'title', 'state', 'attempts'}
            and isinstance(test['title'], str)
            and 0 < len(test['title'].encode()) <= 8192
            and test['state'] == 'passed'
            and type(test['attempts']) is int
            and test['attempts'] == 1,
            'Browser failed/retried test',
        )
    if expected['language_evidence'] == 'test-title':
        require(
            all(any(locale in t['title'] for t in tests) for locale in expected['languages']), 'Missing language cases'
        )
    elif expected['language_evidence'] == 'in-spec-switch':
        required = [
            'keeps member role codes stable while translating the live roster',
            'preserves selected assignee and assignment version when switching inside its modal',
            'keeps the typed rejection reason and exact pending hash through a modal switch',
            'releases only the selected approved revision after a live language change',
        ]
        require(
            len(tests) == 4 and all(any(t['title'].endswith(' / ' + title) for t in tests) for title in required),
            'Missing frozen in-spec language-switch journeys',
        )
    else:
        require(
            expected['language_evidence'] == 'configured' and expected['env'], 'Locale configuration proof missing'
        )
    duration = report.get('duration_ms')
    require(
        type(duration) in (int, float) and math.isfinite(duration) and 0 <= duration <= 40 * 60 * 1000,
        'Browser duration invalid',
    )


def report_reference(folder, relative):
    from ui_common import file_hash

    p = Path(folder) / relative
    require(p.stat().st_size <= REPORT_LIMIT, 'Report byte budget')
    return {'path': relative, 'size': p.stat().st_size, 'sha256': file_hash(p, REPORT_LIMIT)}
