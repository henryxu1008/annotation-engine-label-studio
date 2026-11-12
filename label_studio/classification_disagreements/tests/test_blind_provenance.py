from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event
from unittest import skipUnless
from uuid import uuid4

from classification_disagreements.blind_provenance import (
    account_born_after_reference,
    account_has_never_been_assigned,
    account_has_untainted_privilege_history,
)
from classification_disagreements.models import (
    BlindPrivilegeHistory,
    BlindCaptureCertificate,
    BlindCaptureEpoch,
    BlindProvenanceEvent,
    BlindProvenanceFence,
    ClassificationGoldEvidence,
)
from django.core.management import call_command
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import F
from django.test import TestCase, TransactionTestCase
from organizations.tests.factories import OrganizationFactory
from projects.models import ProjectMember
from projects.tests.factories import ProjectFactory
from tasks.models import TaskAssignment
from tasks.tests.factories import TaskFactory
from users.models import User
from users.tests.factories import UserFactory


def enable_test_capture():
    # Synthetic database only. Production activation is intentionally absent.
    BlindCaptureEpoch.objects.get_or_create(pk=1)
    BlindCaptureEpoch.objects.filter(pk=1).update(generation=F('generation') + 1, enabled=True)


class TestBlindProvenance(TestCase):
    def reference(self, *, kind='reference'):
        return ClassificationGoldEvidence.objects.create(
            project=self.project, created_by=self.owner, actor_id_frozen=self.owner.pk,
            principal='local:test', principal_source='local', kind=kind, idempotency_key=uuid4(),
            request_hash='0' * 64, manifest={}, manifest_hash='1' * 64,
        )

    def setUp(self):
        enable_test_capture()
        self.owner = UserFactory()
        self.project = ProjectFactory(created_by=self.owner)

    def test_account_order_is_database_captured_and_not_a_mutable_date(self):
        before = UserFactory()
        reference = self.reference()
        after = UserFactory()
        assert not account_born_after_reference(account_id=before.pk, reference_id=reference.pk)
        assert account_born_after_reference(account_id=after.pk, reference_id=reference.pk)
        User.objects.filter(pk=before.pk).update(date_joined='2099-01-01T00:00:00Z')
        assert not account_born_after_reference(account_id=before.pk, reference_id=reference.pk)

    def test_reused_account_id_cannot_inherit_eligibility_or_replace_birth_evidence(self):
        reference = self.reference()
        member = UserFactory()
        account_id = member.pk
        prior_birth = BlindProvenanceEvent.objects.get(kind='account', subject_id=account_id).sequence
        assert account_born_after_reference(account_id=account_id, reference_id=reference.pk)
        assert account_has_untainted_privilege_history(account_id=account_id)
        User.objects.filter(pk=account_id).delete()
        replacement = UserFactory(id=account_id)
        assert replacement.pk == account_id
        assert BlindProvenanceEvent.objects.get(kind='account', subject_id=account_id).sequence == prior_birth
        assert not account_has_untainted_privilege_history(account_id=account_id)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindPrivilegeHistory.objects.filter(account_id=account_id).update(elevated=False)

    def test_reused_account_with_missing_history_remains_ineligible(self):
        member = UserFactory()
        account_id = member.pk
        prior_birth = BlindProvenanceEvent.objects.get(kind='account', subject_id=account_id).sequence
        User.objects.filter(pk=account_id).delete()
        BlindPrivilegeHistory.objects.filter(account_id=account_id).delete()
        replacement = UserFactory(id=account_id)
        assert replacement.pk == account_id
        assert BlindProvenanceEvent.objects.get(kind='account', subject_id=account_id).sequence == prior_birth
        assert not account_has_untainted_privilege_history(account_id=account_id)

    def test_reused_account_with_only_retained_certificate_remains_ineligible(self):
        member = UserFactory()
        account_id = member.pk
        assert BlindCaptureCertificate.objects.filter(account_id=account_id).exists()
        User.objects.filter(pk=account_id).delete()
        BlindPrivilegeHistory.objects.filter(account_id=account_id).delete()
        BlindProvenanceEvent.objects.filter(kind='account', subject_id=account_id).delete()
        replacement = UserFactory(id=account_id)
        assert replacement.pk == account_id
        assert BlindCaptureCertificate.objects.filter(account_id=account_id).exists()
        assert not account_has_untainted_privilege_history(account_id=account_id)

    def test_bulk_insert_is_captured_and_missing_events_fail_closed(self):
        reference = self.reference()
        # bulk_create does not invoke Django model save hooks.
        member = UserFactory.build()
        User.objects.bulk_create([member])
        assert account_born_after_reference(account_id=member.pk, reference_id=reference.pk)
        BlindProvenanceEvent.objects.filter(kind='account', subject_id=member.pk).delete()
        assert not account_born_after_reference(account_id=member.pk, reference_id=reference.pk)
        assert not account_born_after_reference(account_id=member.pk, reference_id=999999999)

    def test_score_is_not_a_reference_and_invalid_ids_are_denied(self):
        score = self.reference(kind='score')
        member = UserFactory()
        assert not account_born_after_reference(account_id=member.pk, reference_id=score.pk)
        assert not account_born_after_reference(account_id=True, reference_id=score.pk)

    def test_recorded_order_cannot_be_updated(self):
        reference = self.reference()
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindProvenanceEvent.objects.filter(kind='reference', subject_id=reference.pk).update(sequence=999999)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindProvenanceFence.objects.filter(pk=1).update(sequence=0)

    def test_rolled_back_reference_does_not_leave_evidence(self):
        reference_id = None
        try:
            with transaction.atomic():
                reference_id = self.reference().pk
                raise ValueError('rollback')
        except ValueError:
            pass
        assert not BlindProvenanceEvent.objects.filter(kind='reference', subject_id=reference_id).exists()

    def test_recreated_fence_preserves_order_of_surviving_events(self):
        reference = self.reference()
        BlindProvenanceFence.objects.filter(pk=1).delete()
        member = UserFactory()
        assert account_born_after_reference(account_id=member.pk, reference_id=reference.pk)

    def test_role_removal_and_recreation_cannot_erase_privileged_history(self):
        member = UserFactory()
        assert account_has_untainted_privilege_history(account_id=member.pk)
        membership = ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        assert account_has_untainted_privilege_history(account_id=member.pk)
        ProjectMember.objects.filter(pk=membership.pk).update(role='manager')
        ProjectMember.objects.filter(pk=membership.pk).delete()
        ProjectMember.objects.create(project=self.project, user=member, role='annotator')
        assert not account_has_untainted_privilege_history(account_id=member.pk)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindPrivilegeHistory.objects.filter(account_id=member.pk).update(elevated=False)

    def test_bulk_privilege_staff_and_ownership_are_captured(self):
        reviewer, staff, project_owner, organization_owner = [UserFactory() for _ in range(4)]
        ProjectMember.objects.bulk_create([
            ProjectMember(project=self.project, user=reviewer, role='reviewer', enabled=False),
        ])
        User.objects.filter(pk=staff.pk).update(is_staff=True)
        User.objects.filter(pk=staff.pk).update(is_staff=False)
        ProjectFactory(created_by=project_owner)
        OrganizationFactory(created_by=organization_owner)
        for user in (reviewer, staff, project_owner, organization_owner):
            assert not account_has_untainted_privilege_history(account_id=user.pk)

    def test_superuser_at_account_creation_and_transferred_ownership_are_captured(self):
        superuser = UserFactory(is_superuser=True)
        new_owner = UserFactory()
        assert not account_has_untainted_privilege_history(account_id=superuser.pk)
        type(self.project).objects.filter(pk=self.project.pk).update(created_by=new_owner)
        assert not account_has_untainted_privilege_history(account_id=new_owner.pk)

    def test_prior_assignment_is_remembered_after_deletion_and_cannot_be_reset(self):
        member = UserFactory()
        assert account_has_never_been_assigned(account_id=member.pk)
        task = TaskFactory(project=self.project)
        assignment = TaskAssignment.objects.create(task=task, project=self.project, assignee=member)
        assignment_id = assignment.pk
        assignment.delete()
        assert not account_has_never_been_assigned(account_id=member.pk)
        assert BlindPrivilegeHistory.objects.get(account_id=member.pk).first_assignment_id == assignment_id
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindPrivilegeHistory.objects.filter(account_id=member.pk).update(had_assignment=False)

    def test_bulk_assignment_is_captured_and_rollback_restores_history(self):
        first, second = UserFactory(), UserFactory()
        task = TaskFactory(project=self.project)
        with self.assertRaises(ValueError):
            with transaction.atomic():
                TaskAssignment.objects.bulk_create([
                    TaskAssignment(task=task, project=self.project, assignee=first),
                ])
                raise ValueError('rollback')
        assert account_has_never_been_assigned(account_id=first.pk)
        TaskAssignment.objects.bulk_create([TaskAssignment(task=task, project=self.project, assignee=second)])
        assert not account_has_never_been_assigned(account_id=second.pk)

    def test_reassigned_existing_row_records_new_assignee_history(self):
        first, second = UserFactory(), UserFactory()
        task = TaskFactory(project=self.project)
        assignment = TaskAssignment.objects.create(task=task, project=self.project, assignee=first)
        assignment_id = assignment.pk
        TaskAssignment.objects.filter(pk=assignment.pk).update(assignee=second)
        assignment.delete()
        assert not account_has_never_been_assigned(account_id=second.pk)
        assert BlindPrivilegeHistory.objects.get(account_id=second.pk).first_assignment_id == assignment_id

    def test_missing_history_and_rolled_back_promotion_fail_closed(self):
        member = UserFactory()
        with self.assertRaises(ValueError):
            with transaction.atomic():
                ProjectMember.objects.create(project=self.project, user=member, role='manager')
                raise ValueError('rollback')
        assert account_has_untainted_privilege_history(account_id=member.pk)
        BlindPrivilegeHistory.objects.filter(account_id=member.pk).delete()
        assert not account_has_untainted_privilege_history(account_id=member.pk)
        assert not account_has_untainted_privilege_history(account_id=True)

    def test_migration_invalidates_old_certificates_and_requires_new_generation(self):
        before = UserFactory()
        assert account_has_untainted_privilege_history(account_id=before.pk)
        migration = MigrationRecorder.Migration.objects.create(app='synthetic_test', name=str(uuid4()))
        assert not account_has_untainted_privilege_history(account_id=before.pk)
        while_disabled = UserFactory()
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindCaptureEpoch.objects.filter(pk=1).update(enabled=True)
        enable_test_capture()
        assert not account_has_untainted_privilege_history(account_id=before.pk)
        assert not account_has_untainted_privilege_history(account_id=while_disabled.pk)
        after = UserFactory()
        assert account_has_untainted_privilege_history(account_id=after.pk)
        migration.delete()
        assert not account_has_untainted_privilege_history(account_id=after.pk)

    def test_recreating_epoch_cannot_restore_old_certificates(self):
        member = UserFactory()
        epoch = BlindCaptureEpoch.objects.get(pk=1)
        previous = epoch.generation
        epoch.delete()
        assert not account_has_untainted_privilege_history(account_id=member.pk)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindCaptureEpoch.objects.create(pk=1, generation=previous, enabled=False)
        BlindCaptureEpoch.objects.create(pk=1, generation=previous + 1, enabled=False)
        enable_test_capture()
        assert not account_has_untainted_privilege_history(account_id=member.pk)

    def test_certificate_updates_and_migration_edits_cannot_keep_old_eligibility(self):
        migration = MigrationRecorder.Migration.objects.create(app='synthetic_test', name=str(uuid4()))
        enable_test_capture()
        member = UserFactory()
        assert account_has_untainted_privilege_history(account_id=member.pk)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindCaptureCertificate.objects.filter(account_id=member.pk).update(generation=999999)
        MigrationRecorder.Migration.objects.filter(pk=migration.pk).update(name=str(uuid4()))
        assert not account_has_untainted_privilege_history(account_id=member.pk)


