"""Offline policy checks; GitHub itself evaluates the job condition in the smoke run."""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github' / 'workflows'
UPSTREAM_ONLY = "${{ github.repository == 'HumanSignal/label-studio' }}"
FORK_ONLY = "${{ github.repository == 'qq550723504/annotation-engine-label-studio' }}"


def load_workflow(name):
    # BaseLoader preserves Actions' `on` key instead of YAML 1.1 boolean coercion.
    return yaml.load((WORKFLOWS / name).read_text(encoding='utf-8'), Loader=yaml.BaseLoader)


class FeatureFlagsWorkflowBoundary(unittest.TestCase):
    def setUp(self):
        self.workflow = load_workflow('feature-flags-update.yml')
        self.job = self.workflow['jobs']['commit-feature-flags']

    def test_every_sync_job_is_guarded_before_any_steps(self):
        # An exact trusted repository guard covers schedule, dispatch, and callers.
        # Do not replace it with owner/fork/ref/input/secret checks or step guards:
        # those admit other repositories or leave debug/comment/always() steps live.
        self.assertEqual(set(self.workflow['jobs']), {'commit-feature-flags'})
        self.assertEqual(self.job['if'], UPSTREAM_ONLY)
        self.assertNotIn('continue-on-error', self.job)

    def test_existing_entries_and_ref_contract_are_preserved(self):
        entries = self.workflow['on']
        self.assertEqual(set(entries), {'schedule', 'workflow_dispatch', 'workflow_call'})
        self.assertEqual(entries['schedule'], [{'cron': '0 6 * * 1-5'}])
        for entry in ('workflow_dispatch', 'workflow_call'):
            with self.subTest(entry=entry):
                inputs = entries[entry]['inputs']
                self.assertEqual(set(inputs), {'ref'})
                self.assertEqual(inputs['ref']['default'], 'develop')
                self.assertEqual(inputs['ref']['required'], 'true')
                self.assertEqual(inputs['ref']['type'], 'string')

    def test_upstream_credentials_and_sync_operations_are_not_replaced(self):
        steps = {step.get('name'): step for step in self.job['steps']}
        self.assertEqual(steps['Checkout']['with']['token'], '${{ secrets.GIT_PAT }}')
        self.assertEqual(steps['Checkout']['with']['ref'], '${{ inputs.ref }}')
        download = steps['Download feature flags']
        self.assertEqual(download['env'], {
            'LAUNCHDARKLY_COMMUNITY_SDK_KEY': '${{ secrets.LAUNCHDARKLY_COMMUNITY_SDK_KEY }}',
        })
        self.assertIn('https://sdk.launchdarkly.com/sdk/latest-all', download['run'])
        self.assertIn('git push origin HEAD', steps['Commit and Push']['run'])
        for name in ('Checkout', 'Download feature flags', 'Commit and Push'):
            with self.subTest(step=name):
                self.assertNotIn('continue-on-error', steps[name])
        self.assertEqual(self.workflow['env']['LAUNCHDARKLY_DOWNLOAD_PATH'], 'label_studio/feature_flags.json')

    def test_existing_reusable_callers_still_reach_the_guarded_workflow(self):
        for filename, job_id in (
            ('ff-command.yml', 'update-ff'),
            ('release-cut-off-release-branch.yml', 'update-feature-flags'),
        ):
            with self.subTest(caller=filename):
                caller = load_workflow(filename)['jobs'][job_id]
                self.assertEqual(caller['uses'], './.github/workflows/feature-flags-update.yml')
                self.assertEqual(caller['secrets'], 'inherit')

    def test_live_reusable_probe_is_fork_only_read_only_and_has_no_secrets(self):
        probe = load_workflow('feature-flags-boundary.yml')
        caller = probe['jobs']['fork-reusable-skip']
        # This independent guard prevents the test from synchronizing on upstream.
        self.assertEqual(caller['if'], FORK_ONLY)
        self.assertEqual(caller['needs'], 'contracts')
        self.assertEqual(caller['permissions'], {})
        self.assertNotIn('secrets', caller)
        self.assertEqual(caller['uses'], './.github/workflows/feature-flags-update.yml')
        # Deliberately untrusted input: naming upstream must not grant admission.
        self.assertEqual(caller['with'], {'ref': 'HumanSignal/label-studio'})
        self.assertEqual(probe['permissions'], {'contents': 'read'})
        self.assertNotIn('pull_request_target', probe['on'])
        for event in ('push', 'pull_request'):
            with self.subTest(event=event):
                paths = probe['on'][event]['paths']
                for path in (
                    '.github/workflows/feature-flags-update.yml',
                    '.github/workflows/feature-flags-boundary.yml',
                    'scripts/test_feature_flags_workflow.py',
                ):
                    self.assertIn(path, paths)
        commands = '\n'.join(step.get('run', '') for step in probe['jobs']['contracts']['steps'])
        self.assertIn("test_feature_flags_workflow.py", commands)


if __name__ == '__main__':
    unittest.main()
