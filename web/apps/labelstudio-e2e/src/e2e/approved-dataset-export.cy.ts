/// <reference types="cypress" />

type Fixture = {
  project_id: number;
  password: string;
  users: Record<string, { id: number; email: string }>;
  approved: { submission_id: number; revision: number; result_hash: string }[];
  pending: { submission_id: number; revision: number; result_hash: string };
};

for (const locale of ["en-US", "zh-CN"]) {
  describe(`${locale} approved batch export`, () => {
    let fixture: Fixture;
    const url = () => `/api/projects/${fixture.project_id}/approved-dataset-releases/`;
    const page = () => `/projects/${fixture.project_id}/data`;
    before(() => cy.readFile(".approved-export-e2e.json").then((value) => { fixture = value; }));
    beforeEach(() => {
      Cypress.env("loginLocale", locale);
      cy.task("setApprovedExportManagerEnabled", true);
    });
    afterEach(() => cy.task("setApprovedExportManagerEnabled", true));
    const open = () => {
      cy.loginAs(fixture.users.manager.email, fixture.password, page());
      cy.visit(page());
      cy.get('[data-testid="open-release-workspace"]').click();
      cy.get('[data-testid="open-approved-batch"]').should("contain.text", locale === "zh-CN" ? "导出已批准数据集" : "Export approved dataset").click();
      for (const source of fixture.approved) cy.get(`[data-testid="batch-select-${source.submission_id}"]`).check();
      cy.get(`[data-testid="batch-select-${fixture.pending.submission_id}"]`).should("not.exist");
    };

    it("previews exact revisions, generates JSON/JSONL and a real standard format", () => {
      open();
      cy.intercept({ method: "POST", pathname: /^\/api\/projects\/\d+\/approved-dataset-releases\/?$/ }).as("createBatch");
      cy.get('[data-testid="batch-preview"]').click();
      cy.get('[data-testid="batch-preview-result"]').should("contain.text", "3");
      cy.get('[data-testid="batch-create"]').click();
      cy.wait("@createBatch").then(({ response, request }) => {
        expect(response?.statusCode).eq(202);
        expect(request.body.items.map((item) => item.submission_id)).deep.eq(fixture.approved.map((item) => item.submission_id));
        const releaseId = response?.body.release_id;
        cy.get('[data-testid="batch-download-JSONL"]', { timeout: 90000 }).should("be.visible").click();
        cy.readFile(`${Cypress.config("downloadsFolder")}/records.jsonl`).then((text) => {
          const rows = String(text).trim().split("\n").map((line) => JSON.parse(line));
          expect(rows.map((row) => row.result_hash)).deep.eq(fixture.approved.map((row) => row.result_hash));
          expect(rows.every((row) => row.review.decision === "approved")).eq(true);
        });
        cy.get('[data-testid="batch-download-JSON"]').click();
        cy.readFile(`${Cypress.config("downloadsFolder")}/records.json`).should("have.length", 3);
        cy.screenshot(`${locale}-approved-batch-ready`, { capture: "viewport" });
        cy.request(`${url()}${releaseId}/manifest/`).then(({ body, headers }) => {
          expect(body.schema).eq("fork.approved-export-manifest/v1");
          expect(body.input.content.items).have.length(3);
          expect(headers["x-manifest-sha256"]).match(/^[a-f0-9]{64}$/);
        });
        cy.get('[data-testid="batch-download-manifest"]').click();
        cy.readFile(`${Cypress.config("downloadsFolder")}/manifest-${releaseId}.json`).its("input.release_id").should("eq", releaseId);
        cy.reload();
        cy.get('[data-testid="open-release-workspace"]').click();
        cy.get('[data-testid="open-approved-batch"]').click();
        cy.get('[data-testid="batch-history"]').select(releaseId);
        cy.get('[data-testid="batch-download-JSONL"]').should("be.visible");
        for (const source of fixture.approved) cy.get(`[data-testid="batch-select-${source.submission_id}"]`).check();
      });
      cy.get('[data-testid="batch-format"]').should("contain.text", "CONLL2003").select("CONLL2003");
      cy.get('[data-testid="batch-preview"]').click();
      cy.get('[data-testid="batch-create"]').should("be.enabled").click();
      cy.get('[data-testid="batch-download-CONLL2003"]', { timeout: 90000 }).click();
      cy.readFile(`${Cypress.config("downloadsFolder")}/conll2003.zip`, "binary").should("not.be.empty");
      cy.request({ method: "POST", url: `${url()}preview/`, failOnStatusCode: false,
        body: { items: [...fixture.approved, fixture.pending], output: { format: "JSONL", resource_mode: "references_only" }, filters: {} } })
        .its("status").should("eq", 409);
    });

    it("denies old-page download after membership revocation and denies reviewer direct access", () => {
      open();
      cy.get('[data-testid="batch-preview"]').click();
      cy.get('[data-testid="batch-create"]').should("be.enabled").click();
      cy.get('[data-testid="batch-download-JSONL"]', { timeout: 90000 }).should("be.visible");
      cy.task("setApprovedExportManagerEnabled", false);
      cy.get('[data-testid="batch-download-JSONL"]').click();
      cy.get('[data-testid="batch-error"]').should("contain.text", locale === "zh-CN" ? "来源已不可用" : "no longer available");
      cy.get('[data-testid="batch-download-JSONL"]').should("not.exist");
      cy.screenshot(`${locale}-approved-batch-revoked`, { capture: "viewport" });
      cy.request({ url: `${url()}candidates/`, failOnStatusCode: false }).its("status").should("be.oneOf", [403, 404]);
      cy.task("setApprovedExportManagerEnabled", true);
      cy.loginAs(fixture.users.reviewer.email, fixture.password, page());
      cy.request({ url: `${url()}candidates/`, failOnStatusCode: false }).its("status").should("eq", 403);
      cy.visit(page());
      cy.get('[data-testid="open-approved-batch"]').should("not.exist");
    });
  });
}
