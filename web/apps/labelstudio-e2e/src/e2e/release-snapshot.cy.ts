/// <reference types="cypress" />

for (const locale of ["en-US", "zh-CN"]) {
  describe(`${locale} ReleaseManifest source snapshots`, () => {
    let fixture: any;
    const zh = locale === "zh-CN";
    before(() => cy.readFile(".release-snapshot-e2e.json").then((value) => { fixture = value; }));
    beforeEach(() => {
      Cypress.env("loginLocale", locale); cy.task("setSnapshotManagerEnabled", true);
      cy.intercept({ method: "POST", pathname: /^\/api\/projects\/\d+\/approved-dataset-releases\/?$/ }).as("createBatch");
    });
    afterEach(() => cy.task("setSnapshotManagerEnabled", true));

    const waitForCreatedBatch = () => {
      cy.wait("@createBatch").then(({ response }) => {
        expect(response?.statusCode).eq(202);
        const releaseId = response?.body.release_id;
        expect(releaseId).to.be.a("string").and.not.be.empty;
        // The previous ready batch remains visible until the create response is applied.
        cy.get('[data-testid="batch-history"]').should("have.value", releaseId);
        cy.get('[data-testid="batch-download-JSONL"]', { timeout: 90000 }).should("be.visible");
      });
    };
    const open = (project: number, ids: number[]) => {
      const page = `/projects/${project}/data`;
      cy.loginAs(fixture.users.manager.email, fixture.password, page);
      cy.get('[data-testid="open-release-workspace"]').click();
      cy.get('[data-testid="open-approved-batch"]').click();
      for (const id of ids) cy.get(`[data-testid="batch-select-${id}"]`).check();
      cy.get('[data-testid="batch-preview"]').click();
      cy.get('[data-testid="batch-create"]').should("be.enabled").click();
      waitForCreatedBatch();
    };
    const capture = () => {
      cy.get('[data-testid="snapshot-create"]').should("contain.text", zh ? "捕获来源快照" : "Capture source snapshot").click();
      cy.get('[data-testid="snapshot-manifest"]', { timeout: 90000 }).should("be.visible");
      cy.get('[data-testid="snapshot-digest"]').invoke("text").should("match", /^[a-f0-9]{64}$/);
    };

    it("captures exact text, replays the request, compares membership and restores history", () => {
      open(fixture.project_id, fixture.approved.map((s: any) => s.submission_id));
      capture();
      cy.get('[data-testid="snapshot-id"]').invoke("text").then((first) => {
        cy.get('[data-testid="snapshot-create"]').click();
        cy.get('[data-testid="snapshot-id"]').should("have.text", first);
        cy.get('[data-testid="snapshot-manifest"]').click();
        cy.readFile(`${Cypress.config("downloadsFolder")}/release-manifest-${first}.json`).then((manifest) => {
          expect(manifest.schema).eq("ReleaseManifest/v1");
          expect(manifest.content.items.map((i: any) => Number(i.source.id))).deep.eq(fixture.approved.map((s: any) => s.submission_id));
          expect(manifest.origin_mode).eq("fork_approved");
          expect(manifest.dataset_version_id).eq(undefined);
        });
        cy.get(`[data-testid="batch-select-${fixture.approved[2].submission_id}"]`).uncheck();
        cy.get('[data-testid="batch-preview"]').click();
        cy.get('[data-testid="batch-create"]').should("be.enabled").click();
        waitForCreatedBatch();
        capture();
        cy.get('[data-testid="snapshot-against"]').type(first);
        cy.get('[data-testid="snapshot-compare"]').click();
        cy.get('[data-testid="snapshot-diff"]').should("contain.text", zh ? "新增 1，删除 0，修订 0" : "Added 1, removed 0, revised 0");
        cy.screenshot(`${locale}-release-snapshot-diff`, { capture: "viewport" });
        cy.get('[data-testid="snapshot-id"]').invoke("text").then((second) => {
          cy.reload();
          cy.get('[data-testid="open-release-workspace"]').click();
          cy.get('[data-testid="open-approved-batch"]').click();
          cy.get('[data-testid="batch-history"]').find("option").eq(1).invoke("val").then((release) => {
            cy.get('[data-testid="batch-history"]').select(String(release));
            cy.get('[data-testid="snapshot-history"]').select(second);
            cy.get('[data-testid="snapshot-manifest"]').should("be.visible");
          });
        });
      });
    });

    it("pins managed media and verifies actual downloaded bytes", () => {
      open(fixture.media.project_id, [fixture.media.valid_submission_id]);
      capture();
      cy.get('[data-testid="snapshot-id"]').invoke("text").then((id) => {
        cy.get('[data-testid="snapshot-manifest"]').click();
        cy.readFile(`${Cypress.config("downloadsFolder")}/release-manifest-${id}.json`).then((manifest) => {
          const media = manifest.media[0];
          expect(media.sha256).eq(fixture.media.sha256);
          expect(manifest.content.media_freeze_semantics).eq("snapshot_capture_not_submission_time");
          cy.get(`[data-testid="snapshot-asset-${media.blob_id}"]`).click();
          cy.readFile(`${Cypress.config("downloadsFolder")}/media-${media.blob_id}.bin`, "binary").then((bytes) => {
            cy.task("snapshotSHA256", bytes).should("eq", media.sha256);
          });
        });
      });
      cy.screenshot(`${locale}-release-snapshot-media`, { capture: "viewport" });
    });

    it("fails closed for missing media and exposes no ready download", () => {
      open(fixture.media.project_id, [fixture.media.missing_submission_id]);
      cy.get('[data-testid="snapshot-create"]').click();
      cy.get('[data-testid="snapshot-status"]', { timeout: 90000 }).should("contain.text", zh ? "所需媒体缺失" : "Required media is missing");
      cy.get('[data-testid="snapshot-manifest"]').should("not.exist");
      cy.screenshot(`${locale}-release-snapshot-missing-media`, { capture: "viewport" });
    });

    it("denies stale-page manifest access after membership revocation", () => {
      open(fixture.project_id, fixture.approved.map((s: any) => s.submission_id));
      capture();
      cy.task("setSnapshotManagerEnabled", false);
      cy.get('[data-testid="snapshot-manifest"]').click();
      cy.get('[data-testid="snapshot-error"]').should("contain.text", zh ? "权限已撤销" : "Access revoked");
      cy.get('[data-testid="snapshot-manifest"]').should("not.exist");
      cy.screenshot(`${locale}-release-snapshot-revoked`, { capture: "viewport" });
    });
  });
}
