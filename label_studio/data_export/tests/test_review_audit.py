"""Synthetic audit acceptance: frozen evidence never acquires review authority."""
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from data_export import review_audit as audit
from data_export.audit_models import AuditActorBinding, AuditPackage
from django.conf import settings
from django.db import DatabaseError, transaction
from projects.models import ProjectMember
from rest_framework.test import APITestCase
from review_batches.models import ReviewBatch, SecondReviewTask
from review_batches.tests.second_review_fixtures import OPERATIONS
from review_batches.tests.test_concern_followup import ConcernFixtures
from tasks.models import ReviewDecision, Submission
from tasks.submissions import create_submission


def denied(response, statuses=(400, 403, 404, 409, 422, 429, 503)):
    assert response.status_code in statuses, getattr(response, 'data', response.content[:500])


class AuditFixtures(ConcernFixtures):
    def audit_setup(self, *, initialize_sources=True):
        if initialize_sources:
            self.second_setup()
        private_parent = Path(settings.BASE_DATA_DIR) / 'audit-test-private'
        created = not private_parent.exists()
        private_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created:
            from data_export.audit_files import secure_new_directory

            secure_new_directory(private_parent)
        self.private = tempfile.TemporaryDirectory(dir=private_parent)
        from data_export.audit_files import secure_new_directory

        secure_new_directory(Path(self.private.name))
        self.addCleanup(self.private.cleanup)
        self.audit_policy = dict(
            instance_id='40000000-0000-4000-8000-000000000004',
            projects={f'{self.org.pk}:{self.project.pk}': {'mode': 'fork_review', 'version': 'synthetic-v1'}},
        )
        mode = self.settings(
            ANNOTATION_REVIEW_AUDIT_ENABLED=True,
            ANNOTATION_REVIEW_AUDIT_POLICY=self.audit_policy,
            ANNOTATION_SECOND_REVIEW_FOLLOWUP_ENABLED=True,
            APPROVED_EXPORT_ROOT=str(Path(self.private.name) / 'private'),
            ANNOTATION_PROJECT_OPERATION_PROVIDER='review_batches.tests.confidence_fixtures.ConfidencePolicy',
            CONFIDENCE_TEST_OPERATIONS=[
                *OPERATIONS,
                'record_second_review_followup',
                'export_review_audit',
                'organize_classification',
                'organize_arbitration',
                'arbitrate',
                'security_cancel',
            ],
        )
        mode.enable()
        self.addCleanup(mode.disable)
        self.audit_base = f'/api/projects/{self.project.pk}/review-audit-packages/'
        self.followup_base = f'/api/projects/{self.project.pk}/second-review-followups/'
        self.queue = patch.object(audit, 'queue_available', return_value=True)
        self.queue.start()
        self.addCleanup(self.queue.stop)
        self.real_enqueue = audit.enqueue
        enqueue = patch.object(audit, 'enqueue', return_value=None)
        enqueue.start()
        self.addCleanup(enqueue.stop)
        self.client.force_authenticate(self.manager)

    def audit_body(self, batch):
        self.client.force_authenticate(self.manager)
        response = self.client.post(self.audit_base + 'preview/', {'review_batch_id': batch}, format='json')
        assert response.status_code == 200, getattr(response, 'data', response.content[:500])
        return dict(
            review_batch_id=batch,
            expected_batch_binding_hash=response.data['batch_binding_hash'],
            expected_capture_preview_hash=response.data['capture_preview_hash'],
            profile=audit.PROFILE,
        )

    def capture(self, batch, *, body=None, key=None):
        self.client.force_authenticate(self.manager)
        return self.client.post(
            self.audit_base, body or self.audit_body(batch), format='json', HTTP_IDEMPOTENCY_KEY=str(key or uuid4())
        )

    def ready(self, package=None):
        from unittest.mock import MagicMock

        package = package or AuditPackage.objects.get()
        queue = MagicMock()
        with patch.object(audit.approved, 'api_queue', return_value=queue):
            self.real_enqueue(str(package.pk))
        package.refresh_from_db()
        assert package.state == 'queued'
        audit.generate(str(package.pk), str(package.token))
        package.refresh_from_db()
        assert package.state == 'ready', package.error_code
        return package


