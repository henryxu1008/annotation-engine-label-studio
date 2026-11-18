"""The package overlay must not clone app configuration into a divergent worker."""

from pathlib import Path

import yaml


def test_automation_overlay_only_adds_disabled_flags_to_final_app():
    root = Path(__file__).resolve().parents[3]
    services = yaml.safe_load((root / 'docker-compose.task-packages.yml').read_text(encoding='utf-8'))['services']
    assert set(services) == {'app'}
    assert set(services['app']) == {'environment'}
    flags = services['app']['environment']
    for name in (
        'ANNOTATION_FIXED_PACKAGES_ENABLED',
        'ANNOTATION_PACKAGE_CLAIM_ENABLED',
        'ANNOTATION_PACKAGE_DISPATCH_ENABLED',
        'ANNOTATION_PACKAGE_ADVANCED_ENABLED',
        'ANNOTATION_PACKAGE_AUTOMATION_ENABLED',
    ):
        assert flags[name] == f'${{{name}:-false}}'
