import sqlite3
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase as PlainTestCase
from unittest import skipUnless
from uuid import uuid4

import pytest
from classification_disagreements.blind_provenance import (
    account_born_after_reference,
    account_has_never_been_assigned,
    account_has_untainted_privilege_history,
)
from classification_disagreements.models import (
    BlindCaptureCertificate,
    BlindCaptureEpoch,
    BlindCaptureIdentity,
    BlindPrivilegeHistory,
    BlindProvenanceEvent,
    BlindProvenanceFence,
    BlindRetiredEvidence,
    ClassificationGoldEvidence,
)
from django.core.management import call_command
from django.db import DatabaseError, close_old_connections, connection, connections, transaction
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


@skipUnless(connection.vendor == 'sqlite', 'SQLite WAL locking test')
class TestBlindCaptureSqliteWal(PlainTestCase):
    @pytest.fixture(autouse=True)
    def _allow_isolated_database(self, django_db_blocker):
        with django_db_blocker.unblock():
            yield

    def test_capture_reader_fences_revoke_in_wal_mode(self):
        from classification_disagreements.blind_provenance import lock_capture_epoch_for_read

        # Use an isolated file because Django's in-memory SQLite test database
        # cannot use WAL. No application or development data is touched.
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.sqlite3'
            alias = f'blind_wal_{uuid4().hex}'
            connections.databases[alias] = {**connections.databases['default'], 'NAME': str(path)}
            database = connections[alias]
            try:
                with database.cursor() as cursor:
                    cursor.execute('PRAGMA journal_mode=WAL')
                    assert cursor.fetchone()[0].lower() == 'wal'
                with database.schema_editor() as editor:
                    editor.create_model(BlindCaptureEpoch)
                BlindCaptureEpoch.objects.using(alias).create(pk=1, generation=1, enabled=True)
                entered, release = Event(), Event()

                def read():
                    try:
                        with transaction.atomic(using=alias):
                            lock_capture_epoch_for_read(using=alias)
                            entered.set()
                            if not release.wait(10):
                                raise AssertionError('capture reader was not released')
                    finally:
                        connections[alias].close()

                with ThreadPoolExecutor(max_workers=1) as pool:
                    reader = pool.submit(read)
                    try:
                        assert entered.wait(10)
                        with sqlite3.connect(path, timeout=0.2) as writer:
                            with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                                writer.execute('DELETE FROM classification_disagreements_blindcaptureepoch WHERE id = 1')
                    finally:
                        release.set()
                    reader.result(timeout=20)
                with sqlite3.connect(path) as writer:
                    writer.execute('DELETE FROM classification_disagreements_blindcaptureepoch WHERE id = 1')
                    assert writer.total_changes == 1
            finally:
                database.close()
                del connections.databases[alias]


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

    def test_revoked_account_history_cannot_be_replayed(self):
        reference = self.reference()
        member = UserFactory()
        history = BlindPrivilegeHistory.objects.get(account_id=member.pk)
        assert account_has_untainted_privilege_history(account_id=member.pk)
        assert account_born_after_reference(account_id=member.pk, reference_id=reference.pk)
        history.delete()
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindPrivilegeHistory.objects.create(account_id=member.pk, elevated=False,
                                                   had_assignment=False, first_assignment_id=None)
        assert not account_has_untainted_privilege_history(account_id=member.pk)

    def test_revoked_account_event_cannot_be_replayed(self):
        reference = self.reference()
        member = UserFactory()
        event = BlindProvenanceEvent.objects.get(kind='account', subject_id=member.pk)
        sequence = event.sequence
        event.delete()
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindProvenanceEvent.objects.create(kind='account', subject_id=member.pk, sequence=sequence)
        assert not account_born_after_reference(account_id=member.pk, reference_id=reference.pk)

    def test_revoked_reference_event_cannot_be_replayed(self):
        reference = self.reference()
        member = UserFactory()
        event = BlindProvenanceEvent.objects.get(kind='reference', subject_id=reference.pk)
        sequence = event.sequence
        assert account_born_after_reference(account_id=member.pk, reference_id=reference.pk)
        event.delete()
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindProvenanceEvent.objects.create(kind='reference', subject_id=reference.pk, sequence=sequence)
        assert not account_born_after_reference(account_id=member.pk, reference_id=reference.pk)

    def test_evidence_retirement_is_monotonic_and_transactional(self):
        member = UserFactory()
        event = BlindProvenanceEvent.objects.get(kind='account', subject_id=member.pk)
        event_id = event.pk
        state = BlindRetiredEvidence.objects.get(kind='account', subject_id=member.pk)
        assert not state.retired
        with self.assertRaises(ValueError), transaction.atomic():
            BlindProvenanceEvent.objects.filter(pk=event_id).delete()
            assert BlindRetiredEvidence.objects.get(pk=state.pk).retired
            raise ValueError('rollback')
        assert not BlindRetiredEvidence.objects.get(pk=state.pk).retired
        assert BlindProvenanceEvent.objects.filter(pk=event_id).exists()
        BlindProvenanceEvent.objects.filter(pk=event_id).delete()
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindRetiredEvidence.objects.filter(pk=state.pk).update(retired=False)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindRetiredEvidence.objects.filter(pk=state.pk).update(id=state.pk + 100000)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindRetiredEvidence.objects.filter(pk=state.pk).delete()
        if connection.vendor == 'sqlite':
            table = connection.ops.quote_name(BlindRetiredEvidence._meta.db_table)
            with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(f'INSERT OR REPLACE INTO {table} (id, kind, subject_id, retired) VALUES (%s, %s, %s, %s)',
                               [state.pk, 'account', member.pk, False])

    def test_revoked_evidence_does_not_block_reused_ordinary_account(self):
        member = UserFactory()
        account_id = member.pk
        User.objects.filter(pk=account_id).delete()
        BlindCaptureCertificate.objects.filter(account_id=account_id).delete()
        BlindPrivilegeHistory.objects.filter(account_id=account_id).delete()
        BlindProvenanceEvent.objects.filter(kind='account', subject_id=account_id).delete()
        replacement = UserFactory(id=account_id)
        assert replacement.pk == account_id
        assert not account_has_untainted_privilege_history(account_id=account_id)
        assert BlindPrivilegeHistory.objects.get(account_id=account_id).elevated
        assert not BlindProvenanceEvent.objects.filter(kind='account', subject_id=account_id).exists()

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

    def test_revoked_certificate_cannot_be_reinserted_or_inherited_by_reused_account(self):
        member = UserFactory()
        account_id = member.pk
        certificate = BlindCaptureCertificate.objects.get(account_id=account_id)
        generation = certificate.generation
        assert account_has_untainted_privilege_history(account_id=account_id)
        certificate.delete()
        assert not account_has_untainted_privilege_history(account_id=account_id)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindCaptureCertificate.objects.create(account_id=account_id, generation=generation)
        User.objects.filter(pk=account_id).delete()
        BlindPrivilegeHistory.objects.filter(account_id=account_id).delete()
        BlindProvenanceEvent.objects.filter(kind='account', subject_id=account_id).delete()
        replacement = UserFactory(id=account_id)
        assert replacement.pk == account_id
        assert not BlindCaptureCertificate.objects.filter(account_id=account_id).exists()
        assert not account_has_untainted_privilege_history(account_id=account_id)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindCaptureCertificate.objects.create(account_id=account_id, generation=generation)

    def test_account_born_outside_capture_cannot_receive_late_certificate(self):
        BlindCaptureEpoch.objects.filter(pk=1).update(enabled=False)
        member = UserFactory()
        enable_test_capture()
        generation = BlindCaptureEpoch.objects.get(pk=1).generation
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindCaptureCertificate.objects.create(account_id=member.pk, generation=generation)
        BlindProvenanceEvent.objects.filter(kind='account', subject_id=member.pk).delete()
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindCaptureCertificate.objects.create(account_id=member.pk, generation=generation)
        assert not account_has_untainted_privilege_history(account_id=member.pk)

    def test_capture_identity_is_immutable_and_certificate_revocation_rolls_back(self):
        member = UserFactory()
        identity = BlindCaptureIdentity.objects.get(account_id=member.pk)
        with self.assertRaises(DatabaseError), transaction.atomic():
            identity.delete()
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindCaptureIdentity.objects.filter(pk=member.pk).update(account_id=member.pk + 100000)
        with self.assertRaises(ValueError), transaction.atomic():
            BlindCaptureCertificate.objects.filter(account_id=member.pk).delete()
            raise ValueError('roll back revocation')
        assert BlindCaptureCertificate.objects.filter(account_id=member.pk).exists()
        assert account_has_untainted_privilege_history(account_id=member.pk)

    def test_aborted_account_birth_does_not_retire_the_identifier(self):
        with self.assertRaises(ValueError), transaction.atomic():
            candidate = UserFactory()
            account_id = candidate.pk
            assert BlindCaptureIdentity.objects.filter(pk=account_id).exists()
            raise ValueError('roll back account birth')
        assert not BlindCaptureIdentity.objects.filter(pk=account_id).exists()
        member = UserFactory(id=account_id)
        assert BlindCaptureCertificate.objects.filter(account_id=account_id).exists()
        assert account_has_untainted_privilege_history(account_id=member.pk)


