"""Explicit acceptance entrypoint only; never a production provider fallback."""
from ls84_contract import VERSION, policy_matches, seed_submission_allowed


class IsolatedAcceptancePolicy:
    def evaluate(self, project):
        from access_control.project_operations import (
            EXPORT_REVIEW_AUDIT,
            ORGANIZE_SECOND_REVIEW,
            PROVIDE_REVIEW_CONFIDENCE,
            RECORD_SECOND_REVIEW_FOLLOWUP,
            SECOND_REVIEW,
            SUBMIT,
            LegacyProjectOperationProvider,
            ProjectOperationPolicy,
        )
        from django.conf import settings

        run = getattr(settings, 'LS84_ACCEPTANCE_RUN', {})
        if seed_submission_allowed(project, run, test_environment=settings.TEST_ENVIRONMENT):
            return ProjectOperationPolicy(VERSION, 'active', frozenset({SUBMIT}))
        scopes = run.get('scopes', [])
        if run.get('phase') != 'ready' or not any(
            policy_matches(project, s, test_environment=settings.TEST_ENVIRONMENT) for s in scopes
        ):
            return ProjectOperationPolicy(VERSION, 'paused', frozenset(), 'SYNTHETIC_SCOPE_ONLY')
        base = LegacyProjectOperationProvider().evaluate(project)
        return ProjectOperationPolicy(VERSION, 'active', base.allowed_operations | {
            EXPORT_REVIEW_AUDIT, ORGANIZE_SECOND_REVIEW, SECOND_REVIEW,
            RECORD_SECOND_REVIEW_FOLLOWUP, PROVIDE_REVIEW_CONFIDENCE,
        })
