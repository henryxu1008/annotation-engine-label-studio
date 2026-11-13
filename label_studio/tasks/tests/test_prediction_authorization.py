from unittest.mock import PropertyMock, patch

from organizations.models import OrganizationMember
from projects.models import ProjectMember
from rest_framework.test import APITestCase
from tasks.models import Prediction, Task, TaskAssignment
from tasks.tests import test_submission_review_workflow as fixtures
from tasks.tests.factories import PredictionFactory, TaskFactory
from users.tests.factories import UserFactory


class TestPredictionAuthorization(APITestCase):
    setUpTestData = classmethod(fixtures.TestSubmissionReviewWorkflow.setUpTestData.__func__)
    setUp = fixtures.TestSubmissionReviewWorkflow.setUp

    def test_list_detail_write_and_revocation_follow_task_assignment(self):
        allowed = PredictionFactory(task=self.task, result=[])
        other_task = TaskFactory(project=self.project)
        hidden = PredictionFactory(task=other_task, result=[])
        outsider = UserFactory(active_organization=self.organization)
        self.organization.add_user(outsider)

        self.client.force_authenticate(self.annotator)
        response = self.client.get('/api/predictions/')
        assert response.status_code == 200
        data = response.json()
        rows = data['results'] if isinstance(data, dict) else data
        assert {row['id'] for row in rows} == {allowed.pk}
        assert self.client.get(f'/api/predictions/{allowed.pk}/').status_code == 200
        assert self.client.get(f'/api/predictions/{hidden.pk}/').status_code == 404
        assert allowed.has_permission(self.annotator)
        assert not hidden.has_permission(self.annotator)
        assert self.client.patch(f'/api/predictions/{allowed.pk}/', {'result': []}, format='json').status_code == 403
        assert self.client.delete(f'/api/predictions/{allowed.pk}/').status_code == 403
        assert self.client.post('/api/predictions/', {'task': self.task.pk, 'result': []}, format='json').status_code == 403
        hidden_response = self.client.post('/api/predictions/', {'task': other_task.pk, 'result': []}, format='json')
        missing_response = self.client.post('/api/predictions/', {'task': 99999999, 'result': []}, format='json')
        assert hidden_response.status_code == missing_response.status_code == 400

        self.client.force_authenticate(outsider)
        assert self.client.get(f'/api/predictions/{allowed.pk}/').status_code == 404
        assert not allowed.has_permission(outsider)
        from access_control.project_operations import local_principal

        with patch('access_control.identity.get_identity_provider') as provider:
            provider.return_value.resolve.return_value = local_principal(self.annotator)
            assert self.client.get(f'/api/predictions/{allowed.pk}/').status_code == 200
        self.client.force_authenticate(self.reviewer)
        assert self.client.get(f'/api/predictions/{allowed.pk}/').status_code == 404

        self.client.force_authenticate(self.manager)
        assert self.client.get(f'/api/predictions/{hidden.pk}/').status_code == 200
        assert self.client.patch(f'/api/predictions/{hidden.pk}/', {'task': self.task.pk}, format='json').status_code == 400
        assert self.client.patch(f'/api/predictions/{hidden.pk}/', {'result': []}, format='json').status_code == 200
        from projects.tests.factories import ProjectFactory

        other_project = ProjectFactory(organization=self.organization, created_by=self.manager)
        Prediction.objects.filter(pk=hidden.pk).update(project=other_project)
        hidden.refresh_from_db()
        assert self.client.get(f'/api/predictions/{hidden.pk}/').status_code == 404
        assert not hidden.has_permission(self.manager)

        self.assignment.cancel()
        self.client.force_authenticate(self.annotator)
        assert self.client.get(f'/api/predictions/{allowed.pk}/').status_code == 404
        assert not allowed.has_permission(self.annotator)
        self.assignment = TaskAssignment.objects.create(task=self.task, project=self.project,
                                                        assignee=self.annotator, assigned_by=self.manager)
        ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=False)
        assert self.client.get(f'/api/predictions/{allowed.pk}/').status_code == 404
        ProjectMember.objects.filter(project=self.project, user=self.annotator).update(enabled=True)
        OrganizationMember.objects.filter(organization=self.organization, user=self.annotator).update(
            deleted_at='2026-01-01T00:00:00Z',
        )
        assert self.client.get(f'/api/predictions/{allowed.pk}/').status_code == 404
        assert not allowed.has_permission(self.annotator)

    def test_manager_cannot_cross_project_or_create_quality_prediction(self):
        from classification_disagreements.models import BlindQualityAssignment

        quality = TaskFactory(project=self.project)
        assignment = TaskAssignment.objects.create(task=quality, project=self.project,
                                                   assignee=self.annotator, assigned_by=self.manager)
        BlindQualityAssignment.objects.create(assignment_id=assignment.pk, task_id=quality.pk,
                                              project_id=self.project.pk, assignee_id=self.annotator.pk)
        from projects.tests.factories import ProjectFactory

        other_project = ProjectFactory(organization=self.organization, created_by=self.manager)
        self.client.force_authenticate(self.manager)
        assert self.client.post('/api/predictions/', {'task': quality.pk, 'result': []}, format='json').status_code == 400
        response = self.client.post('/api/predictions/', {
            'task': self.task.pk, 'project': other_project.pk, 'result': [],
        }, format='json')
        assert response.status_code == 400
        assert self.client.post('/api/predictions/', {'task': self.task.pk, 'result': []}, format='json').status_code == 201

    def test_interactive_ml_requires_current_task_access(self):
        from ml.models import MLBackend

        with patch('ml.models.WebhookSerializer') as webhook:
            webhook.return_value.is_valid.return_value = False
            backend = MLBackend.objects.create(project=self.project, url='http://127.0.0.1:1', is_interactive=True)
        hidden = TaskFactory(project=self.project)
        self.client.force_authenticate(self.annotator)
        url = f'/api/ml/{backend.pk}/interactive-annotating'
        with patch.object(MLBackend, 'interactive_annotating', return_value={'data': {}}) as predict:
            assert self.client.post(url, {'task': hidden.pk, 'context': {}}, format='json').status_code == 404
            predict.assert_not_called()
            assert self.client.post(url, {'task': self.task.pk, 'context': {}}, format='json').status_code == 200
            assert predict.call_count == 1
            self.assignment.cancel()
            assert self.client.post(url, {'task': self.task.pk, 'context': {}}, format='json').status_code == 404
            assert predict.call_count == 1
        with patch.object(MLBackend, 'train') as train:
            assert self.client.post(f'/api/ml/{backend.pk}/train').status_code == 403
            assert self.client.post(f'/api/ml/{backend.pk}/predict/test?random=true').status_code == 403
            train.assert_not_called()
            self.client.force_authenticate(self.manager)
            assert self.client.post(f'/api/ml/{backend.pk}/train').status_code == 200
            train.assert_called_once()

    def test_quality_data_is_not_sent_to_external_ml_backend(self):
        from classification_disagreements.models import BlindQualityAssignment
        from ml.api_connector import MLApi
        from ml.models import MLBackend, MLBackendState
        from rest_framework.exceptions import ValidationError

        quality = TaskFactory(project=self.project)
        assignment = TaskAssignment.objects.create(task=quality, project=self.project,
                                                   assignee=self.annotator, assigned_by=self.manager)
        BlindQualityAssignment.objects.create(assignment_id=assignment.pk, task_id=quality.pk,
                                              project_id=self.project.pk, assignee_id=self.annotator.pk)
        assert Task.get_random(project=self.project).pk == self.task.pk
        backend = MLBackend(project=self.project, url='http://127.0.0.1:1', is_interactive=True,
                            state=MLBackendState.CONNECTED)
        with patch.object(backend, 'update_state', return_value='v1') as health, patch.object(
            backend, '_get_predictions_from_ml_backend', return_value=[],
        ) as remote, patch.object(MLBackend, 'api', new_callable=PropertyMock) as api:
            assert backend.predict_tasks([quality]) is None
            health.assert_not_called()
            remote.assert_not_called()
            assert backend._predict(quality) is None
            assert backend.interactive_annotating(quality)['errors']
            api.assert_not_called()
            backend.predict_tasks([quality, self.task])
            assert [item['id'] for item in remote.call_args.args[0]] == [self.task.pk]
        connector = MLApi(url='http://127.0.0.1:1')
        with patch.object(connector, '_request') as send, self.assertRaises(ValidationError):
            connector.train(self.project)
        send.assert_not_called()

    def test_ml_setup_never_sends_owner_token_when_blind_is_enabled_or_retained(self):
        from classification_disagreements.models import BlindQualityAssignment
        from ml.api_connector import MLApi
        from rest_framework.exceptions import ValidationError

        connector = MLApi(url='http://127.0.0.1:1')
        with patch.object(connector, '_request') as send:
            with self.settings(ANNOTATION_CLASSIFICATION_BLIND_ENABLED=True), self.assertRaises(ValidationError):
                connector.setup(self.project)
            send.assert_not_called()
            BlindQualityAssignment.objects.create(assignment_id=self.assignment.pk, task_id=self.task.pk,
                                                  project_id=self.project.pk, assignee_id=self.annotator.pk)
            with self.settings(ANNOTATION_CLASSIFICATION_BLIND_ENABLED=False), self.assertRaises(ValidationError):
                connector.setup(self.project)
            send.assert_not_called()

    def test_deleted_project_tombstone_does_not_permanently_block_ml_setup(self):
        from classification_disagreements.models import BlindQualityAssignment
        from ml.api_connector import MLApi
        from projects.tests.factories import ProjectFactory
        from rest_framework.authtoken.models import Token

        other = ProjectFactory(organization=self.organization, created_by=self.manager)
        Token.objects.get_or_create(user=other.created_by)
        BlindQualityAssignment.objects.create(assignment_id=self.assignment.pk, task_id=self.task.pk,
                                              project_id=self.project.pk, assignee_id=self.annotator.pk)
        self.project.delete()
        connector = MLApi(url='http://127.0.0.1:1')
        with self.settings(ANNOTATION_CLASSIFICATION_BLIND_ENABLED=False), patch.object(connector, '_request') as send:
            connector.setup(other)
            assert send.call_count == 1