class SyntheticTransactionTestCase(TransactionTestCase):
    def _fixture_teardown(self):
        # The test database contains legacy tables no longer represented by
        # models; PostgreSQL requires them in the same TRUNCATE operation.
        call_command('flush', verbosity=0, interactive=False, database='default',
                     reset_sequences=False, allow_cascade=True)


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL transaction-lock test')
class TestBlindProvenanceConcurrency(SyntheticTransactionTestCase):
    def setUp(self):
        enable_test_capture()

    def test_epoch_deletion_waits_for_inflight_certificate(self):
        started, release, deleting = Event(), Event(), Event()
        generation = BlindCaptureEpoch.objects.get(pk=1).generation

        def create_account():
            close_old_connections()
            try:
                with transaction.atomic():
                    user = UserFactory()
                    started.set()
                    if not release.wait(10):
                        raise AssertionError('account transaction was not released')
                    return user.pk
            finally:
                close_old_connections()

        def delete_epoch():
            close_old_connections()
            try:
                deleting.set()
                BlindCaptureEpoch.objects.filter(pk=1).delete()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            creation = pool.submit(create_account)
            try:
                assert started.wait(10)
                deletion = pool.submit(delete_epoch)
                assert deleting.wait(10)
                with self.assertRaises(TimeoutError):
                    deletion.result(timeout=0.3)
            finally:
                release.set()
            account_id = creation.result(timeout=10)
            deletion.result(timeout=10)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindCaptureEpoch.objects.create(pk=1, generation=generation, enabled=False)
        assert not account_has_untainted_privilege_history(account_id=account_id)

    def test_reference_publication_fences_concurrent_account_creation(self):
        owner = UserFactory()
        project = ProjectFactory(created_by=owner)
        inserted = Event()
        release = Event()
        started = Event()

        def publish():
            close_old_connections()
            try:
                with transaction.atomic():
                    ref = ClassificationGoldEvidence.objects.create(
                        project_id=project.pk, created_by_id=owner.pk, actor_id_frozen=owner.pk,
                        principal='local:test', principal_source='local', kind='reference',
                        idempotency_key=uuid4(), request_hash='0' * 64, manifest={}, manifest_hash='1' * 64,
                    )
                    inserted.set()
                    if not release.wait(10):
                        raise AssertionError('publication was not released')
                    return ref.pk
            finally:
                close_old_connections()

        def create_account():
            close_old_connections()
            try:
                started.set()
                return UserFactory().pk
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            publishing = pool.submit(publish)
            try:
                assert inserted.wait(10)
                creating = pool.submit(create_account)
                assert started.wait(10)
                with self.assertRaises(TimeoutError):
                    creating.result(timeout=0.3)
            finally:
                release.set()
            reference_id = publishing.result(timeout=10)
            account_id = creating.result(timeout=10)
        assert account_born_after_reference(account_id=account_id, reference_id=reference_id)

    def test_privilege_grant_waits_for_locked_history(self):
        project = ProjectFactory()
        member = UserFactory()
        locked, release, started = Event(), Event(), Event()

        def hold_history():
            close_old_connections()
            try:
                with transaction.atomic():
                    history = BlindPrivilegeHistory.objects.select_for_update().get(account_id=member.pk)
                    assert not history.elevated
                    locked.set()
                    if not release.wait(10):
                        raise AssertionError('history lock was not released')
            finally:
                close_old_connections()

        def promote():
            close_old_connections()
            try:
                started.set()
                ProjectMember.objects.create(project_id=project.pk, user_id=member.pk, role='manager')
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            holding = pool.submit(hold_history)
            try:
                assert locked.wait(10)
                promotion = pool.submit(promote)
                assert started.wait(10)
                with self.assertRaises(TimeoutError):
                    promotion.result(timeout=0.3)
            finally:
                release.set()
            holding.result(timeout=10)
            promotion.result(timeout=10)
        assert not account_has_untainted_privilege_history(account_id=member.pk)


