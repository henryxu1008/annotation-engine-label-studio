from uuid import uuid4

from classification_disagreements.tests import test_arbitration as arbitration
from review_batches.models import ReviewBatch


class HandoffFixtures:
    make_source = arbitration.TestArbitration.make_source
    set_answer = arbitration.TestArbitration.set_answer
    preview = arbitration.TestArbitration.preview
    body = arbitration.TestArbitration.body
    create = arbitration.TestArbitration.create
    new_batch = arbitration.TestArbitration.new_batch
    dispatch_body = arbitration.TestArbitration.dispatch_body
    dispatch = arbitration.TestArbitration.dispatch
    pending = arbitration.TestArbitration.pending
    command_body = arbitration.TestArbitration.command_body
    command = arbitration.TestArbitration.command

    def setUp(self):
        arbitration.TestArbitration.setUp(self)
        mode = self.settings(ANNOTATION_REVIEW_BATCH_MODES={f"{self.org.pk}:{self.project.pk}": "fork_review"})
        mode.enable()
        self.addCleanup(mode.disable)
        self.analysis = self.new_batch()
        self.review_base = f"/api/projects/{self.project.pk}/review-batches/"
        self.batch = self.review_batch()
        self.review_batch_id = self.batch.pk
        self.source_id = self.first.pk
        self.handoff_url = f"{self.review_base}{self.batch.pk}/sources/{self.source_id}/arbitration-handoffs/"
        self.context_url = self.handoff_url.replace("arbitration-handoffs/", "arbitration-context/")

    def review_batch(self, *, rate=100, prioritized=True):
        args = {"name": "Handoff 合成交接", "rate": rate, "seed": "handoff"}
        if prioritized:
            args["priority"] = {"kind": "classification_conflict", "batch_id": self.analysis.pk,
                                "manifest_hash": self.analysis.manifest_hash}
        preview = self.client.post(self.review_base + "preview/", args, format="json")
        assert preview.status_code == 200, preview.data
        created = self.client.post(self.review_base, {**args, "expected_plan_hash": preview.data["plan_hash"]},
                                   format="json", HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        assert created.status_code == 201, created.data
        return ReviewBatch.objects.get(pk=created.data["id"])

    def handoff_body(self, target=None):
        read = self.client.get(self.context_url)
        assert read.status_code == 200, read.data
        data = read.data
        return {
            "mode": "link_existing" if target else "dispatch",
            "expected_plan_hash": data["expected_plan_hash"],
            "expected_input_set_hash": data["expected_input_set_hash"],
            **({"arbitration_id": target.pk, "expected_arbitration_version": target.version}
               if target else {"assignee_id": self.arbiter.pk}),
        }

    def handoff(self, body=None, key=None):
        return self.client.post(self.handoff_url, body or self.handoff_body(), format="json",
                                HTTP_IDEMPOTENCY_KEY=str(key or uuid4()))

    def handoff_detail(self, identifier):
        return self.client.get(f"{self.review_base}{self.review_batch_id}/arbitration-handoffs/{identifier}/")