class SyntheticTransactionTestCase(TransactionTestCase):
    def _fixture_teardown(self):
        # The test database contains legacy tables no longer represented by
        # models; PostgreSQL requires them in the same TRUNCATE operation.
        if connection.vendor != 'sqlite':
            call_command('flush', verbosity=0, interactive=False, database='default',
                         reset_sequences=False, allow_cascade=True)
            return
        # SQLite flush uses DELETE, so immutable evidence triggers reject it.
        # Temporarily remove only those guards in Django's isolated in-memory
        # test database; the production database and its triggers are untouched.
        name = str(connection.settings_dict['NAME'])
        if name != ':memory:' and not (name.startswith('file:memorydb_') and 'mode=memory' in name):
            raise RuntimeError('Synthetic flush requires an in-memory SQLite test database.')
        with connection.cursor() as cursor:
            cursor.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND sql IS NOT NULL")
            guards = [(trigger, sql) for trigger, sql in cursor.fetchall()
                      if 'BEFORE DELETE' in sql.upper() and 'RAISE' in sql.upper()]
            for trigger, _ in guards:
                cursor.execute(f'DROP TRIGGER {connection.ops.quote_name(trigger)}')
        try:
            call_command('flush', verbosity=0, interactive=False, database='default',
                         reset_sequences=False, allow_cascade=True)
        finally:
            with connection.cursor() as cursor:
                for _, sql in guards:
                    cursor.execute(sql)


