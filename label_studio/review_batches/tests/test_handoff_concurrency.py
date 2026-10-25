import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from classification_disagreements.models import ArbitrationTask
from classification_disagreements.tests import test_arbitration as arbitration
from classification_disagreements.tests import test_arbitration_concurrency as concurrent
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import Project, ProjectMember
from rest_framework.test import APIClient
from review_batches.models import ReviewArbitrationHandoff

from .handoff_fixtures import HandoffFixtures


@override_settings(
    ANNOTATION_REVIEW_BATCHES_ENABLED=True,
    ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=True,
    ANNOTATION_CLASSIFICATION_ARBITRATION_ENABLED=True,
    ANNOTATION_REVIEW_ARBITRATION_HANDOFF_ENABLED=True,
)
class TestHandoffConcurrency(HandoffFixtures, TransactionTestCase):
    client_class = APIClient
    client_for = concurrent.TestArbitrationConcurrency.client_for
    post = concurrent.TestArbitrationConcurrency.post
    competing = concurrent.TestArbitrationConcurrency.competing

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Independent PostgreSQL connections required")
        arbitration.TestArbitration.setUpTestData.__func__(self)
        HandoffFixtures.setUp(self)

    def test_same_key_commits_one_task_and_one_handoff(self):
        body, key = self.handoff_body(), uuid4()
        responses = self.competing(self.manager, self.manager, self.handoff_url, self.handoff_url,
                                    body, body, key, key)
        assert [r.status_code for r in responses] == [201, 200]
        assert responses[0].data["id"] == responses[1].data["id"]
        assert ArbitrationTask.objects.count() == ReviewArbitrationHandoff.objects.count() == 1

    def test_different_keys_same_dispatch_input_do_not_duplicate_task(self):
        body = self.handoff_body()
        responses = self.competing(self.manager, self.manager, self.handoff_url, self.handoff_url,
                                    body, body, uuid4(), uuid4())
        assert [r.status_code for r in responses] == [201, 409]
        assert ArbitrationTask.objects.count() == ReviewArbitrationHandoff.objects.count() == 1

    def test_same_key_different_body_cannot_replace_assignee(self):
        body, key = self.handoff_body(), uuid4()
        other = {**body, "assignee_id": self.other_arbiter.pk}
        responses = self.competing(self.manager, self.manager, self.handoff_url, self.handoff_url,
                                    body, other, key, key)
        assert [r.status_code for r in responses] == [201, 409]
        assert ArbitrationTask.objects.get().assignee_id_frozen == self.arbiter.pk

    def test_different_link_keys_do_not_duplicate_binding(self):
        target = self.pending()
        body = self.handoff_body(target)
        responses = self.competing(self.manager, self.manager, self.handoff_url, self.handoff_url,
                                    body, body, uuid4(), uuid4())
        assert [r.status_code for r in responses] == [201, 409]
        assert ReviewArbitrationHandoff.objects.count() == 1

    def test_normal_dispatch_wins_before_handoff_without_partial_link(self):
        body = self.handoff_body()
        normal = self.dispatch_body(self.analysis)
        responses = self.competing(self.manager, self.manager, self.arb_base, self.handoff_url,
                                    normal, body, uuid4(), uuid4())
        assert [r.status_code for r in responses] == [201, 409]
        assert ArbitrationTask.objects.count() == 1 and not ReviewArbitrationHandoff.objects.exists()

    def test_handoff_wins_before_normal_dispatch(self):
        body = self.handoff_body()
        normal = self.dispatch_body(self.analysis)
        responses = self.competing(self.manager, self.manager, self.handoff_url, self.arb_base,
                                    body, normal, uuid4(), uuid4())
        assert [r.status_code for r in responses] == [201, 409]
        assert ArbitrationTask.objects.count() == ReviewArbitrationHandoff.objects.count() == 1

    def test_decision_before_link_requires_fresh_version(self):
        target = self.pending()
        body = self.handoff_body(target)
        responses = self.competing(
            self.arbiter, self.manager, f"{self.arb_base}{target.pk}/decision/", self.handoff_url,
            self.command_body(target), body, uuid4(), uuid4(),
        )
        assert [r.status_code for r in responses] == [201, 409]
        assert not ReviewArbitrationHandoff.objects.exists()

    def test_link_before_decision_preserves_version_at_commit(self):
        target = self.pending()
        body = self.handoff_body(target)
        responses = self.competing(
            self.manager, self.arbiter, self.handoff_url, f"{self.arb_base}{target.pk}/decision/",
            body, self.command_body(target), uuid4(), uuid4(),
        )
        assert [r.status_code for r in responses] == [201, 201]
        record = ReviewArbitrationHandoff.objects.get()
        assert record.origin["target"]["version_at_link"] == 1
        assert self.handoff_detail(record.pk).data["arbitration_status"] == "resolved"

    def test_cancel_before_link_rejects_stale_target(self):
        target = self.pending()
        body = self.handoff_body(target)
        responses = self.competing(
            self.manager, self.manager, f"{self.arb_base}{target.pk}/cancel/", self.handoff_url,
            self.command_body(target, cancel=True), body, uuid4(), uuid4(), cancel_first=True,
        )
        assert [r.status_code for r in responses] == [201, 409]
        assert not ReviewArbitrationHandoff.objects.exists()

    def test_link_before_cancel_keeps_historical_association(self):
        target = self.pending()
        body = self.handoff_body(target)
        responses = self.competing(
            self.manager, self.manager, self.handoff_url, f"{self.arb_base}{target.pk}/cancel/",
            body, self.command_body(target, cancel=True), uuid4(), uuid4(),
        )
        assert [r.status_code for r in responses] == [201, 201]
        record = ReviewArbitrationHandoff.objects.get()
        assert record.origin["target"]["status_at_link"] == "pending"
        assert self.handoff_detail(record.pk).data["arbitration_status"] == "cancelled"

    def ordered_source_change(self, mutate):
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
        body = self.handoff_body()
        client = self.client_for(self.manager)

        def first():
            close_old_connections()
            try:
                with transaction.atomic():
                    Project.objects.select_for_update().get(pk=self.project.pk)
                    mutate()
                    entered.set()
                    assert release.wait(20)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            owner = pool.submit(first)
            try:
                assert entered.wait(20)
                writer = pool.submit(self.post, client, self.handoff_url, body, uuid4(), attempted)
                assert attempted.wait(20)
            finally:
                release.set()
            owner.result(25)
            return writer.result(25)

    def test_revocation_first_denies_without_orphan_task(self):
        response = self.ordered_source_change(
            lambda: OrganizationMember.objects.filter(organization=self.org, user=self.manager).update(deleted_at=timezone.now())
        )
        assert response.status_code in (403, 404)
        assert not ArbitrationTask.objects.exists() and not ReviewArbitrationHandoff.objects.exists()

    def test_new_revision_first_denies_stale_review_pin(self):
        response = self.ordered_source_change(
            lambda: self.make_source(assignment=self.first.assignment, revision=2)
        )
        assert response.status_code == 409
        assert not ArbitrationTask.objects.exists() and not ReviewArbitrationHandoff.objects.exists()
