"""Wrong-but-valid batch types must stop before field-specific metadata access."""
from unittest.mock import patch

from classification_disagreements import field_api, fields
from classification_disagreements.tests import test_arbitration as classification
from django.test import override_settings
from rest_framework.test import APITestCase

from .handoff_fixtures import HandoffFixtures
from .test_ner_handoff import NerHandoffFixtures


class OtherBatchContext:
    def assert_field_context_ineligible(self):
        mode = {f'{self.org.pk}:{self.project.pk}': 'fork_review'}
        with self.settings(
            ANNOTATION_FIELD_DISAGREEMENTS_ENABLED=True,
            ANNOTATION_FIELD_BATCHES_ENABLED=True,
            ANNOTATION_FIELD_REVIEW_PRIORITY_ENABLED=True,
            ANNOTATION_FIELD_ARBITRATION_ENABLED=True,
            ANNOTATION_FIELD_REVIEW_ARBITRATION_HANDOFF_ENABLED=True,
            ANNOTATION_FIELD_DISAGREEMENT_MODES=mode,
            ANNOTATION_FIELD_ARBITRATION_MODES=mode,
        ), patch.object(field_api, 'load', side_effect=AssertionError('No wrong-type snapshots')), patch.object(
            fields, 'normalize', side_effect=AssertionError('No wrong-type calculations')
        ):
            response = self.client.get(
                f'/api/projects/{self.project.pk}/review-batches/{self.batch.pk}/sources/{self.first.pk}/field-arbitration-context/'
            )
        assert response.status_code == 200, response.data
        assert response.data['eligible'] is False
        assert response.data['processing_cost']['hydration_passes'] == 0
        assert response.data['processing_cost']['calculation_passes'] == 0


@override_settings(
    ANNOTATION_REVIEW_BATCHES_ENABLED=True,
    ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=True,
    ANNOTATION_CLASSIFICATION_ARBITRATION_ENABLED=True,
)
class TestClassificationFieldContext(OtherBatchContext, HandoffFixtures, APITestCase):
    setUpTestData = classmethod(classification.TestArbitration.setUpTestData.__func__)

    def test_classification_context_is_ineligible_without_field_plan_keys(self):
        self.assert_field_context_ineligible()


@override_settings(
    ANNOTATION_REVIEW_BATCHES_ENABLED=True,
    ANNOTATION_NER_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_NER_BATCHES_ENABLED=True,
    ANNOTATION_NER_REVIEW_PRIORITY_ENABLED=True,
    ANNOTATION_NER_ARBITRATION_ENABLED=True,
)
class TestNerFieldContext(OtherBatchContext, NerHandoffFixtures, APITestCase):
    def test_ner_context_is_ineligible_without_field_plan_keys(self):
        self.assert_field_context_ineligible()
