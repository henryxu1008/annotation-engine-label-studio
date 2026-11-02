"""Only synthetic static Text/Choices, with two independent reviewers."""
from uuid import uuid4

from access_control.project_operations import (
    EDIT,
    ORGANIZE_REVIEW,
    ORGANIZE_SECOND_REVIEW,
    PROVIDE_REVIEW_CONFIDENCE,
    READ_WORK,
    REVIEW,
    SECOND_REVIEW,
    SUBMIT,
)
from projects.models import ProjectMember
from tasks.submissions import review_submission
from users.tests.factories import UserFactory

from .confidence_fixtures import INSTANCE, ConfidenceFixtures

OPERATIONS = [
    READ_WORK,
    EDIT,
    ORGANIZE_REVIEW,
    REVIEW,
    SUBMIT,
    PROVIDE_REVIEW_CONFIDENCE,
    ORGANIZE_SECOND_REVIEW,
    SECOND_REVIEW,
]


class SecondReviewFixtures(ConfidenceFixtures):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.supervisor = UserFactory(active_organization=cls.org, first_name='Independent', last_name='复核员')
        cls.org.add_user(cls.supervisor)
        ProjectMember.objects.create(project=cls.project, user=cls.supervisor, role='reviewer')

    def second_setup(self):
        self.confidence_setup()
        mode = self.settings(
            ANNOTATION_SECOND_REVIEW_ENABLED=True,
            ANNOTATION_SECOND_REVIEW_INSTANCE_ID=INSTANCE,
            ANNOTATION_SECOND_REVIEW_MODES={f'{self.org.pk}:{self.project.pk}': 'fork_review'},
            CONFIDENCE_TEST_OPERATIONS=OPERATIONS,
        )
        mode.enable()
        self.addCleanup(mode.disable)
        self.second_base = f'/api/projects/{self.project.pk}/second-reviews/'

    def prepared(self, *, decision='approved', reason='Initial 初审', confidence=False):
        source = self.make_source()
        if confidence:
            assert self.publish(source).status_code == 201
            args = {**self.args, 'rate': 100}
        else:
            args = {k: v for k, v in self.args.items() if k != 'priority'}
            args['rate'] = 100
        result = self.preview(args)
        assert result.status_code == 200, result.data
        result = self.create_batch({**args, 'expected_plan_hash': result.data['plan_hash']})
        assert result.status_code == 201, result.data
        batch_id = result.data['id']
        review_submission(submission=source, reviewer=self.reviewer, decision=decision, reason=reason)
        source.refresh_from_db()
        return source, batch_id

    def context(self, source, batch_id):
        self.client.force_authenticate(self.manager)
        return self.client.get(self.base + f'{batch_id}/sources/{source.pk}/second-review-context/')

    def dispatch_body(self, source, batch_id):
        result = self.context(source, batch_id)
        assert result.status_code == 200, result.data
        return dict(
            batch_id=batch_id,
            submission_id=source.pk,
            expected_plan_hash=result.data['plan_hash'],
            expected_source_hash=result.data['source_hash'],
            assignee_id=self.supervisor.pk,
            expected_viewer_hash=result.data['viewer_hash'],
        )

    def dispatch(self, source, batch_id, body=None, key=None):
        body = body or self.dispatch_body(source, batch_id)
        self.client.force_authenticate(self.manager)
        result = self.client.post(self.second_base, body, format='json', HTTP_IDEMPOTENCY_KEY=str(key or uuid4()))
        return result, body

    def opinion_body(self, task_id, outcome='confirmed', reason=''):
        self.client.force_authenticate(self.supervisor)
        result = self.client.get(self.second_base + f'{task_id}/source/')
        assert result.status_code == 200, result.data
        return dict(
            expected_version=result.data['task']['version'],
            expected_source_hash=result.data['task']['source_hash'],
            outcome=outcome,
            reason=reason,
            expected_viewer_hash=result.data['viewer_hash'],
        )

    def decide(self, task_id, body=None, key=None):
        body = body or self.opinion_body(task_id)
        self.client.force_authenticate(self.supervisor)
        return self.client.post(
            self.second_base + f'{task_id}/decision/', body, format='json', HTTP_IDEMPOTENCY_KEY=str(key or uuid4())
        )
