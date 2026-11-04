"""Audit is an additional closed call; original25 calls/budgets stay exact."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_second_review_ci import fixture as supervision_fixture
from ui_audit import PROVIDER, validate_fixture
from ui_common import ROOT, digest
from ui_inventory import calls, inventory
from ui_second_review import validate_fixture as validate_supervision


def fixture():
    source = supervision_fixture()
    configured = validate_supervision(source)
    configured.update(
        ANNOTATION_PROJECT_OPERATION_PROVIDER=PROVIDER,
        ANNOTATION_REVIEW_AUDIT_ENABLED=True,
        ANNOTATION_REVIEW_AUDIT_POLICY=dict(
            instance_id=str(uuid4()),
            projects={
                key: dict(mode='fork_review', version='synthetic-audit/v1')
                for key in configured['ANNOTATION_REVIEW_BATCH_MODES']
            },
        ),
        APPROVED_EXPORT_ROOT=str(Path(tempfile.gettempdir()).resolve() / 'audit-private-exports'),
    )
    return dict(
        schema='review-audit-e2e/v1',
        supervision=source,
        settings=configured,
        ready={locale: str(uuid4()) for locale in ('en-US', 'zh-CN')},
    )


class AuditInventory(unittest.TestCase):
    def test_original25_inventory_remains_byte_semantic_identical(self):
        value = inventory()
        value['shards'].pop('review-audit')
        self.assertEqual(digest(value), '10399a863dbbbdc5a244a89bf895877f8b6061d0453b95b6555556402eff5968')

    def test_new_call_has_its_own_native_bilingual_budget(self):
        call = calls('review-audit')[0]
        self.assertEqual(len(calls('review-audit')), 1)
        self.assertEqual((call['timeout_seconds'], call['kill_grace_seconds']), (300, 15))
        self.assertEqual(call['languages'], ['en-US', 'zh-CN'])
        self.assertEqual(call['config_file'], 'cypress.review-audit.config.ts')
        self.assertEqual(call['browser'], 'electron')
        self.assertIn('review-audit', inventory()['shards'])

    def test_workflow_preserves_original_required_name_parallelism_and_limits(self):
        import yaml

        jobs = yaml.load(
            (ROOT / '.github/workflows/enterprise-browser-e2e.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader
        )['jobs']
        browser = jobs['browser']
        self.assertEqual(browser['strategy']['max-parallel'], '3')
        self.assertEqual(browser['timeout-minutes'], '40')
        self.assertIn('review-audit', browser['strategy']['matrix']['shard'])
        self.assertEqual(jobs['current-ui-validation']['name'], 'Current enterprise UI validation')

    def test_mode_requires_explicit_exact_shard(self):
        from ui_server import fixture_mode

        self.assertEqual(fixture_mode({'shard': 'review-audit'}, review_audit=True), 'review-audit')
        for scope, flags in (
            ({'shard': 'second-review'}, dict(review_audit=True)),
            ({'shard': 'review-audit'}, {}),
            ({'shard': 'review-audit'}, dict(review_audit=True, second_review=True)),
        ):
            with self.assertRaises(ValueError):
                fixture_mode(scope, **flags)

    def test_fixture_is_strict_and_never_inherits_ambient_settings(self):
        original = fixture()
        configured = validate_fixture(original)
        configured['ANNOTATION_REVIEW_AUDIT_POLICY']['projects'].clear()
        self.assertTrue(original['settings']['ANNOTATION_REVIEW_AUDIT_POLICY']['projects'])
        for mutate in (
            lambda v: v.update(schema='unknown'),
            lambda v: v.update(extra=1),
            lambda v: v['settings'].update(ANNOTATION_REVIEW_AUDIT_ENABLED=1),
            lambda v: v['settings'].update(ANNOTATION_PROJECT_OPERATION_PROVIDER='arbitrary.import'),
            lambda v: v['settings'].update(unknown='secret'),
            lambda v: v['settings']['ANNOTATION_REVIEW_AUDIT_POLICY'].update(instance_id='invalid'),
            lambda v: v['settings']['ANNOTATION_REVIEW_AUDIT_POLICY']['projects'].clear(),
            lambda v: v['settings'].update(APPROVED_EXPORT_ROOT='relative'),
            lambda v: v['ready'].update({'en-US': 'not-uuid'}),
        ):
            candidate = copy.deepcopy(original)
            mutate(candidate)
            with self.assertRaises(ValueError):
                validate_fixture(candidate)
