"""Reuse the guarded fresh synthetic fixture for review/arbitration handoff."""
from .seed_review_priority_e2e import Command as PriorityFixture


class Command(PriorityFixture):
    domain = 'review84-handoff.example.test'
    prefix = 'review84-handoff'
    fixture_schema = 'review-handoff-e2e/v1'
    default_output = 'web/apps/labelstudio-e2e/.review-handoff-e2e.json'
