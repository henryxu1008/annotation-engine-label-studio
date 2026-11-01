"""Synthetic confidence issuers; no external model, retained data or service."""
from uuid import uuid4

from access_control.project_operations import (
    ORGANIZE_REVIEW,
    PROVIDE_REVIEW_CONFIDENCE,
    READ_WORK,
    REVIEW,
    SUBMIT,
    ProjectOperationPolicy,
)
from django.conf import settings
from organizations.tests.factories import OrganizationFactory
from projects.models import ProjectMember
from projects.tests.factories import ProjectFactory
from review_batches import confidence_contract as c
from tasks.models import Annotation, Submission, TaskAssignment
from tasks.submissions import _canonical_snapshot, _snapshot_hash
from tasks.tests.factories import TaskFactory
from users.tests.factories import UserFactory

XML = '<View><Text name="text" value="$text"/><Choices name="label" toName="text"><Choice value="A"/><Choice value="B"/></Choices></View>'
INSTANCE = '10000000-0000-4000-8000-000000000001'
NAMESPACE = '20000000-0000-4000-8000-000000000002'
PROFILE = '30000000-0000-4000-8000-000000000003'
POLICY = 'review_batches.tests.confidence_fixtures.ConfidencePolicy'


class ConfidencePolicy:
    def evaluate(self, project):
        return ProjectOperationPolicy(
            'confidence-synthetic/v1',
            'active',
            frozenset(
                getattr(
                    settings,
                    'CONFIDENCE_TEST_OPERATIONS',
                    [READ_WORK, ORGANIZE_REVIEW, PROVIDE_REVIEW_CONFIDENCE, REVIEW, SUBMIT],
                )
            ),
        )


class ConfidenceFixtures:
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.manager = cls.org.created_by
        cls.project = ProjectFactory(organization=cls.org, created_by=cls.manager, label_config=XML)
        for name, role in (
            ('producer', 'manager'),
            ('worker', 'annotator'),
            ('second_worker', 'annotator'),
            ('reviewer', 'reviewer'),
        ):
            user = UserFactory(active_organization=cls.org)
            cls.org.add_user(user)
            ProjectMember.objects.create(project=cls.project, user=user, role=role)
            setattr(cls, name, user)

    def confidence_setup(self):
        self.descriptor = {
            'schema': c.PROFILE,
            'profile_id': PROFILE,
            'producer_namespace': NAMESPACE,
            'label': 'Revision confidence 修订置信',
            'version': 'synthetic-v1',
            'evaluator_id': 'offline-evaluator',
            'evaluator_version': '1',
            'model_id': 'synthetic-no-network',
            'model_artifact_sha256': 'a' * 64,
            'score_definition': c.SCORE_DEFINITION,
            'calibration_version': 'synthetic-calibration/v1',
            'integer_rule': c.INTEGER_RULE,
            'input_contract': c.INPUT_CONTRACT,
        }
        self.profile_digest = c.digest(self.descriptor)
        self.entries = [
            {
                'organization_id': self.org.pk,
                'project_id': self.project.pk,
                'descriptor': self.descriptor,
                'digest': self.profile_digest,
                'fresh': True,
            }
        ]
        self.grants = [
            {
                'organization_id': self.org.pk,
                'project_id': self.project.pk,
                'principal_id': f'label_studio_local:{self.producer.pk}',
                'source': 'label_studio_local',
                'actor_id': self.producer.pk,
                'producer_namespace': NAMESPACE,
                'profile_ids': [PROFILE],
            }
        ]
        mode = self.settings(
            ANNOTATION_REVIEW_BATCHES_ENABLED=True,
            ANNOTATION_REVIEW_CONFIDENCE_ENABLED=True,
            ANNOTATION_REVIEW_BATCH_MODES={f'{self.org.pk}:{self.project.pk}': 'fork_review'},
            ANNOTATION_REVIEW_CONFIDENCE_INSTANCE_ID=INSTANCE,
            ANNOTATION_REVIEW_CONFIDENCE_PROFILES=self.entries,
            ANNOTATION_REVIEW_CONFIDENCE_GRANTS=self.grants,
            ANNOTATION_PROJECT_OPERATION_PROVIDER=POLICY,
        )
        mode.enable()
        self.addCleanup(mode.disable)
        self.base = f'/api/projects/{self.project.pk}/review-batches/'
        self.input_base = f'/api/projects/{self.project.pk}/review-confidence-inputs/'
        self.fact_base = f'/api/projects/{self.project.pk}/review-confidence-observations/'
        self.args = {
            'name': 'Confidence 置信',
            'rate': 50,
            'seed': 'fixed',
            'priority': {
                'kind': c.KIND,
                'profile_id': PROFILE,
                'profile_digest': self.profile_digest,
                'threshold_units': 400000,
            },
        }
        self.client.force_authenticate(self.manager)

    def make_source(self, worker=None, *, text='Frozen text 冻结文本', revision=1, assignment=None):
        worker = worker or self.worker
        if assignment is None:
            task = TaskFactory(project=self.project, data={'text': text})
            assignment = TaskAssignment.objects.create(
                project=self.project, task=task, assignee=worker, assigned_by=self.manager
            )
        annotation = Annotation.objects.create(
            project=self.project,
            task=assignment.task,
            completed_by=worker,
            result=[{'from_name': 'label', 'to_name': 'text', 'type': 'choices', 'value': {'choices': ['A']}}],
        )
        assignment.annotation = annotation
        assignment.save()
        snapshot = _canonical_snapshot(annotation, assignment)
        return Submission.objects.create(
            assignment=assignment,
            annotation=annotation,
            submitted_by=worker,
            revision=revision,
            result_snapshot=snapshot,
            result_hash=_snapshot_hash(snapshot),
        )

    def source_input(self, row, actor=None):
        self.client.force_authenticate(actor or self.producer)
        return self.client.get(
            self.input_base + f'{row.pk}/', {'profile_id': PROFILE, 'profile_digest': self.profile_digest}
        )

    def fact_body(self, row, score=100000):
        result = self.source_input(row)
        assert result.status_code == 200, result.data
        return {
            'submission_id': row.pk,
            'profile_id': PROFILE,
            'profile_digest': self.profile_digest,
            'evaluated_input_hash': result.data['input_hash'],
            'score_units': score,
            'evaluated_at': '2026-11-01T00:00:00Z',
        }

    def publish(self, row=None, score=100000, *, body=None, key=None, actor=None):
        body = body or self.fact_body(row, score)
        self.client.force_authenticate(actor or self.producer)
        return self.client.post(self.fact_base, body, format='json', HTTP_IDEMPOTENCY_KEY=str(key or uuid4()))

    def preview(self, args=None, actor=None):
        self.client.force_authenticate(actor or self.manager)
        return self.client.post(self.base + 'preview/', args or self.args, format='json')

    def create_batch(self, body=None, *, key=None, actor=None):
        if body is None:
            result = self.preview()
            assert result.status_code == 200, result.data
            body = {**self.args, 'expected_plan_hash': result.data['plan_hash']}
        self.client.force_authenticate(actor or self.manager)
        return self.client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=str(key or uuid4()))
