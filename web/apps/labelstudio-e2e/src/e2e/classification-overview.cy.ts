/// <reference types="cypress" />

describe("single frozen classification batch quality overview", () => {
  let fixture: any;
  before(() => cy.readFile(".classification-overview-e2e.json").then((value) => { fixture = value; }));
  for (const locale of ["en-US", "zh-CN"] as const) {
    it(`keeps task weights, scope and frozen evidence in ${locale}`, () => {
      const zh = locale === "zh-CN", scope = fixture.projects[locale];
      const base = `/api/projects/${scope.id}/classification-disagreements/batches/`;
      let initial: any, frozen: string;
      const login = (role: string) => {
        cy.loginAs(fixture.users[role].email, fixture.password, "/user/account/personal-info");
        cy.setUserLocale(locale); cy.visit(`/projects/${scope.id}/data`);
      };
      const open = (batch: any) => {
        cy.get('[data-testid="open-classification-batches"]', { timeout: 30000 }).click();
        cy.contains("button", batch.name).click();
        cy.get('[data-testid="quality-read"]').scrollIntoView().click();
      };
      login("manager"); open(scope.batch);
      cy.get('[data-testid="quality-summary"]').should("contain.text", "50.00%").and("contain.text", "72.22%").and("contain.text", "66.67%");
      cy.get(`[data-quality-actor="${fixture.users.a.id}"]`).should("contain.text", "70.83%");
      cy.contains(zh ? "一致程度不代表正确率" : "Agreement is not correctness").should("be.visible");
      cy.request(`${base}${scope.batch.id}/quality-overview/?page_size=1`).then(({ body }) => { initial = body; frozen = body.frozen; });
      cy.then(() => cy.request({ url: `${base}${scope.batch.id}/quality-overview/`, qs: { frozen, page: 2, page_size: 1 } }))
        .then(({ body }) => {
          expect(body.result_hash).to.eq(initial.result_hash); expect(body.summarized_at).to.eq(initial.summarized_at);
          expect(body.summary).to.deep.eq(initial.summary); expect(body.annotators[0].actor_id).not.to.eq(initial.annotators[0].actor_id);
        });
      cy.get(`[data-quality-actor="${fixture.users.a.id}"] [data-quality-task="${scope.cases.split.task}"]`).click();
      cy.get('[data-testid="classification-report"]').should("be.visible");
      cy.request(`${base}${scope.batch.id}/items/${scope.cases.split.task}/`).then(({ body }) => {
        expect(body.item.inputs.map((v: any) => v.submission_id)).not.to.include(scope.newer_revision);
      });
      cy.contains("button", zh ? "返回队列" : "Back to queue").click();
      cy.get('[data-testid="quality-summary"]').should("contain.text", "72.22%");
      cy.get('[data-testid="classification-batches"] select').should("have.value", "disagreement");
      let lateEntered = false;
      cy.intercept({ method: "GET", pathname: new RegExp(`${base}${scope.batch.id}/quality-overview/?$`), times: 1 },
        (request) => { lateEntered = true; request.continue((response) => response.setDelay(1000)); }).as("lateOverview");
      cy.get('[data-testid="quality-read"]').click();
      cy.wrap(null).should(() => expect(lateEntered).to.eq(true));
      cy.window().then((win) => win.dispatchEvent(new win.Event("blur")));
      cy.wait("@lateOverview");
      cy.get('[data-testid="quality-summary"]').should("not.exist"); cy.get('[data-quality-task]').should("not.exist");
      cy.get('[data-testid="classification-batch-refresh"]').click();
      cy.get('[data-testid="quality-summary"]').should("contain.text", "72.22%");
      cy.screenshot(`classification-quality-overview-${locale}`, { capture: "fullPage" });
      cy.request(`${base}${scope.batch.id}/`).its("body.manifest_hash").should("eq", scope.batch.manifest_hash);
      login("manager");
      cy.intercept({ method: "GET", pathname: new RegExp(`${base}${scope.page_batch.id}/quality-overview/?$`) }).as("pagedOverview");
      open(scope.page_batch);
      let pageFrozen: string;
      cy.wait("@pagedOverview").then(({ response }) => { pageFrozen = response!.body.frozen; expect(response!.body.pagination.count).to.eq(51); });
      cy.get('[data-quality-actor]').should("have.length", 50);
      cy.get('[data-testid="quality-next"]').click();
      cy.wait("@pagedOverview").then(({ response }) => { expect(response!.body.frozen).to.eq(pageFrozen); expect(response!.body.pagination.page).to.eq(2); });
      cy.get('[data-quality-actor]').should("have.length", 1);
      cy.get('[data-quality-task]').first().click();
      cy.get('[data-testid="classification-report"]').should("be.visible");
      cy.contains("button", zh ? "返回队列" : "Back to queue").click();
      cy.wait("@pagedOverview").then(({ response }) => expect(response!.body.frozen).to.eq(pageFrozen));
      cy.get('[data-quality-actor]').should("have.length", 1); cy.get('[data-testid="quality-next"]').should("be.disabled");
      login("a");
      cy.then(() => cy.request({ url: `${base}${scope.batch.id}/quality-overview/`, qs: { frozen }, failOnStatusCode: false }))
        .its("status").should("be.oneOf", [403, 404]);
      login("reviewer"); open(scope.self_batch);
      cy.contains(zh ? "仅覆盖你可见的任务" : "visible subset").should("be.visible");
      cy.request(`${base}${scope.self_batch.id}/quality-overview/`).then(({ body }) => {
        expect(body.scope_counts.tasks).to.eq(2); expect(body.scope.reference_kind).to.eq("projection_hash");
        expect(JSON.stringify(body)).not.to.contain(scope.self_batch.manifest_hash);
        expect(body.annotators.map((v: any) => v.actor_id)).not.to.include(fixture.users.reviewer.id);
      });
      login("manager"); open(scope.empty_batch);
      cy.contains(zh ? "没有可比较任务" : "No comparable tasks").should("be.visible");
      cy.get('[data-testid="quality-summary"]').should("not.contain.text", "100.00%");
      login("manager"); open(scope.budget_batch);
      cy.get('[data-testid="classification-quality-overview"] [role="alert"]').should("be.visible");
      cy.get('[data-testid="quality-summary"]').should("not.exist");
      cy.request({ url: `${base}${scope.budget_batch.id}/quality-overview/`, failOnStatusCode: false }).its("status").should("eq", 422);
      login("manager"); open(scope.batch);
      cy.request("DELETE", `/api/tasks/${scope.cases.agreement.task}/`);
      cy.get('[data-testid="quality-read"]').click();
      cy.contains(zh ? "统计分母不变" : "denominator is unchanged").should("be.visible");
      cy.get('[data-testid="quality-summary"]').should("not.exist"); cy.get('[data-quality-actor]').should("not.exist");
      cy.then(() => cy.request({ url: `${base}${scope.batch.id}/quality-overview/`, qs: { frozen } })).then(({ body }) => {
        expect(body.scope_counts).to.deep.eq(initial.scope_counts); expect(body.summary).to.eq(null); expect(body.receipt_verified).to.eq(false);
      });
      login("manager");
      cy.request(`/api/projects/${scope.id}/members/?limit=100`).then(({ body }) => {
        const member = (body.results ?? body).find((v: any) => v.user.id === fixture.users.reviewer.id);
        cy.request("DELETE", `/api/projects/${scope.id}/members/${member.id}/`);
      });
      login("reviewer");
      cy.request({ url: `${base}${scope.self_batch.id}/quality-overview/`, failOnStatusCode: false }).its("status").should("be.oneOf", [403, 404]);
    });
  }
});