class TestBlindProvenanceMigration(SyntheticTransactionTestCase):
    def test_existing_reference_is_backfilled_but_existing_account_is_not(self):
        before = [('classification_disagreements', '0020_classification_gold_evidence')]
        executor = MigrationExecutor(connection)
        # Restore every current leaf, including later blind-safety migrations,
        # so subsequent transaction tests run against the real schema.
        after = executor.loader.graph.leaf_nodes()
        executor.migrate(before)
        try:
            owner = UserFactory()
            project = ProjectFactory(created_by=owner)
            reference = ClassificationGoldEvidence.objects.create(
                project=project, created_by=owner, actor_id_frozen=owner.pk,
                principal='local:test', principal_source='local', kind='reference',
                idempotency_key=uuid4(), request_hash='0' * 64, manifest={}, manifest_hash='1' * 64,
            )
            old = UserFactory()
        finally:
            MigrationExecutor(connection).migrate(after)
        while_disabled = UserFactory()
        assert not account_has_untainted_privilege_history(account_id=while_disabled.pk)
        enable_test_capture()
        new = UserFactory()
        assert not account_born_after_reference(account_id=old.pk, reference_id=reference.pk)
        assert not account_has_untainted_privilege_history(account_id=old.pk)
        assert account_born_after_reference(account_id=new.pk, reference_id=reference.pk)
        assert account_has_untainted_privilege_history(account_id=new.pk)
