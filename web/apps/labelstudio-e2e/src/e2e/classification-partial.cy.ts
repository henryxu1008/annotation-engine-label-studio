/// <reference types="cypress" />

describe("frozen classification partial matching", () => {
  let fixture: any;
  before(() => cy.readFile(".classification-partial-e2e.json").then((value) => { fixture = value; }));
  for (const locale of ["en-US", "zh-CN"] as const) {
    it(`explains exact frozen revisions without changing existing workflows in ${locale}`, () => {
      const zh = locale === "zh-CN", scope = fixture.projects[locale];
      const base = `/api/projects/${scope.id}/classification-disagreements/`;
      const task = scope.cases.split.task;
      let sourceFrozen: string, partialFrozen: string, initial: any;
      const visit = (role: string) => {
        cy.loginAs(fixture.users[role].email, fixture.password, "/user/account/personal-info");
        cy.get('[data-testid="language-preference-select"]').should("be.enabled").then(($select) => {
          if ($select.val() !== locale) {
            cy.intercept({ method: "PATCH", pathname: /^\/api\/current-user\/locale\/?$/ }).as("partialLocale");
            cy.get('[data-testid="language-preference-select"]').select(locale);
            cy.wait("@partialLocale").its("response.statusCode").should("eq", 200);
          }
        });
        cy.visit(`/projects/${scope.id}/data`);
        cy.get("html").should("have.attr", "lang", locale);
      };
      const explain = () => cy.get('[data-testid="classification-partial-load"]').scrollIntoView().click();
      visit("manager");
      cy.get('[data-testid="open-classification-disagreement"]', { timeout: 30000 }).click();
      cy.get('[data-testid="classification-workspace"] input[type="number"]').clear().type(String(task));
      cy.contains("button", zh ? "读取可选修订" : "Load available revisions").click();
      cy.get('[data-testid="classification-workspace"] input[type="checkbox"]').should("have.length", 3).each(($row) => cy.wrap($row).check());
      cy.intercept({ method: "GET", pathname: new RegExp(`${base}tasks/${task}/?$`) }).as("partialBase");
      cy.contains("button", zh ? "冻结输入并对比" : "Freeze inputs and compare").click();
      cy.wait("@partialBase").then(({ response }) => { sourceFrozen = response!.body.frozen; expect(response!.body.agreement).to.eq(0); });
      cy.intercept({ method: "GET", pathname: new RegExp(`${base}tasks/${task}/partial-match/?$`) }).as("partialRead");
      explain();
      cy.wait("@partialRead").then(({ request, response }) => {
        expect(response?.statusCode).to.eq(200); expect(request.query.source_frozen).to.eq(sourceFrozen);
        initial = response!.body; partialFrozen = initial.frozen;
        expect(initial.partial.score).to.deep.eq({ numerator: "4", denominator: "9" });
        expect(initial.base.agreement).to.eq(0);
      });
      cy.get('[data-testid="classification-partial-score"]').should("contain.text", "44.44%");
      cy.get('[data-testid="classification-agreement"]').should("contain.text", "0.00%");
      cy.get('[data-testid="classification-partial-pair"]').should("have.length", 3).first()
        .should("contain.text", zh ? "共同标签" : "Shared labels");
      explain();
      cy.wait("@partialRead").then(({ request, response }) => {
        expect(request.query.frozen).to.eq(partialFrozen);
        expect(response!.body.receipt).to.deep.eq(initial.receipt);
        expect(response!.body.partial).to.deep.eq(initial.partial);
      });
      cy.get('[data-testid="classification-partial-score"]').scrollIntoView().should("be.visible");
      cy.screenshot(`classification-partial-manual-${locale}`, { capture: "viewport" });

      cy.reload();
      cy.get('[data-testid="open-classification-batches"]', { timeout: 30000 }).click();
      cy.contains("button", scope.batch.name).click();
      cy.get(`[data-task="${task}"]`).click(); explain();
      cy.get('[data-testid="classification-partial-score"]').should("contain.text", "44.44%");
      cy.get('[data-testid="classification-agreement"]').should("contain.text", "0.00%");
      cy.get('[data-testid="classification-partial-score"]').scrollIntoView();
      cy.screenshot(`classification-partial-batch-${locale}`, { capture: "viewport" });
      cy.contains("button", zh ? "返回队列" : "Back to queue").click();
      cy.get('[data-testid="classification-batches"] select').should("have.value", "disagreement").select("unavailable");
      cy.get(`[data-task="${scope.cases.empty.task}"]`).click(); explain();
      cy.get('[data-testid="classification-partial-result"]').should("contain.text", zh ? "不可比较" : "Not comparable");
      cy.get('[data-testid="classification-partial-score"]').should("not.exist");
      cy.contains("button", zh ? "返回队列" : "Back to queue").click();
      cy.get('[data-testid="classification-batches"] select').should("have.value", "unavailable").select("disagreement");
      cy.intercept({ method: "GET", pathname: new RegExp(`${base}batches/${scope.batch.id}/items/${scope.cases.budget.task}/partial-match/?$`) }).as("partialBudget");
      cy.get(`[data-task="${scope.cases.budget.task}"]`).click(); explain();
      cy.wait("@partialBudget").then(({ response }) => {
        expect(response?.statusCode).to.eq(422);
        expect(response!.body.display_context.reason).to.eq("CLASSIFICATION_PARTIAL_TOO_LARGE");
      });
      cy.get('[data-testid="classification-batches"] [role="alert"]').should("be.visible");
      cy.get('[data-testid="classification-report"]').should("not.exist");
      cy.request(`${base}batches/${scope.batch.id}/`).its("body.manifest_hash").should("eq", scope.batch.manifest_hash);

      visit("a");
      cy.then(() => cy.request({ url: `${base}tasks/${task}/partial-match/`, qs: { frozen: partialFrozen }, failOnStatusCode: false }))
        .its("status").should("be.oneOf", [403, 404]);
      visit("reviewer");
      cy.then(() => cy.request(`${base}tasks/${task}/partial-match/?frozen=${encodeURIComponent(partialFrozen)}`)).its("body.partial.score")
        .should("deep.eq", { numerator: "4", denominator: "9" });
      cy.request({ url: `${base}batches/${scope.batch.id}/items/${scope.cases.self.task}/partial-match/`, failOnStatusCode: false })
        .its("status").should("eq", 404);
      visit("manager");
      cy.request(`/api/projects/${scope.id}/members/?limit=100`).then(({ body }) => {
        const member = (body.results ?? body).find((row: any) => row.user.id === fixture.users.reviewer.id);
        cy.request("DELETE", `/api/projects/${scope.id}/members/${member.id}/`);
      });
      visit("reviewer");
      cy.then(() => cy.request({ url: `${base}tasks/${task}/partial-match/`, qs: { frozen: partialFrozen }, failOnStatusCode: false }))
        .its("status").should("be.oneOf", [403, 404]);
      visit("manager");
      cy.request("DELETE", `/api/tasks/${task}/`);
      cy.then(() => cy.request(`${base}tasks/${task}/partial-match/?frozen=${encodeURIComponent(partialFrozen)}`))
        .then(({ body }) => { expect(body.partial.status).to.eq("unavailable"); expect(body.base.locations).to.deep.eq([]); });
      cy.request(`${base}batches/${scope.batch.id}/`).its("body.manifest_hash").should("eq", scope.batch.manifest_hash);
    });
  }
});
