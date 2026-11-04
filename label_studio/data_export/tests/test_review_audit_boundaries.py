"""Negative paths and real legacy export/snapshot compatibility for the audit adapter."""
import copy
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from access_control.project_operations import local_principal
from data_export import approved, audit_format, snapshots
from data_export import audit_contract as c
from data_export import review_audit as audit
from data_export.audit_models import AuditActorBinding, AuditArtifact, AuditPackage
from django.db import DatabaseError, OperationalError, connection, transaction
from django.test import override_settings
from django.utils import timezone
from projects.models import ProjectMember
from rest_framework.exceptions import APIException
from rest_framework.test import APITestCase
from tasks.models import ReviewDecision, Submission
from tasks.submissions import _snapshot_hash
from users.models import User
from users.tests.factories import UserFactory

from .test_review_audit import AuditFixtures, denied


class TestAuditBoundaries(AuditFixtures, APITestCase):
    def setUp(self):
        self.audit_setup()

    def test_B03_revoked_creators_are_filtered_before_pagination(self):
        source, batch = self.prepared()
        body = self.audit_body(batch)
        assert self.capture(batch, body=body).status_code == 202
        own = AuditPackage.objects.get()
        self.client.force_authenticate(self.producer)
        assert (
            self.client.post(self.audit_base, body, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())).status_code
            == 202
        )
        hidden = AuditPackage.objects.exclude(pk=own.pk).get()
        ProjectMember.objects.filter(project=self.project, user=self.producer).update(enabled=False)
        self.client.force_authenticate(self.manager)
        result = self.client.get(self.audit_base + '?page_size=1')
        assert result.status_code == 200, result.data
        assert [row['package_id'] for row in result.data['results']] == [str(own.pk)]
        assert result.data['next_cursor'] is None and str(hidden.pk) not in str(result.data)

    def test_A03_different_frozen_task_input_is_not_equal_answers(self):
        source, batch = self.prepared(decision='rejected')
        source.assignment.task.data = {'text': 'Different frozen input'}
        source.assignment.task.save(update_fields=['data'])
        self.successor(source)
        assert self.capture(batch).status_code == 202
        records = json.loads(AuditPackage.objects.get().projection)['records']
        diff = next(r for r in records if r['type'] == 'difference')
        assert diff['state'] == 'unavailable' and diff['added_count'] is None and diff['removed_count'] is None

    def test_A02_unselected_and_unchecked_are_not_approved(self):
        self.make_source()
        self.make_source(self.second_worker)
        args = {k: v for k, v in self.args.items() if k != 'priority'}
        preview = self.preview(args)
        created = self.create_batch({**args, 'expected_plan_hash': preview.data['plan_hash']})
        assert created.status_code == 201, created.data
        assert self.capture(created.data['id']).status_code == 202
        records = json.loads(AuditPackage.objects.get().projection)['records']
        roots = [r for r in records if r['type'] == 'candidate']
        assert len(roots) == 2 and sum(r['selected'] for r in roots) == 1
        assert {r['state'] for r in records if r['type'] == 'revision'} == {'pending'}

    def test_B02_direct_collection_and_object_surfaces_reject_other_roles(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = self.ready()
        strangers = [
            self.worker,
            self.reviewer,
            UserFactory(is_staff=True, is_superuser=True, active_organization=self.org),
        ]
        for actor in strangers:
            self.org.add_user(actor)
            self.client.force_authenticate(actor)
            for suffix in (
                '',
                f'{package.pk}/',
                f'{package.pk}/manifest/',
                f'{package.pk}/artifacts/{package.artifact.pk}/download/',
            ):
                denied(self.client.get(self.audit_base + suffix), (403, 404))
            denied(
                self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (403, 404)
            )

    def test_B06_deleted_creator_generation_never_rehydrates_same_user_id(self):
        source, batch = self.prepared()
        body = self.audit_body(batch)
        key = str(uuid4())
        self.client.force_authenticate(self.producer)
        assert self.client.post(self.audit_base, body, format='json', HTTP_IDEMPOTENCY_KEY=key).status_code == 202
        package = AuditPackage.objects.get()
        old = AuditActorBinding.objects.get(pk=package.creator_id)
        user_id = self.producer.pk
        User.objects.filter(pk=user_id).delete()
        old.refresh_from_db()
        assert old.tombstone and old.live_user_id is None
        new = UserFactory(id=user_id, active_organization=self.org)
        self.org.add_user(new)
        ProjectMember.objects.create(project=self.project, user=new, role='manager')
        self.client.force_authenticate(new)
        denied(self.client.get(self.audit_base + f'{package.pk}/'), (403, 404))
        with (
            patch.object(audit.audit_source, 'load', side_effect=AssertionError('no source')),
            patch.object(audit, 'queue_available', side_effect=AssertionError('no queue')),
        ):
            denied(self.client.post(self.audit_base, body, format='json', HTTP_IDEMPOTENCY_KEY=key), (409,))
        assert AuditPackage.objects.count() == 1

    def test_C02_corrupt_plan_and_reused_numeric_root_are_rejected(self):
        source, batch = self.prepared()
        body = self.audit_body(batch)
        wrong = {**body, 'expected_batch_binding_hash': 'f' * 64}
        denied(self.capture(batch, body=wrong), (409,))
        assert not AuditPackage.objects.exists()

    def test_C03_confidence_versions_freeze_without_current_registry(self):
        source, batch = self.prepared(confidence=True)
        with self.settings(
            ANNOTATION_REVIEW_CONFIDENCE_PROFILES=[],
            ANNOTATION_REVIEW_CONFIDENCE_GRANTS=[],
            ANNOTATION_REVIEW_CONFIDENCE_ENABLED=False,
        ):
            assert self.capture(batch).status_code == 202
        private = json.loads(AuditPackage.objects.get().input_bundle)
        root = next(r for r in private['graph']['facts'] if r['type'] == 'batch')
        frozen = root['bindings']['plan']['priority']
        assert frozen['profile'] == self.descriptor and frozen['profile_digest'] == self.profile_digest

    def test_C07_legacy_large_number_hash_is_not_replaced_by_jcs(self):
        source, batch = self.prepared(decision='rejected')
        source.assignment.task.meta = {'large': 2**53 + 1}
        source.assignment.task.save(update_fields=['meta'])
        target = self.successor(source)
        assert target.result_snapshot['task']['meta']['large'] == 2**53 + 1
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        private = json.loads(package.input_bundle)
        pins = next(
            v['bindings']['pins']
            for v in private['graph']['facts']
            if v['type'] == 'revision' and v['key'] == str(target.pk)
        )
        assert pins['result_hash'] == _snapshot_hash(target.result_snapshot)
        assert str(2**53 + 1) not in package.projection

    def test_C08_author_manager_audit_does_not_grant_self_review(self):
        source, batch = self.prepared()
        ProjectMember.objects.filter(project=self.project, user=self.worker).update(role='manager')
        body = self.audit_body(batch)
        self.client.force_authenticate(self.worker)
        response = self.client.post(self.audit_base, body, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        assert response.status_code == 202, response.data
        source.refresh_from_db()
        assert source.status == 'approved' and ReviewDecision.objects.filter(submission=source).count() == 1

    def test_D08_scope_reports_historical_limit_instead_of_inventing_events(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = self.ready()
        manifest = json.loads(package.manifest)
        assert (
            manifest['history_completeness'] == 'not_provable' and manifest['live_source_integrity'] == 'not_checked'
        )
        assert manifest['effect'] == 'supervisory_only'

    def test_E02_reason_absent_and_present_redacted_are_distinct(self):
        source, batch = self.prepared(reason='')
        assert self.capture(batch).status_code == 202
        value = json.loads(AuditPackage.objects.get().projection)
        assert next(r for r in value['records'] if r['type'] == 'initial_review')['reason_state'] == 'absent'

    def test_E04_all_frozen_fields_are_guarded_in_direct_sql(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        table = connection.ops.quote_name(AuditPackage._meta.db_table)
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                f'UPDATE {table} SET input_sha256=%s WHERE id=%s',
                ['f' * 64, package.pk.hex if connection.vendor == 'sqlite' else package.pk],
            )
        assert AuditPackage.objects.get().input_sha256 == package.input_sha256

    def test_E05_dangling_edge_and_duplicate_identity_cannot_pass_reader(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        value = json.loads(AuditPackage.objects.get().projection)
        for change in ('dangling', 'duplicate'):
            bad = copy.deepcopy(value)
            if change == 'dangling':
                next(r for r in bad['records'] if r['type'] == 'edge')['target'] = 'f' * 64
            else:
                bad['records'].append(next(r for r in bad['records'] if r['type'] != 'edge'))
            with self.assertRaises(APIException):
                audit_format.projection(c.canonical(bad).decode())

    def test_F02_cross_owner_and_instance_keys_never_read_source_or_preflight(self):
        source, batch = self.prepared()
        body = self.audit_body(batch)
        key = str(uuid4())
        assert self.capture(batch, body=body, key=key).status_code == 202
        self.client.force_authenticate(self.producer)
        with (
            patch.object(audit.audit_source, 'load', side_effect=AssertionError('no source')),
            patch.object(audit, 'queue_available', side_effect=AssertionError('no queue')),
            patch('data_export.audit_files.private_root', side_effect=AssertionError('no files')),
        ):
            result = self.client.post(self.audit_base, body, format='json', HTTP_IDEMPOTENCY_KEY=key)
            denied(result, (409,))
            assert str(AuditPackage.objects.get().pk) not in str(result.data)
        rotated = {**self.audit_policy, 'instance_id': str(uuid4())}
        with self.settings(ANNOTATION_REVIEW_AUDIT_POLICY=rotated):
            denied(self.capture(batch, body=body, key=key), (409,))
        assert AuditPackage.objects.count() == 1

    def test_F02_viewer_salt_rotation_keeps_original_body_recoverable(self):
        source, batch = self.prepared()
        body = self.audit_body(batch)
        key = uuid4()
        assert self.capture(batch, body=body, key=key).status_code == 202
        with self.settings(SECRET_KEY='synthetic-audit-rotated-signing-key-only'), patch.object(
            audit.audit_source, 'load', side_effect=AssertionError('no source')
        ):
            assert self.capture(batch, body=body, key=key).status_code == 202

    def test_F04_dispatch_ack_unknown_preserves_single_producer_token(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        queue = MagicMock()
        queue.enqueue.side_effect = OSError('synthetic lost queue ACK')
        with patch.object(approved, 'api_queue', return_value=queue):
            self.real_enqueue(str(package.pk))
            self.real_enqueue(str(package.pk))
        package.refresh_from_db()
        assert package.state == 'queued' and package.attempt_number == 1
        assert queue.enqueue.call_count == 1

    def test_F05_queued_lease_and_running_fence_use_their_own_clocks(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        queue = MagicMock()
        with patch.object(approved, 'api_queue', return_value=queue):
            self.real_enqueue(str(package.pk))
        package.refresh_from_db()
        old = package.token
        at = package.queued_at
        with patch.object(audit.timezone, 'now', return_value=at + timedelta(minutes=29)):
            assert (
                self.client.post(
                    self.audit_base + f'{package.pk}/retry/', {}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
                ).status_code
                == 200
            )
        package.refresh_from_db()
        assert package.token == old and package.state == 'queued'
        with patch.object(audit.timezone, 'now', return_value=at + timedelta(minutes=30, seconds=1)):
            assert (
                self.client.post(
                    self.audit_base + f'{package.pk}/retry/', {}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4())
                ).status_code
                == 200
            )
        with patch.object(approved, 'api_queue', return_value=queue):
            self.real_enqueue(str(package.pk))
        package.refresh_from_db()
        assert package.token != old and package.attempt_number == 2
        audit.generate(str(package.pk), str(old))
        package.refresh_from_db()
        assert package.state == 'queued'

    def test_F06_creator_revocation_at_publish_prevents_ready(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        queue = MagicMock()
        with patch.object(approved, 'api_queue', return_value=queue):
            self.real_enqueue(str(package.pk))
        package.refresh_from_db()
        original = audit_format.write

        def revoke(*args, **kwargs):
            result = original(*args, **kwargs)
            ProjectMember.objects.filter(project=self.project, user=self.manager).update(enabled=False)
            from organizations.models import OrganizationMember

            OrganizationMember.objects.filter(user=self.manager, organization=self.org).update(
                deleted_at=timezone.now()
            )
            return result

        with patch.object(audit_format, 'write', side_effect=revoke):
            audit.generate(str(package.pk), str(package.token))
        package.refresh_from_db()
        assert package.state == 'blocked_auth' and not AuditArtifact.objects.exists()

    def test_F07_uncertain_publish_does_not_delete_potential_artifact(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        queue = MagicMock()
        with patch.object(approved, 'api_queue', return_value=queue):
            self.real_enqueue(str(package.pk))
        package.refresh_from_db()
        original = AuditPackage.save

        def unknown(row, *args, **kwargs):
            original(row, *args, **kwargs)
            if row.state == 'ready':
                raise OperationalError('synthetic commit status unknown')

        with patch.object(AuditPackage, 'save', unknown), self.assertRaises(DatabaseError):
            audit.generate(str(package.pk), str(package.token))
        from data_export.audit_files import directory

        assert (directory(package) / 'review-audit.zip').is_file()
        assert list(directory(package).parent.glob('.scratch-*'))
        package.refresh_from_db()
        assert package.state == 'running'

    def test_G03_windows_junction_ancestor_and_path_traversal_are_rejected(self):
        import os
        import subprocess

        from core.utils.private_file import PrivateFileError, checked_file

        if os.name != 'nt':
            self.skipTest('Windows native junction branch')
        root = Path(self.private.name)
        actual = root / 'actual'
        actual.mkdir()
        (actual / 'f').write_bytes(b'a')
        alias = root / 'alias'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(actual)], capture_output=True)
        assert result.returncode == 0
        with self.assertRaises(PrivateFileError):
            with checked_file(alias / 'f', c.digest(b'a'), 1):
                pass

    def test_G04_untrusted_private_root_dacl_is_not_silently_repaired(self):
        from data_export.audit_files import private_root

        source, batch = self.prepared()
        private_root()
        with patch('data_export.audit_files.windows_acl', side_effect=lambda *args: c.fail('UNSAFE_ROOT', 503)):
            if __import__('os').name == 'nt':
                denied(self.capture(batch), (503,))

    def test_G06_private_subtree_reservation_outlives_audit_mode(self):
        from data_export.audit_files import private_root
        from data_export.boundary import is_approved_export_path

        private = private_root()
        for enabled in (True, False):
            with self.settings(ANNOTATION_REVIEW_AUDIT_ENABLED=enabled):
                assert is_approved_export_path(private / 'token' / 'review-audit.zip')

    def test_G04_real_windows_ancestor_delete_child_acl_is_rejected(self):
        import os
        import subprocess

        if os.name != 'nt':
            self.skipTest('Windows native DACL branch')
        grand = Path(self.private.name) / 'untrusted-parent'
        grand.mkdir()
        result = subprocess.run(['icacls', str(grand), '/grant', '*S-1-1-0:(DC)'], capture_output=True)
        assert result.returncode == 0
        from data_export.audit_files import private_root

        with self.settings(APPROVED_EXPORT_ROOT=str(grand / 'private')):
            with self.assertRaises(APIException):
                private_root()

    def test_G08_allow_event_failure_prevents_file_bytes(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = self.ready()
        with patch.object(audit, 'event', side_effect=OSError('synthetic audit sink unavailable')):
            response = self.client.get(self.audit_base + f'{package.pk}/artifacts/{package.artifact.pk}/download/')
        assert response.status_code >= 400 and not getattr(response, 'streaming', False)

    def test_H05_stored_byte_gate_runs_before_oversized_json_decoder(self):
        source, batch = self.prepared()
        snapshot = copy.deepcopy(source.result_snapshot)
        snapshot['padding'] = 'x' * (2 * c.MIB + 1)
        Submission.objects.filter(pk=source.pk).update(result_snapshot=snapshot)
        from django.db.models import JSONField

        original = JSONField.from_db_value

        def bounded(field, value, *args, **kwargs):
            assert value is None or len(value.encode()) <= 2 * c.MIB, 'oversized JSON hydrated before SQL gate'
            return original(field, value, *args, **kwargs)

        with patch.object(JSONField, 'from_db_value', bounded):
            denied(self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (422,))

    def test_H07_partial_zip_never_exceeds_scratch_cap_or_becomes_ready(self):
        source, batch = self.prepared()
        with self.settings(ANNOTATION_REVIEW_AUDIT_LIMITS={'artifact': 1024, 'scratch': 2048}):
            assert self.capture(batch).status_code == 202
            package = AuditPackage.objects.get()
            queue = MagicMock()
            with patch.object(approved, 'api_queue', return_value=queue):
                self.real_enqueue(str(package.pk))
            package.refresh_from_db()
            audit.generate(str(package.pk), str(package.token))
            package.refresh_from_db()
            assert (
                package.state == 'failed_final'
                and package.error_code == 'AUDIT_TOO_LARGE'
                and not AuditArtifact.objects.exists()
            )
            from data_export.audit_files import directory

            assert not (directory(package) / 'review-audit.zip').exists()

    def test_J05_real_approved_export_and_snapshot_remain_exact(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Legacy #79/#80 acceptance requires native PostgreSQL')
        from data_export.audit_files import private_root

        private_root()
        source, batch = self.prepared()
        policy = dict(
            source_namespace=str(uuid4()),
            projects={f'{self.org.pk}:{self.project.pk}': dict(mode='fork_approved', version='legacy-v1')},
        )
        body = dict(
            items=[dict(submission_id=source.pk, revision=source.revision, result_hash=source.result_hash)],
            output=dict(format='JSON', resource_mode='references_only'),
            filters={},
        )
        with self.settings(APPROVED_EXPORT_POLICY=policy), patch.object(
            approved, 'queue_available', return_value=True
        ):
            release = approved.freeze(
                self.project, local_principal(self.manager), self.manager, body, 'approved-before-audit'
            )
            from data_export.tests.test_approved_exports import TestApprovedExports

            TestApprovedExports.generate(self, release)
            assert release.state == 'ready', release.error_code
            snapshot = snapshots.create(
                release, local_principal(self.manager), self.manager, 'snapshot-before-audit', {}
            )
            snapshot.state = 'queued'
            snapshot.save(update_fields=['state'])
            snapshots.generate(str(snapshot.pk), str(snapshot.token))
            snapshot.refresh_from_db()
            assert snapshot.state == 'ready', snapshot.error_code
            before = bytes(release.manifest), bytes(snapshot.manifest), source.result_hash
            assert self.capture(batch).status_code == 202
            release.refresh_from_db()
            snapshot.refresh_from_db()
            source.refresh_from_db()
            assert (bytes(release.manifest), bytes(snapshot.manifest), source.result_hash) == before
            from django.db.models.deletion import ProtectedError

            with self.assertRaises(ProtectedError):
                self.project.delete()


from classification_disagreements.tests import test_arbitration as arbitration_fixture
from review_batches.tests.handoff_fixtures import HandoffFixtures


@override_settings(
    ANNOTATION_REVIEW_BATCHES_ENABLED=True,
    ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=True,
    ANNOTATION_CLASSIFICATION_ARBITRATION_ENABLED=True,
    ANNOTATION_REVIEW_ARBITRATION_HANDOFF_ENABLED=True,
)
class TestAuditArbitration(HandoffFixtures, AuditFixtures, APITestCase):
    setUpTestData = classmethod(arbitration_fixture.TestArbitration.setUpTestData.__func__)

    def setUp(self):
        HandoffFixtures.setUp(self)
        self.audit_setup(initialize_sources=False)

    def test_A06_direct_handoff_pending_and_boundary_cohort(self):
        assert self.handoff().status_code == 201
        assert self.capture(self.batch.pk).status_code == 202
        value = json.loads(AuditPackage.objects.get().projection)
        assert {'handoff', 'arbitration', 'boundary_source_ref'} <= {r['type'] for r in value['records']}

    def test_A06_authoritative_arbitration_decision_remains_distinct(self):
        response = self.handoff()
        assert response.status_code == 201, response.data
        from classification_disagreements.models import ArbitrationTask

        target = ArbitrationTask.objects.get(pk=response.data['arbitration_id'])
        self.client.force_authenticate(self.arbiter)
        result = self.command(target)
        assert result.status_code == 201, result.data
        assert self.capture(self.batch.pk).status_code == 202
        value = json.loads(AuditPackage.objects.get().projection)
        assert any(
            r['type'] == 'arbitration_decision' and r['decision'] == 'adopt_exact_submission' for r in value['records']
        )

    def test_A06_arbitration_cancellation_is_a_distinct_timed_record(self):
        response = self.handoff()
        assert response.status_code == 201, response.data
        from classification_disagreements.models import ArbitrationTask

        target = ArbitrationTask.objects.get(pk=response.data['arbitration_id'])
        self.client.force_authenticate(self.manager)
        result = self.command(target, cancel=True)
        assert result.status_code == 201, result.data
        assert self.capture(self.batch.pk).status_code == 202
        value = json.loads(AuditPackage.objects.get().projection)
        cancel = next(r for r in value['records'] if r['type'] == 'cancellation')
        assert cancel['created_at'] and cancel['reason_state'] == 'present_redacted'

    def test_D05_deleted_arbitration_pk_cannot_rehydrate_a_new_target(self):
        from classification_disagreements.models import ArbitrationTask

        result = self.handoff()
        assert result.status_code == 201
        target = ArbitrationTask.objects.get(pk=result.data['arbitration_id'])
        frozen = {field.attname: getattr(target, field.attname) for field in target._meta.fields}
        target.delete()
        ArbitrationTask.objects.create(**frozen)
        assert self.capture(self.batch.pk).status_code == 202
        value = json.loads(AuditPackage.objects.get().projection)
        assert next(row for row in value['records'] if row['type'] == 'arbitration')['state'] == 'unavailable'

    def test_H04_real_retained_cohort_union_2000_and_2001(self):
        """Typed historical ledgers, at most20 references per target; no validator mocks.

        Boundary refs need no live answer/source hydration. These synthetic ORM
        fixtures prove loader bounds, not authenticity of a historical producer.
        """
        from classification_disagreements import arbitration
        from classification_disagreements.models import ArbitrationTask
        from review_batches import handoff
        from review_batches.models import ReviewArbitrationHandoff

        response = self.handoff()
        assert response.status_code == 201, response.data
        original = ArbitrationTask.objects.get(pk=response.data['arbitration_id'])
        ledger = ReviewArbitrationHandoff.objects.get(arbitration=original)
        assert len(original.origin['item']['cohort']) == 2
        for total, expected in ((2000, 202), (2001, 422)):
            with transaction.atomic():
                targets, next_ref = [], 100000
                # The original target contributes2. 99*20 +18/19 completes
                # the exact shared boundary without breaking producer arity.
                for length in [20] * 99 + [total - 2 - 99 * 20]:
                    values = {
                        f.attname: getattr(original, f.attname) for f in original._meta.fields if not f.primary_key
                    }
                    origin = copy.deepcopy(original.origin)
                    cohort = []
                    for _ in range(length):
                        next_ref += 1
                        entry = copy.deepcopy(original.origin['item']['cohort'][0])
                        entry.update(assignment_id=next_ref, assignee_id=next_ref)
                        entry['source'].update(assignment_id=next_ref, submission_id=next_ref, submitter_id=next_ref)
                        cohort.append(entry)
                    origin['item']['cohort'] = cohort
                    values.update(
                        origin=origin,
                        origin_hash=arbitration.digest(origin),
                        input_set_hash=arbitration.input_hash(self.project, origin['item']),
                        participant_ids=arbitration.participants(origin['item']),
                        idempotency_key=uuid4(),
                    )
                    targets.append(ArbitrationTask(**values))
                ArbitrationTask.objects.bulk_create(targets)
                ledgers = []
                for target in targets:
                    arbitration.checked(target, self.project)
                    values = {f.attname: getattr(ledger, f.attname) for f in ledger._meta.fields if not f.primary_key}
                    key, created = uuid4(), timezone.now()
                    origin = copy.deepcopy(ledger.origin)
                    origin.update(key=str(key), created_at=created.isoformat(), target=handoff.target_pin(target))
                    values.update(
                        arbitration_id=target.pk,
                        arbitration_id_frozen=target.pk,
                        idempotency_key=key,
                        created_at=created,
                        origin=origin,
                        origin_hash=arbitration.digest(origin),
                    )
                    row = ReviewArbitrationHandoff(**values)
                    handoff.checked(row, self.project)
                    ledgers.append(row)
                ReviewArbitrationHandoff.objects.bulk_create(ledgers)
                captured = (
                    self.capture(self.batch.pk)
                    if expected == 202
                    else self.client.post(
                        self.audit_base + 'preview/', {'review_batch_id': self.batch.pk}, format='json'
                    )
                )
                assert captured.status_code == expected, captured.data
                if expected == 202:
                    projection = c.strict(AuditPackage.objects.get().projection)
                    assert projection['counts']['boundary_source_ref'] == total
                    assert sum(row['type'] == 'boundary_source_ref' for row in projection['records']) == total
                else:
                    assert not AuditPackage.objects.exists()
                transaction.set_rollback(True)
