"""Real1000-root materialization within all other byte/graph/SQL limits."""
from data_export import audit_contract as c
from data_export.audit_models import AuditPackage
from rest_framework.test import APITestCase
from tasks.models import Annotation, Submission, Task, TaskAssignment
from tasks.submissions import _canonical_snapshot, _snapshot_hash

from .test_review_audit import AuditFixtures


class TestAuditScale(AuditFixtures, APITestCase):
    def setUp(self):
        self.audit_setup()

    def test_H03_H08_real1000_root_batch_and_1001_producer_refusal(self):
        self.project.label_config = (
            '<View><Text name="t" value="$x"/><Choices name="c" toName="t"><Choice value="a"/></Choices></View>'
        )
        self.project.save(update_fields=['label_config'])
        tasks = Task.objects.bulk_create([Task(project=self.project, data={'x': ''}) for _ in range(1000)])
        assignments = TaskAssignment.objects.bulk_create(
            [
                TaskAssignment(project=self.project, task=task, assignee=self.worker, assigned_by=self.manager)
                for task in tasks
            ]
        )
        annotations = Annotation.objects.bulk_create(
            [
                Annotation(
                    project=self.project,
                    task=task,
                    completed_by=self.worker,
                    lead_time=0,
                    result=[dict(from_name='c', to_name='t', type='choices', value={'choices': ['a']})],
                )
                for task in tasks
            ]
        )
        rows = []
        for assignment, annotation in zip(assignments, annotations):
            snapshot = _canonical_snapshot(annotation, assignment)
            rows.append(
                Submission(
                    assignment=assignment,
                    annotation=annotation,
                    submitted_by=self.worker,
                    revision=1,
                    result_snapshot=snapshot,
                    result_hash=_snapshot_hash(snapshot),
                )
            )
        assert sum(len(c.exact(row.result_snapshot)) for row in rows) <= 512 * 1024
        Submission.objects.bulk_create(rows)
        args = {k: v for k, v in self.args.items() if k != 'priority'}
        args['rate'] = 100
        preview = self.preview(args)
        assert preview.status_code == 200, preview.data
        saved = self.create_batch({**args, 'expected_plan_hash': preview.data['plan_hash']})
        assert saved.status_code == 201, saved.data
        response = self.capture(saved.data['id'])
        assert response.status_code == 202, response.data
        package = AuditPackage.objects.get()
        summary = c.strict(package.summary_record)
        assert summary['counts']['candidate'] == summary['counts']['revision'] == 1000
        extra = tasks[0]
        assignment = TaskAssignment.objects.create(
            project=self.project, task=extra, assignee=self.second_worker, assigned_by=self.manager
        )
        snapshot = _canonical_snapshot(annotations[0], assignment)
        Submission.objects.create(
            assignment=assignment,
            annotation=annotations[0],
            submitted_by=self.second_worker,
            revision=1,
            result_snapshot=snapshot,
            result_hash=_snapshot_hash(snapshot),
        )
        refused = self.preview(args)
        assert refused.status_code == 422, refused.data
        assert AuditPackage.objects.count() == 1