class TestReviewAudit(AuditFixtures, APITestCase):
    def setUp(self):
        self.audit_setup()

    def test_A01_complete_frozen_static_batch_no_business_writes(self):
        source, batch = self.prepared()
        before = list(Submission.objects.values()), list(ReviewDecision.objects.values())
        response = self.capture(batch)
        assert response.status_code == 202, response.data
        package = AuditPackage.objects.get()
        assert package.state == 'dispatch_pending'
        assert package.input_sha256 and package.projection_sha256
        assert (list(Submission.objects.values()), list(ReviewDecision.objects.values())) == before
        assert json.loads(package.projection)['effect'] == 'supervisory_only'

    def test_A02_selected_and_unselected_each_have_all_four_source_states(self):
        from tasks.submissions import review_submission

        sources = [self.make_source() for _ in range(8)]
        args = {key: value for key, value in self.args.items() if key != 'priority'}
        args['rate'] = 50
        preview = self.preview(args)
        batch = self.create_batch({**args, 'expected_plan_hash': preview.data['plan_hash']}).data['id']
        plan = ReviewBatch.objects.get(pk=batch).plan
        by_id = {row.pk: row for row in sources}
        expected = set()
        for selected in (True, False):
            candidates = [item for item in plan['candidates'] if item['selected'] is selected]
            assert len(candidates) == 4
            for item, state in zip(candidates, ('pending', 'approved', 'rejected', 'superseded')):
                row = by_id[item['submission_id']]
                if state in ('approved', 'rejected'):
                    review_submission(submission=row, reviewer=self.reviewer, decision=state, reason='Synthetic state')
                elif state == 'superseded':
                    create_submission(annotation=row.annotation, assignment=row.assignment, actor=self.worker)
                expected.add((str(row.pk), selected, state))
        before = list(Submission.objects.values()), list(ReviewDecision.objects.values())
        assert self.capture(batch).status_code == 202
        facts = json.loads(AuditPackage.objects.get().input_bundle)['graph']['facts']
        frozen = {(row['type'], row['key']): row for row in facts}
        observed = {
            (
                identifier,
                frozen['candidate', identifier]['public']['selected'],
                frozen['revision', identifier]['public']['state'],
            )
            for identifier, _, _ in expected
        }
        assert observed == expected and len({(selected, state) for _, selected, state in observed}) == 8
        assert (list(Submission.objects.values()), list(ReviewDecision.objects.values())) == before

    def test_B02_no_legacy_audit_operation(self):
        source, batch = self.prepared()
        with self.settings(CONFIDENCE_TEST_OPERATIONS=OPERATIONS):
            response = self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json')
        assert response.status_code == 403
        assert not AuditPackage.objects.exists() and not AuditActorBinding.objects.exists()

    def test_A03_all_root_lineage_and_adjacent_differences(self):
        source, batch = self.prepared(decision='rejected')
        later = create_submission(annotation=source.annotation, assignment=source.assignment, actor=self.worker)
        response = self.capture(batch)
        assert response.status_code == 202, response.data
        records = json.loads(AuditPackage.objects.get().projection)['records']
        assert {r['revision'] for r in records if r['type'] == 'revision'} == {source.revision, later.revision}
        assert sum(r['type'] == 'difference' for r in records) == 1

    def test_A04_initial_review_has_no_batch_execution_claim(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        initial = next(
            r for r in json.loads(AuditPackage.objects.get().projection)['records'] if r['type'] == 'initial_review'
        )
        assert initial['association'] == 'subject_observed' and initial['batch_execution_provenance'] == 'not_recorded'
        assert initial['actor_incarnation'] == 'not_recorded' and len(initial['actor_ref']) == 64

    def test_A05_second_concern_and_followup_closed_graph(self):
        source, batch, task = self.concerned()
        assert self.record_followup(self.followup_body(task)).status_code == 201
        assert self.capture(batch).status_code == 202
        types = {r['type'] for r in json.loads(AuditPackage.objects.get().projection)['records']}
        assert {'second_review', 'second_opinion', 'concern_followup'} <= types

    def test_A05_pending_confirmed_and_cancelled_are_distinct_frozen_records(self):
        for state in ('pending', 'confirmed', 'cancelled'):
            source, batch = self.prepared()
            response, body = self.dispatch(source, batch)
            assert response.status_code == 201, response.data
            task = response.data['receipt']['task_id']
            if state == 'confirmed':
                response = self.decide(task, self.opinion_body(task, 'confirmed'))
                assert response.status_code == 201, response.data
            elif state == 'cancelled':
                self.client.force_authenticate(self.manager)
                response = self.client.post(
                    self.second_base + f'{task}/cancel/',
                    dict(expected_version=1, reason='Stop 错派', expected_viewer_hash=body['expected_viewer_hash']),
                    format='json',
                    HTTP_IDEMPOTENCY_KEY=str(uuid4()),
                )
                assert response.status_code == 201, response.data
            before = list(Submission.objects.values()), list(ReviewDecision.objects.values())
            captured = self.capture(batch)
            assert captured.status_code == 202, captured.data
            package = AuditPackage.objects.get(pk=captured.data['package_id'])
            records = json.loads(package.projection)['records']
            review = next(row for row in records if row['type'] == 'second_review')
            assert review['state'] == ('completed' if state == 'confirmed' else state)
            assert review['effect'] == 'supervisory_only'
            opinions = [row for row in records if row['type'] == 'second_opinion']
            cancellations = [row for row in records if row['type'] == 'cancellation']
            assert len(opinions) == (state == 'confirmed')
            assert len(cancellations) == (state == 'cancelled')
            if opinions:
                assert opinions[0]['outcome'] == 'confirmed' and opinions[0]['reason_state'] == 'absent'
            if cancellations:
                assert cancellations[0]['reason_state'] == 'present_redacted'
            assert (list(Submission.objects.values()), list(ReviewDecision.objects.values())) == before
            self.ready(package)

    def test_A07_unknown_input_never_falls_back_to_generic_export(self):
        source, batch = self.prepared()
        with patch('data_export.audit_source.types.supported', return_value=False):
            denied(self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (400,))
        assert not AuditPackage.objects.exists()

    def test_B01_operation_is_independent_of_read_work(self):
        source, batch = self.prepared()
        with self.settings(CONFIDENCE_TEST_OPERATIONS=['export_review_audit']):
            denied(self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (403,))

    def test_B03_cross_project_and_package_identifiers(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        from projects.tests.factories import ProjectFactory

        other = ProjectFactory(organization=self.org, created_by=self.manager)
        with self.settings(
            ANNOTATION_REVIEW_AUDIT_POLICY={
                **self.audit_policy,
                'projects': {
                    **self.audit_policy['projects'],
                    f'{self.org.pk}:{other.pk}': dict(mode='fork_review', version='synthetic-v1'),
                },
            }
        ):
            denied(self.client.get(f'/api/projects/{other.pk}/review-audit-packages/{package.pk}/'), (404,))
        denied(self.client.get(self.audit_base + f'{uuid4()}/'), (404,))

    def test_B04_request_cannot_supply_actor_or_effect(self):
        source, batch = self.prepared()
        body = self.audit_body(batch)
        for key, value in [
            ('actor_id', self.manager.pk),
            ('effect', 'approved'),
            ('captured_at', '2000-01-01'),
            ('mode', 'core_only_internal'),
        ]:
            denied(self.capture(batch, body={**body, key: value}), (400,))
        assert not AuditPackage.objects.exists()

    def test_B05_revocation_precedes_metadata_and_conditional_download(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = self.ready()
        ProjectMember.objects.filter(project=self.project, user=self.manager).update(enabled=False)
        # Owner status itself remains a Manager; remove active org membership too.
        from organizations.models import OrganizationMember

        OrganizationMember.objects.filter(user=self.manager, organization=self.org).update(
            deleted_at=__import__('django.utils.timezone', fromlist=['now']).now()
        )
        for method in ('get', 'head', 'options'):
            for path in (
                self.audit_base,
                self.audit_base + f'{package.pk}/',
                self.audit_base + f'{package.pk}/manifest/',
                self.audit_base + f'{package.pk}/artifacts/{package.artifact.pk}/download/',
            ):
                denied(getattr(self.client, method)(path, HTTP_RANGE='bytes=0-2', HTTP_IF_NONE_MATCH='*'), (403, 404))

    def test_B07_retired_source_features_do_not_reopen_sources(self):
        source, batch = self.prepared(confidence=True)
        assert self.capture(batch).status_code == 202
        package = self.ready()
        with self.settings(
            ANNOTATION_REVIEW_BATCHES_ENABLED=False,
            ANNOTATION_REVIEW_CONFIDENCE_ENABLED=False,
            ANNOTATION_REVIEW_CONFIDENCE_PROFILES=[],
            ANNOTATION_REVIEW_CONFIDENCE_GRANTS=[],
        ), patch.object(audit.audit_source, 'load', side_effect=AssertionError('no source lookup')):
            assert self.client.get(self.audit_base + f'{package.pk}/').status_code == 200

    def test_B08_default_core_unknown_and_background_provider_fail_closed(self):
        source, batch = self.prepared()
        with self.settings(ANNOTATION_REVIEW_AUDIT_ENABLED=False):
            denied(self.client.get(self.audit_base + 'capabilities/'), (403,))
        for mode in ('core_only_internal', 'fork_approved', 'unknown'):
            policy = {
                **self.audit_policy,
                'projects': {f'{self.org.pk}:{self.project.pk}': dict(mode=mode, version='v1')},
            }
            with self.settings(ANNOTATION_REVIEW_AUDIT_POLICY=policy):
                denied(
                    self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (403,)
                )

    def test_C01_preview_drift_rolls_back_package_and_actor_binding(self):
        source, batch = self.prepared(decision='rejected')
        body = self.audit_body(batch)
        create_submission(annotation=source.annotation, assignment=source.assignment, actor=self.worker)
        denied(self.capture(batch, body=body), (409,))
        assert not AuditPackage.objects.exists() and not AuditActorBinding.objects.exists()

    def test_C04_frozen_first_review_conflict_is_corruption(self):
        source, batch = self.prepared()
        assert self.dispatch(source, batch)[0].status_code == 201
        ReviewDecision.objects.filter(submission=source).update(reason='changed after frozen proof')
        denied(self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (409,))

    def test_C05_two_pass_membership_change_rolls_back(self):
        source, batch = self.prepared()
        original = audit.audit_source.load
        calls = []

        def drifting(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(1)
            if len(calls) == 2:
                result[1].facts[next(iter(result[1].facts))]['public']['candidate_count'] = 999
            return result

        with patch.object(audit.audit_source, 'load', side_effect=drifting):
            denied(self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (409,))
        assert not AuditPackage.objects.exists()

    def test_C06_worker_formats_only_frozen_projection(self):
        source, batch = self.prepared(decision='rejected')
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        frozen = package.projection
        create_submission(annotation=source.annotation, assignment=source.assignment, actor=self.worker)
        with patch.object(audit.audit_source, 'load', side_effect=AssertionError('no source read')):
            package = self.ready(package)
        assert package.projection == frozen

    def test_D01_missing_root_never_guesses_another_batch(self):
        denied(self.client.post(self.audit_base + 'preview/', dict(review_batch_id=999999), format='json'), (404,))

    def test_D02_retained_typed_source_after_submission_deleted(self):
        source, batch = self.prepared()
        assert self.dispatch(source, batch)[0].status_code == 201
        self.successor(source)
        source.delete()
        assert self.capture(batch).status_code == 202
        value = json.loads(AuditPackage.objects.get().projection)
        assert value['evidence'] == 'with_gaps' and value['gaps']['live_origin_unavailable'] >= 1

    def test_D03_no_type_proof_cannot_use_current_project_xml(self):
        source, batch = self.prepared()
        source.delete()
        denied(self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (422,))

    def test_D04_missing_first_review_is_an_explicit_gap(self):
        source, batch = self.prepared()
        ReviewDecision.objects.filter(submission=source).delete()
        assert self.capture(batch).status_code == 202
        value = json.loads(AuditPackage.objects.get().projection)
        assert value['gaps']['initial_review_unavailable'] == 1
        assert not any(r['type'] == 'revision' and r['state'] == 'pending' for r in value['records'])

    def test_D05_receipt_only_followup_survives_deleted_supervision(self):
        source, batch, task = self.concerned()
        assert self.record_followup(self.followup_body(task)).status_code == 201
        SecondReviewTask.objects.get(pk=task).delete()
        assert self.capture(batch).status_code == 202
        types = {r['type'] for r in json.loads(AuditPackage.objects.get().projection)['records']}
        assert {'second_review', 'second_opinion', 'concern_followup'} <= types

    def test_D06_frozen_package_after_task_batch_and_review_deletion(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        source.assignment.task.delete()
        ReviewBatch.objects.get(pk=batch).delete()
        with patch.object(audit.audit_source, 'load', side_effect=AssertionError('no source read')):
            package = self.ready()
            response = self.client.get(self.audit_base + f'{package.pk}/manifest/')
        assert response.status_code == 200

    def test_D07_project_cleanup_preserves_actor_registry_and_files(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = self.ready()
        from data_export.audit_files import artifact_path

        path = artifact_path(package, package.artifact)
        self.project.delete()
        assert not AuditPackage.objects.exists() and AuditActorBinding.objects.count() == 1
        assert path.exists()

    def test_E01_redaction_applies_to_private_bundle_and_public_files(self):
        secret = 'PII_REASON_CANARY_不出包'
        source, batch = self.prepared(reason=secret)
        assert self.capture(batch).status_code == 202
        package = self.ready()
        assert secret not in package.input_bundle and secret not in package.projection
        response = self.client.get(self.audit_base + f'{package.pk}/artifacts/{package.artifact.pk}/download/')
        blob = b''.join(response.streaming_content)
        assert secret.encode() not in blob and self.manager.email.encode() not in blob
        assert source.result_hash.encode() not in blob

    def test_E03_aliases_cross_package_unlinkable_and_same_package_stable(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        first = self.ready()
        assert self.capture(batch).status_code == 202
        second = AuditPackage.objects.exclude(pk=first.pk).get()
        a = {r['id'] for r in json.loads(first.projection)['records'] if 'id' in r}
        b = {r['id'] for r in json.loads(second.projection)['records'] if 'id' in r}
        assert not a & b

    def test_E06_strict_json_transport_and_jcs(self):
        for raw in (
            b'{"review_batch_id":1,"review_batch_id":2}',
            b'{"review_batch_id":NaN}',
            b'{"review_batch_id":true}',
        ):
            denied(self.client.post(self.audit_base + 'preview/', raw, content_type='application/json'), (400,))
        from data_export import audit_contract as c

        assert c.canonical({'文': '🙂', 'a': 1}) == b'{"a":1,"\xe6\x96\x87":"\xf0\x9f\x99\x82"}'

    def test_E07_formatter_drift_blocks_source_free_retry(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        from unittest.mock import MagicMock

        with patch.object(audit.approved, 'api_queue', return_value=MagicMock()):
            self.real_enqueue(str(package.pk))
        package.refresh_from_db()
        with patch.object(audit.c, 'formatter', return_value={'unknown': 'deployment'}):
            audit.generate(str(package.pk), str(package.token))
        package.refresh_from_db()
        assert package.state == 'blocked_runtime'
        result = self.client.post(
            self.audit_base + f'{package.pk}/retry/', {}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
        )
        assert result.status_code == 200, result.data
        with patch.object(audit.audit_source, 'load', side_effect=AssertionError('no source read')):
            self.ready(package)

    def test_F01_original_key_is_source_free_even_after_batch_deletion(self):
        source, batch = self.prepared()
        body = self.audit_body(batch)
        key = uuid4()
        assert self.capture(batch, body=body, key=key).status_code == 202
        ReviewBatch.objects.get(pk=batch).delete()
        with patch.object(audit.audit_source, 'load', side_effect=AssertionError('no source read')):
            result = self.capture(batch, body=body, key=key)
        assert result.status_code == 202 and AuditPackage.objects.count() == 1

    def test_F02_same_key_changed_body_is_conflict(self):
        source, batch = self.prepared()
        body = self.audit_body(batch)
        key = uuid4()
        assert self.capture(batch, body=body, key=key).status_code == 202
        denied(self.capture(batch, body={**body, 'expected_capture_preview_hash': 'a' * 64}, key=key), (409,))

    def test_F03_active_owner_limit_is_atomic(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        denied(self.capture(batch), (429,))
        assert AuditPackage.objects.count() == 1 and AuditActorBinding.objects.count() == 1

    def test_G01_authenticated_head_range_and_manifest(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = self.ready()
        path = self.audit_base + f'{package.pk}/artifacts/{package.artifact.pk}/download/'
        full = self.client.get(path)
        data = b''.join(full.streaming_content)
        assert full.status_code == 200 and audit.c.digest(data) == package.artifact.sha256
        part = self.client.get(path, HTTP_RANGE='bytes=0-9')
        assert part.status_code == 206 and b''.join(part.streaming_content) == data[:10]
        head = self.client.head(path)
        assert head.status_code == 200 and int(head['Content-Length']) == len(data)
        list(head.streaming_content)

    def test_G05_file_corruption_quarantines_without_rebuilding(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = self.ready()
        from data_export.audit_files import artifact_path

        path = artifact_path(package, package.artifact)
        path.chmod(0o600)
        path.write_bytes(b'corrupt')
        denied(self.client.get(self.audit_base + f'{package.pk}/manifest/'), (409,))
        package.refresh_from_db()
        assert package.state == 'quarantined'

    def test_G07_multiple_and_out_of_bounds_ranges_rejected(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = self.ready()
        path = self.audit_base + f'{package.pk}/artifacts/{package.artifact.pk}/download/'
        for value in ('bytes=0-2,5-8', 'bytes=999999999-', 'nonsense', 'bytes=-0'):
            result = self.client.get(path, HTTP_RANGE=value)
            assert result.status_code == 416, (value, result.status_code)

    def test_H01_body_and_query_boundaries(self):
        for raw in (b' ' * 16385, b'{"review_batch_id":1,"unexpected":0}'):
            denied(self.client.post(self.audit_base + 'preview/', raw, content_type='application/json'), (400, 422))
        denied(self.client.get(self.audit_base + '?page_size=1&page_size=2'), (400,))
        denied(self.client.get(self.audit_base + '?page_size=true'), (400,))

    def test_H03_lineage_never_silently_truncated(self):
        source, batch = self.prepared(decision='rejected')
        create_submission(annotation=source.annotation, assignment=source.assignment, actor=self.worker)
        with self.settings(ANNOTATION_REVIEW_AUDIT_LIMITS={'per_assignment': 1}):
            denied(self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (422,))

    def test_H04_entity_and_edge_caps_fail_before_capture(self):
        source, batch = self.prepared()
        for limits in ({'entities': 1}, {'edges': 1}):
            with self.settings(ANNOTATION_REVIEW_AUDIT_LIMITS=limits):
                denied(
                    self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (422,)
                )
        assert not AuditPackage.objects.exists()

    def test_H06_two_pass_shared_bytes_cannot_be_freed_by_same_hash(self):
        source, batch = self.prepared()
        with self.settings(ANNOTATION_REVIEW_AUDIT_LIMITS={'canonical': 32}):
            denied(self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (422,))

    def test_H08_source_query_budget_is_checked_before_next_query(self):
        source, batch = self.prepared()
        with self.settings(ANNOTATION_REVIEW_AUDIT_LIMITS={'sql_per_pass': 1}):
            denied(self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (422,))

    def test_J01_database_guard_cannot_be_bypassed_by_queryset(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        for changes in (
            dict(projection='{}'),
            dict(owner_hash='a' * 64),
            dict(creator_principal='forged'),
            dict(input_bundle='[]'),
        ):
            with self.assertRaises(DatabaseError), transaction.atomic():
                AuditPackage.objects.filter(pk=package.pk).update(**changes)
        with self.assertRaises(DatabaseError), transaction.atomic():
            AuditActorBinding.objects.filter(pk=package.creator_id).update(live_user=None, tombstone=True)

    def test_J02_no_direct_ready_or_rewrite_ready(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        with self.assertRaises(DatabaseError), transaction.atomic():
            AuditPackage.objects.filter(pk=package.pk).update(state='ready')
        package = self.ready()
        with self.assertRaises(DatabaseError), transaction.atomic():
            AuditPackage.objects.filter(pk=package.pk).update(token=uuid4())

    def test_J03_no_child_delete_or_actor_registry_flush_backdoor(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        with self.assertRaises(DatabaseError), transaction.atomic():
            AuditPackage.objects.all().delete()
        self.project.delete()
        assert not AuditPackage.objects.exists()
        with self.assertRaises(DatabaseError), transaction.atomic():
            AuditActorBinding.objects.all().delete()


class TestAuditDownloadExceptions(AuditFixtures, APITestCase):
    def setUp(self):
        self.audit_setup()

    def prepared_package(self):
        source, batch = self.prepared()
        captured = self.capture(batch)
        assert captured.status_code == 202
        return self.ready(AuditPackage.objects.get(pk=captured.data['package_id']))

    def download_url(self, package, artifact_id=None):
        return self.audit_base + f'{package.pk}/artifacts/{artifact_id or package.artifact.pk}/download/'

    @contextmanager
    def opened_handles(self):
        from core.utils import private_file

        handles = []
        original = private_file.os.fdopen

        def track(*args, **kwargs):
            handle = original(*args, **kwargs)
            handles.append(handle)
            return handle

        with patch.object(private_file.os, 'fdopen', side_effect=track):
            yield handles

    def assert_ready(self, package):
        package.refresh_from_db()
        assert package.state == 'ready' and not package.error_code

    def assert_wrong_uuid(self, method):
        package = self.prepared_package()
        with self.opened_handles() as handles:
            response = getattr(self.client, method)(self.download_url(package, uuid4()))
        assert response.status_code == 404
        assert len(handles) == 1 and handles[0].closed
        self.assert_ready(package)
        correct = self.client.get(self.download_url(package))
        assert correct.status_code == 200
        assert audit.c.digest(b''.join(correct.streaming_content)) == package.artifact.sha256

    def test_wrong_artifact_uuid_get_does_not_quarantine(self):
        self.assert_wrong_uuid('get')

    def test_wrong_artifact_uuid_head_does_not_quarantine(self):
        self.assert_wrong_uuid('head')

    def test_late_authorization_denial_preserves_ready_and_closes_file(self):
        for method, action, headers in (
            ('get', 'download', {}),
            ('head', 'download', {}),
            ('get', 'download', {'HTTP_RANGE': 'bytes=0-9'}),
            ('get', 'download', {'HTTP_RANGE': 'bytes=999999999-'}),
            ('get', 'manifest', {}),
        ):
            package = self.prepared_package()

            def deny(*args, **kwargs):
                audit.c.fail('FORBIDDEN', 403)

            path = self.download_url(package) if action == 'download' else self.audit_base + f'{package.pk}/manifest/'
            with self.opened_handles() as handles, patch.object(audit, 'recheck', side_effect=deny):
                response = getattr(self.client, method)(path, **headers)
            assert response.status_code == 403, (method, action, headers)
            assert not response.streaming and 'download_url' not in str(response.data)
            assert len(handles) == 1 and handles[0].closed
            self.assert_ready(package)

    def test_allow_event_io_failure_preserves_ready_and_redacts_error(self):
        import traceback

        package = self.prepared_package()
        original = audit.event

        def fail_allow(ctx, phase, **kwargs):
            if phase == 'download':
                raise OSError('PII_DOWNLOAD_IO_CANARY')
            return original(ctx, phase, **kwargs)

        with self.opened_handles() as handles, patch.object(audit, 'event', side_effect=fail_allow):
            with self.assertLogs('core.utils.common', level='ERROR') as logs:
                response = self.client.get(self.download_url(package))
        assert response.status_code == 500 and response.data['detail'] == 'AUDIT_REQUEST_REJECTED'
        assert not response.streaming and len(handles) == 1 and handles[0].closed
        assert 'PII_DOWNLOAD_IO_CANARY' not in str(response.data) + '\n'.join(logs.output)
        assert all(
            'PII_DOWNLOAD_IO_CANARY' not in ''.join(traceback.format_exception(*record.exc_info))
            for record in logs.records
        )
        self.assert_ready(package)

    def assert_caller_exception(self, error):
        from data_export.audit_api import AuditAPI

        package = self.prepared_package()
        view = AuditAPI()
        with self.opened_handles() as handles, self.assertRaises(type(error)) as caught:
            with view.checked(package) as (handle, artifact):
                assert handle is handles[0] and not handle.closed
                raise error
        assert caught.exception is error and not getattr(view, 'quarantine_id', None)
        assert len(handles) == 1 and handles[0].closed
        self.assert_ready(package)

    def test_caller_api_exception_propagates_unchanged(self):
        from rest_framework.exceptions import PermissionDenied

        self.assert_caller_exception(PermissionDenied('AUDIT_FORBIDDEN'))

    def test_caller_os_error_propagates_unchanged(self):
        self.assert_caller_exception(OSError('synthetic caller failure'))

    def test_stream_keeps_verified_descriptor_until_consumed(self):
        from data_export import audit_format

        package = self.prepared_package()
        with self.opened_handles() as handles, patch.object(
            audit_format, 'verify', wraps=audit_format.verify
        ) as verify:
            response = self.client.get(self.download_url(package))
        assert response.status_code == 200 and len(handles) == 1 and not handles[0].closed
        assert verify.call_count == 1 and verify.call_args.args[0] is handles[0]
        assert response.ranged_file.f is handles[0]
        assert audit.c.digest(b''.join(response.streaming_content)) == package.artifact.sha256
        assert handles[0].closed
        self.assert_ready(package)

    def test_unconsumed_stream_close_releases_descriptor(self):
        from django.test.client import closing_iterator_wrapper

        package = self.prepared_package()
        with self.opened_handles() as handles:
            response = self.client.get(self.download_url(package))
        assert response.status_code == 200 and len(handles) == 1 and not handles[0].closed
        # Reuse the test client's connection shielding while closing the real
        # response without reading bytes inside this transactional TestCase.
        list(closing_iterator_wrapper((), response.close))
        assert handles[0].closed
        self.assert_ready(package)

    def test_nonstream_and_invalid_range_release_descriptor(self):
        package = self.prepared_package()
        for path, headers, status in (
            (self.audit_base + f'{package.pk}/', {}, 200),
            (self.audit_base + f'{package.pk}/manifest/', {}, 200),
            (self.download_url(package), {'HTTP_RANGE': 'bytes=999999999-'}, 416),
        ):
            with self.opened_handles() as handles:
                response = self.client.get(path, **headers)
            assert response.status_code == status and not response.streaming
            assert len(handles) == 1 and handles[0].closed
            self.assert_ready(package)

    def test_missing_artifact_file_is_still_quarantined(self):
        from data_export.audit_files import artifact_path

        package = self.prepared_package()
        path = artifact_path(package, package.artifact)
        path.chmod(0o600)
        path.unlink()
        with self.opened_handles() as handles:
            response = self.client.get(self.download_url(package))
        assert response.status_code == 409 and response.data['detail'] == 'AUDIT_FILE_INVALID'
        assert handles == []
        package.refresh_from_db()
        assert package.state == 'quarantined'

    def test_real_hash_failure_still_quarantines_and_closes_file(self):
        from data_export.audit_files import artifact_path

        package = self.prepared_package()
        path = artifact_path(package, package.artifact)
        bad = bytearray(path.read_bytes())
        bad[0] ^= 1
        path.chmod(0o600)
        path.write_bytes(bad)
        with self.opened_handles() as handles:
            response = self.client.get(self.download_url(package))
        assert response.status_code == 409 and response.data['detail'] == 'AUDIT_FILE_INVALID'
        assert len(handles) == 1 and handles[0].closed
        package.refresh_from_db()
        assert package.state == 'quarantined'

    def test_bad_zip_matching_observed_hash_still_quarantines_and_closes_file(self):
        from data_export import audit_format

        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        original = audit_format.write

        def corrupt_writer(package, bundle, output, **kwargs):
            manifest = original(package, bundle, output, **kwargs)
            output.seek(0)
            output.truncate()
            output.write(b'not a zip')
            return manifest

        # A faulty formatter publishes through real generation and immutable
        # size/hash metadata. The read must reach the actual ZIP verifier.
        with patch.object(audit_format, 'write', side_effect=corrupt_writer):
            package = self.ready()
        with self.opened_handles() as handles, patch.object(
            audit_format, 'verify', wraps=audit_format.verify
        ) as verify:
            response = self.client.get(self.download_url(package))
        assert response.status_code == 409 and response.data['detail'] == 'AUDIT_FILE_INVALID'
        assert verify.call_count == 1 and verify.call_args.args[0] is handles[0]
        assert len(handles) == 1 and handles[0].closed
        package.refresh_from_db()
        assert package.state == 'quarantined'
