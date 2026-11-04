"""Direct acceptance evidence for lifecycle and scope subcases, without guard bypasses."""
import copy
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from data_export import audit_contract as c
from data_export import audit_files, audit_format
from data_export import review_audit as audit
from data_export.audit_models import AuditActorBinding, AuditArtifact, AuditPackage
from django.db import DatabaseError, transaction
from django.utils import timezone
from projects.models import Project
from rest_framework.test import APITestCase
from review_batches.models import ReviewBatch
from tasks.models import Submission
from tasks.submissions import _snapshot_hash

from .test_review_audit import AuditFixtures, denied


class TestAuditMatrix(AuditFixtures, APITestCase):
    def setUp(self):
        self.audit_setup()

    def test_A07_real_ner_field_bbox_and_dynamic_choices_are_unsupported(self):
        configs = (
            '<View><Text name="text" value="$text"/><Labels name="ner" toName="text"><Label value="Person"/></Labels></View>',
            '<View><Text name="text" value="$text"/><TextArea name="field" toName="text"/></View>',
            '<View><Image name="image" value="$image"/><RectangleLabels name="bbox" toName="image"><Label value="Object"/></RectangleLabels></View>',
            '<View><Text name="text" value="$text"/><Choices name="label" toName="text" value="$options"/></View>',
        )
        for config in configs:
            source = self.make_source()
            snapshot = copy.deepcopy(source.result_snapshot)
            snapshot['project']['label_config'] = config
            snapshot['task']['data'].update(image='synthetic://image', options=['A'])
            Submission.objects.filter(pk=source.pk).update(
                result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
            )
            args = {k: v for k, v in self.args.items() if k != 'priority'}
            args['rate'] = 100
            preview = self.preview(args)
            assert preview.status_code == 200, preview.data
            saved = self.create_batch({**args, 'expected_plan_hash': preview.data['plan_hash']})
            assert saved.status_code == 201, saved.data
            response = self.client.post(
                self.audit_base + 'preview/', {'review_batch_id': saved.data['id']}, format='json'
            )
            assert (
                response.status_code == 400 and response.data['display_context']['reason'] == 'AUDIT_UNSUPPORTED_INPUT'
            )
            source.delete()
        assert not AuditPackage.objects.exists() and not AuditActorBinding.objects.exists()

    def test_C02_root_identity_fields_are_immutable_and_same_pk_recreation_changes_binding(self):
        source, batch_id = self.prepared()
        body = self.audit_body(batch_id)
        batch = ReviewBatch.objects.get(pk=batch_id)
        for changes in (
            {'actor_id_frozen': self.producer.pk},
            {'creator_principal': 'different-trusted-principal'},
            {'created_at': batch.created_at + timedelta(seconds=1)},
            {'request_hash': 'f' * 64},
            {'plan_hash': 'f' * 64},
        ):
            with self.assertRaises(DatabaseError), transaction.atomic():
                ReviewBatch.objects.filter(pk=batch_id).update(**changes)
        fields = {f.attname: getattr(batch, f.attname) for f in batch._meta.fields}
        batch.delete()
        fields.update(
            actor_id_frozen=self.producer.pk, creator_principal='different-trusted-principal', request_hash='e' * 64
        )
        ReviewBatch.objects.create(**fields)
        denied(self.capture(batch_id, body=body), (409,))
        assert not AuditPackage.objects.exists() and not AuditActorBinding.objects.exists()

    def test_D07_soft_delete_all_surfaces_then_same_pk_project_has_no_old_package(self):
        project_id = self.project.pk
        recreated = {field.attname: getattr(self.project, field.attname) for field in self.project._meta.fields}
        source, batch = self.prepared()
        body = self.audit_body(batch)
        assert self.capture(batch, body=body).status_code == 202
        package = self.ready()
        paths = (
            self.audit_base,
            self.audit_base + 'capabilities/',
            self.audit_base + f'{package.pk}/',
            self.audit_base + f'{package.pk}/manifest/',
            self.audit_base + f'{package.pk}/artifacts/{package.artifact.pk}/download/',
        )
        Project.all_objects.filter(pk=self.project.pk).update(deleted_at=timezone.now())
        for method in ('get', 'head', 'options'):
            for path in paths:
                denied(getattr(self.client, method)(path), (403, 404))
        for path, payload in (
            (self.audit_base, body),
            (self.audit_base + 'preview/', {'review_batch_id': batch}),
            (self.audit_base + f'{package.pk}/retry/', {}),
        ):
            denied(self.client.post(path, payload, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())), (403, 404))
        Project.all_objects.filter(pk=self.project.pk).update(deleted_at=None)
        self.project.delete()
        recreated['title'] = 'Recreated project with the original numeric ID'
        Project.all_objects.bulk_create([Project(**recreated)])
        assert Project.all_objects.filter(pk=project_id).exists()
        assert self.client.get(self.audit_base + f'{package.pk}/').status_code == 404
        assert not AuditPackage.objects.exists() and AuditActorBinding.objects.count() == 1

    def test_E07_publish_runtime_drift_blocks_and_new_token_repeats_exact_zip_bytes(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        with patch.object(audit.approved, 'api_queue', return_value=MagicMock()):
            self.real_enqueue(str(package.pk))
        package.refresh_from_db()
        first_token = package.token
        formatter = c.strict(package.input_bundle)['formatter']
        produced = []
        original = audit_format.write

        def write(*args, **kwargs):
            result = original(*args, **kwargs)
            output = args[2]
            output.flush()
            produced.append(Path(output.name).read_bytes())
            return result

        with patch.object(
            audit.c, 'formatter', side_effect=[formatter, formatter, {'deployment': 'changed-before-publish'}]
        ), patch.object(audit_format, 'write', side_effect=write):
            audit.generate(str(package.pk), str(first_token))
        package.refresh_from_db()
        assert package.state == 'blocked_runtime' and package.error_code == 'AUDIT_RUNTIME_CHANGED'
        assert len(produced) == 1 and not AuditArtifact.objects.exists()
        response = self.client.post(
            self.audit_base + f'{package.pk}/retry/', {}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
        )
        assert response.status_code == 200, response.data
        with patch.object(audit.audit_source, 'load', side_effect=AssertionError('No new source after T')):
            package = self.ready(package)
        assert package.token != first_token
        assert audit_files.artifact_path(package, package.artifact).read_bytes() == produced[0]
