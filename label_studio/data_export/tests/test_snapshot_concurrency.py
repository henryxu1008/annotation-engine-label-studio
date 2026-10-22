import tempfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event
from unittest.mock import patch

from access_control.identity import LOCAL_IDENTITY_SOURCE, Principal
from data_export import approved as base
from data_export import snapshots as service
from data_export.approved_models import ApprovedDatasetRelease as Release
from data_export.approved_models import ApprovedDatasetReleaseAttempt as Attempt
from data_export.snapshot_models import ReleaseSnapshot as Snapshot
from data_export.tests import test_approved_exports as fixtures
from django.db import close_old_connections
from django.test import TransactionTestCase, override_settings
from projects.models import ProjectMember
from users.models import User
from users.tests.factories import UserFactory


class TestSnapshotConcurrency(TransactionTestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix="issue80-race-")
        self.addCleanup(self.root.cleanup)
        self.organization, self.owner, self.project, body, self.policy = (
            fixtures.TestApprovedExportConcurrency.fixture(self)
        )
        self.settings_override = override_settings(
            APPROVED_EXPORT_POLICY=self.policy, APPROVED_EXPORT_ROOT=self.root.name
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.principal = self.principal_for(self.owner)
        with patch.object(base, "queue_available", return_value=True), patch.object(base, "dispatch"):
            self.release = base.freeze(self.project, self.principal, self.owner, body, "concurrent-release")
        self.release.state, self.release.attempt_number = "queued", 1
        self.release.save(update_fields=["state", "attempt_number"])
        attempt = Attempt.objects.create(release=self.release, number=1)
        base.generate(str(self.release.pk), str(attempt.token))
        self.release.refresh_from_db()
        assert self.release.state == "ready", self.release.error_code

    @staticmethod
    def principal_for(user):
        return Principal(f"{LOCAL_IDENTITY_SOURCE}:{user.pk}", LOCAL_IDENTITY_SOURCE, user.email, user.pk)

    @staticmethod
    def isolated(call):
        close_old_connections()
        try:
            return call()
        finally:
            close_old_connections()

    def test_two_connections_create_one_snapshot_and_duplicate_worker_claim(self):
        def capture():
            actor = User.objects.get(pk=self.owner.pk)
            return str(
                service.create(
                    Release.objects.get(pk=self.release.pk), self.principal_for(actor), actor, "same-key", {}
                ).pk
            )

        with patch.object(service, "dispatch"), ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: self.isolated(capture), range(2)))
        assert len(set(ids)) == Snapshot.objects.count() == 1
        snapshot = Snapshot.objects.get()
        Snapshot.objects.filter(pk=snapshot.pk).update(state="queued")
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(
                pool.map(
                    lambda _: self.isolated(lambda: service.generate(str(snapshot.pk), str(snapshot.token))), range(2)
                )
            )
        snapshot.refresh_from_db()
        assert snapshot.state == "ready", snapshot.error_code
        assert len(list((service.directory(snapshot).parent).iterdir())) == 1

    def test_publication_fences_snapshot_and_receipt_creator_disable(self):
        administrator = UserFactory(is_staff=True, is_superuser=True)
        actor = UserFactory(active_organization=self.organization)
        ProjectMember.objects.create(project=self.project, user=actor, role="manager")
        with patch.object(service, "dispatch"):
            snapshot = service.create(self.release, self.principal_for(actor), actor, "authority-race", {})
        Snapshot.objects.filter(pk=snapshot.pk).update(state="queued")
        entered, release_fence = Event(), Event()
        original = base.audit

        def audit(project, principal, user, phase, **kwargs):
            if phase == "snapshot_finalize":
                entered.set()
                assert release_fence.wait(10)
            return original(project, principal, user, phase, **kwargs)

        def revoke(user_id):
            return User.objects.filter(pk=user_id).update(is_active=False, session_actor=administrator)

        with patch.object(base, "audit", side_effect=audit), ThreadPoolExecutor(max_workers=3) as pool:
            writer = pool.submit(self.isolated, lambda: service.generate(str(snapshot.pk), str(snapshot.token)))
            assert entered.wait(10)
            revocations = [
                pool.submit(self.isolated, lambda key=key: revoke(key)) for key in (actor.pk, self.owner.pk)
            ]
            try:
                for revocation in revocations:
                    with self.assertRaises(TimeoutError):
                        revocation.result(timeout=0.2)
            finally:
                release_fence.set()
            writer.result(timeout=15)
            for revocation in revocations:
                assert revocation.result(timeout=15) == 1
        snapshot.refresh_from_db()
        assert snapshot.state == "ready", snapshot.error_code
        with self.assertRaises(Exception):
            service.identity(snapshot, self.project)
