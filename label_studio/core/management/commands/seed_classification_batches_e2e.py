"""Separate users/projects for durable classification queue browser acceptance."""

from .seed_classification_e2e import Command as ClassificationFixture


class Command(ClassificationFixture):
    domain = "classification81-batches.example.test"
    prefix = "class81-batches"
    fixture_schema = "classification-batches-e2e/v1"
    default_output = "web/apps/labelstudio-e2e/.classification-batches81-e2e.json"
    extra_cases = ("unsubmitted", "self")