@skipUnless(connection.vendor == 'sqlite', 'SQLite test database flush guard')
class TestCaptureIdentityTestDatabaseFlush(TransactionTestCase):
    def test_a_django_can_flush_after_creating_an_account(self):
        member = UserFactory()
        assert BlindCaptureIdentity.objects.filter(pk=member.pk).exists()

    def test_b_flush_restores_the_identity_delete_guard(self):
        member = UserFactory()
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindCaptureIdentity.objects.filter(pk=member.pk).delete()


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL transaction-lock test')
class TestBlindProvenanceConcurrency(SyntheticTransactionTestCase):
    def setUp(self):
        enable_test_capture()

    def test_retirement_fences_concurrent_replay_with_old_snapshot(self):
        for kind in ('account', 'reference', 'privilege'):
            with self.subTest(kind=kind):
                member = UserFactory()
                if kind == 'account':
                    original = BlindProvenanceEvent.objects.get(kind='account', subject_id=member.pk)
                    values = {'kind': 'account', 'subject_id': member.pk, 'sequence': original.sequence}
                    model = BlindProvenanceEvent
                    subject_id = member.pk
                elif kind == 'reference':
                    project = ProjectFactory(created_by=member)
                    reference = ClassificationGoldEvidence.objects.create(
                        project=project, created_by=member, actor_id_frozen=member.pk,
                        principal='local:test', principal_source='local', kind='reference', idempotency_key=uuid4(),
                        request_hash='0' * 64, manifest={}, manifest_hash='1' * 64,
                    )
                    original = BlindProvenanceEvent.objects.get(kind='reference', subject_id=reference.pk)
                    values = {'kind': 'reference', 'subject_id': reference.pk, 'sequence': original.sequence}
                    model = BlindProvenanceEvent
                    subject_id = reference.pk
                else:
                    original = BlindPrivilegeHistory.objects.get(account_id=member.pk)
                    values = {'account_id': member.pk, 'elevated': False,
                              'had_assignment': False, 'first_assignment_id': None}
                    model = BlindPrivilegeHistory
                    subject_id = member.pk
                snapshot, deleted, attempting, release = Event(), Event(), Event(), Event()

                def replay():
                    close_old_connections()
                    try:
                        with transaction.atomic():
                            with connection.cursor() as cursor:
                                cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                            assert model.objects.filter(pk=original.pk).exists()
                            snapshot.set()
                            if not deleted.wait(15):
                                raise AssertionError('revocation did not start')
                            attempting.set()
                            model.objects.create(**values)
                        return 'replayed'
                    except DatabaseError:
                        return 'denied'
                    finally:
                        close_old_connections()

                def revoke():
                    close_old_connections()
                    try:
                        with transaction.atomic():
                            model.objects.filter(pk=original.pk).delete()
                            deleted.set()
                            if not release.wait(15):
                                raise AssertionError('revocation was not released')
                    finally:
                        close_old_connections()

                with ThreadPoolExecutor(max_workers=2) as pool:
                    inserting = pool.submit(replay)
                    try:
                        assert snapshot.wait(10)
                        removing = pool.submit(revoke)
                        assert deleted.wait(10) and attempting.wait(10)
                        with self.assertRaises(TimeoutError):
                            inserting.result(timeout=0.3)
                    finally:
                        release.set()
                    removing.result(timeout=20)
                    assert inserting.result(timeout=20) == 'denied'
                assert not model.objects.filter(pk=original.pk).exists()
                assert BlindRetiredEvidence.objects.get(kind=kind, subject_id=subject_id).retired

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
    @skipUnless(connection.vendor == 'postgresql', 'PostgreSQL concurrent index recovery only')
    def test_exposure_index_migration_recovers_invalid_concurrent_index(self):
        from django.conf import settings

        with connection.cursor() as cursor:
            cursor.execute('SELECT current_database(), rolsuper FROM pg_roles WHERE rolname = current_user')
            database, superuser = cursor.fetchone()
        if not settings.TEST_ENVIRONMENT or not database.startswith('test_') or not superuser:
            self.skipTest('Requires an isolated PostgreSQL test database and test superuser.')
        executor = MigrationExecutor(connection)
        after = executor.loader.graph.leaf_nodes()
        executor.migrate([('classification_disagreements', '0053_blind_completion_state_fence')])
        name = 'blind_file_read_scope_idx'
        try:
            with connection.cursor() as cursor:
                cursor.execute('CREATE INDEX blind_file_read_scope_idx ON '
                               'classification_disagreements_blindprojectfileexposure (account_id, generation)')
                # Emulate the catalog state left by an interrupted concurrent
                # build, only in the disposable test database.
                cursor.execute('UPDATE pg_index SET indisvalid = false WHERE indexrelid = to_regclass(%s)', [name])
            MigrationExecutor(connection).migrate(after)
            with connection.cursor() as cursor:
                cursor.execute('SELECT indisvalid, indisready FROM pg_index WHERE indexrelid = to_regclass(%s)', [name])
                assert cursor.fetchone() == (True, True)
        finally:
            MigrationExecutor(connection).migrate(after)

    @skipUnless(connection.vendor in ('postgresql', 'sqlite'), 'Supported blind read databases only')
    def test_exposure_index_migration_resumes_after_existing_matching_index(self):
        from classification_disagreements.models import BlindProjectFileExposure, BlindTextLengthExposure

        executor = MigrationExecutor(connection)
        after = executor.loader.graph.leaf_nodes()
        executor.migrate([('classification_disagreements', '0053_blind_completion_state_fence')])
        q = connection.ops.quote_name
        table = q(BlindProjectFileExposure._meta.db_table)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'CREATE INDEX {q("blind_file_read_scope_idx")} ON {table} (account_id, generation)')
            MigrationExecutor(connection).migrate(after)
            with connection.cursor() as cursor:
                file_indexes = connection.introspection.get_constraints(cursor, BlindProjectFileExposure._meta.db_table)
                text_indexes = connection.introspection.get_constraints(cursor, BlindTextLengthExposure._meta.db_table)
            assert file_indexes['blind_file_read_scope_idx']['columns'] == ['account_id', 'generation']
            assert text_indexes['blind_text_read_scope_idx']['columns'] == [
                'account_id', 'generation', 'byte_length', 'assignment_id',
            ]
        finally:
            MigrationExecutor(connection).migrate(after)

    @skipUnless(connection.vendor in ('postgresql', 'sqlite'), 'Supported blind read databases only')
    def test_exposure_index_migration_refuses_a_conflicting_index(self):
        from classification_disagreements.models import BlindProjectFileExposure

        executor = MigrationExecutor(connection)
        after = executor.loader.graph.leaf_nodes()
        executor.migrate([('classification_disagreements', '0053_blind_completion_state_fence')])
        q = connection.ops.quote_name
        table = q(BlindProjectFileExposure._meta.db_table)
        name = q('blind_file_read_scope_idx')
        try:
            for definition in ('(generation, account_id)', '(account_id, generation DESC)',
                               '(account_id, generation)WHERE generation > 0'):
                with self.subTest(definition=definition):
                    with connection.cursor() as cursor:
                        cursor.execute(f'CREATE INDEX {name} ON {table} {definition}')
                    with self.assertRaisesRegex(RuntimeError, 'does not match'):
                        MigrationExecutor(connection).migrate(after)
                    with connection.cursor() as cursor:
                        existing = connection.introspection.get_constraints(cursor, BlindProjectFileExposure._meta.db_table)
                        assert 'blind_file_read_scope_idx' in existing
                        cursor.execute(f'DROP INDEX {name}')
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f'DROP INDEX IF EXISTS {name}')
            MigrationExecutor(connection).migrate(after)

    def test_evidence_state_migration_backfills_and_preserves_revocation(self):
        BlindCaptureEpoch.objects.get_or_create(pk=1)
        executor = MigrationExecutor(connection)
        after = executor.loader.graph.leaf_nodes()
        executor.migrate([('classification_disagreements', '0050_blind_completion_submission_fence')])
        try:
            member = UserFactory()
            account_id = member.pk
            assert BlindProvenanceEvent.objects.filter(kind='account', subject_id=account_id).exists()
            assert BlindPrivilegeHistory.objects.filter(account_id=account_id).exists()
        finally:
            MigrationExecutor(connection).migrate(after)
        states = dict(BlindRetiredEvidence.objects.filter(subject_id=account_id, kind__in=('account', 'privilege'))
                      .values_list('kind', 'retired'))
        assert states == {'account': False, 'privilege': False}
        assert not BlindCaptureEpoch.objects.get(pk=1).enabled
        BlindProvenanceEvent.objects.filter(kind='account', subject_id=account_id).delete()
        assert BlindRetiredEvidence.objects.get(kind='account', subject_id=account_id).retired
        with self.assertRaisesRegex(RuntimeError, 'cannot be discarded'):
            MigrationExecutor(connection).migrate([('classification_disagreements', '0050_blind_completion_submission_fence')])
        assert BlindRetiredEvidence.objects.get(kind='account', subject_id=account_id).retired

    def test_capture_identity_migration_backfills_live_and_retained_accounts(self):
        BlindCaptureEpoch.objects.get_or_create(pk=1)
        executor = MigrationExecutor(connection)
        after = executor.loader.graph.leaf_nodes()
        executor.migrate([('classification_disagreements', '0048_blind_read_attestation')])
        try:
            live = UserFactory()
            live_id = live.pk
            BlindPrivilegeHistory.objects.filter(account_id=live_id).delete()
            BlindProvenanceEvent.objects.filter(kind='account', subject_id=live_id).delete()
            event_only = UserFactory()
            event_id = event_only.pk
            User.objects.filter(pk=event_id).delete()
            BlindPrivilegeHistory.objects.filter(account_id=event_id).delete()
            history_only = UserFactory()
            history_id = history_only.pk
            User.objects.filter(pk=history_id).delete()
            BlindProvenanceEvent.objects.filter(kind='account', subject_id=history_id).delete()
        finally:
            MigrationExecutor(connection).migrate(after)
        assert set(BlindCaptureIdentity.objects.filter(pk__in=[live_id, event_id, history_id])
                   .values_list('pk', flat=True)) == {live_id, event_id, history_id}
        assert not BlindCaptureEpoch.objects.get(pk=1).enabled
        enable_test_capture()
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindCaptureCertificate.objects.create(account_id=live_id,
                generation=BlindCaptureEpoch.objects.get(pk=1).generation)

    def test_capture_identity_migration_refuses_to_lose_orphaned_identity(self):
        member = UserFactory()
        account_id = member.pk
        User.objects.filter(pk=account_id).delete()
        BlindPrivilegeHistory.objects.filter(account_id=account_id).delete()
        BlindProvenanceEvent.objects.filter(kind='account', subject_id=account_id).delete()
        BlindCaptureCertificate.objects.filter(account_id=account_id).delete()
        assert BlindCaptureIdentity.objects.filter(pk=account_id).exists()
        with self.assertRaisesRegex(RuntimeError, 'cannot be discarded'):
            MigrationExecutor(connection).migrate([('classification_disagreements', '0048_blind_read_attestation')])
        assert BlindCaptureIdentity.objects.filter(pk=account_id).exists()

    def test_ordinary_binding_migration_preserves_existing_binding_and_guards(self):
        from classification_disagreements.models import BlindReadBinding

        executor = MigrationExecutor(connection)
        after = executor.loader.graph.leaf_nodes()
        executor.migrate([('classification_disagreements', '0046_blind_read_binding')])
        try:
            binding = BlindReadBinding.objects.create(
                assignment_id_frozen=9876543, task_id_frozen=1, project_id_frozen=1, actor_id_frozen=1,
                decision_id_frozen=1, source_id_frozen=9876543, source_task_id_frozen=1,
                decision_request_hash='1' * 64, purpose='ordinary', reference_id_frozen=None,
                reference_hash=None, proof_hash=None, binding_hash='2' * 64,
            )
            binding_id = binding.pk
        finally:
            MigrationExecutor(connection).migrate(after)
        assert BlindReadBinding.objects.get(pk=binding_id).binding_hash == '2' * 64
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindReadBinding.objects.filter(pk=binding_id).update(binding_hash='3' * 64)
        with self.assertRaises(DatabaseError), transaction.atomic():
            BlindReadBinding.objects.filter(pk=binding_id).delete()

    def test_reference_identity_migration_backfills_live_and_retired_evidence(self):
        from classification_disagreements.models import BlindEvidenceIdentity, BlindQualityAssignment

        executor = MigrationExecutor(connection)
        after = executor.loader.graph.leaf_nodes()
        executor.migrate([('classification_disagreements', '0043_blind_exposure_row_fence')])
        try:
            owner = UserFactory()
            member = UserFactory()
            project = ProjectFactory(created_by=owner)

            def reference():
                return ClassificationGoldEvidence.objects.create(
                    project=project, created_by=owner, actor_id_frozen=owner.pk,
                    principal='local:test', principal_source='local', kind='reference',
                    idempotency_key=uuid4(), request_hash='0' * 64, manifest={}, manifest_hash='1' * 64,
                )

            live = reference()
            provenance_only = reference()
            provenance_id = provenance_only.pk
            provenance_only.delete()
            retired = reference()
            retired_id = retired.pk
            task = TaskFactory(project=project)
            assignment = TaskAssignment.objects.create(task=task, project=project, assignee=member, assigned_by=owner)
            BlindQualityAssignment.objects.create(
                assignment_id=assignment.pk, task_id=task.pk, project_id=project.pk, assignee_id=member.pk,
                reference_id=retired_id, reference_hash=retired.manifest_hash,
            )
            retired.delete()
            BlindProvenanceEvent.objects.filter(kind='reference', subject_id=retired_id).delete()
        finally:
            MigrationExecutor(connection).migrate(after)
        assert set(BlindEvidenceIdentity.objects.filter(pk__in=[live.pk, provenance_id, retired_id])
                   .values_list('pk', flat=True)) == {live.pk, provenance_id, retired_id}

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
