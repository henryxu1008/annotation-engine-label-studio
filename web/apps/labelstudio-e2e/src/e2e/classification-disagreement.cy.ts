/// <reference types="cypress" />

describe("read-only classification disagreement", () => {
  let fixture: any;
  before(() => cy.readFile(".classification81-e2e.json").then((value) => { fixture = value; }));
  for (const locale of ["en-US", "zh-CN"] as const) {
    it(`freezes exact inputs, explains disagreement and denies revoked access in ${locale}`, () => {
      const zh = locale === "zh-CN", scope = fixture.projects[locale];
      const path = `/projects/${scope.id}/data`;
      const base = `/api/projects/${scope.id}/classification-disagreements/`;
      let receipt: string, initial: any;
      const visit = (role: string) => {
        cy.loginAs(fixture.users[role].email, fixture.password, "/user/account/personal-info");
        cy.get('[data-testid="language-preference-select"]').should("be.enabled").then(($select) => {
          if ($select.val() !== locale) {
            cy.intercept({ method: "PATCH", pathname: /^\/api\/current-user\/locale\/?$/ }).as("classificationLocaleSaved");
            cy.get('[data-testid="language-preference-select"]').select(locale);
            cy.wait("@classificationLocaleSaved").then(({ response }) => {
              expect(response?.statusCode).to.eq(200);
              expect(response?.body.preference).to.eq(locale);
            });
          }
        });
        cy.get('[data-testid="language-preference-select"]').should("have.value", locale).and("not.be.disabled");
        cy.get("html").should("have.attr", "lang", locale);
        cy.visit(path);
        cy.get("html").should("have.attr", "lang", locale);
      };
      const open = () => cy.get('[data-testid="open-classification-disagreement"]', { timeout: 30000 }).click();
      const choose = (name: string) => {
        cy.get('[data-testid="classification-workspace"] input[type="number"]').clear().type(String(scope.cases[name].task));
        cy.contains("button", zh ? "读取可选修订" : "Load available revisions").click();
        cy.get('[data-testid="classification-workspace"] input[type="checkbox"]').should("have.length", 3).each(($row) => cy.wrap($row).check());
      };
      const compare = () => cy.contains("button", zh ? "冻结输入并对比" : "Freeze inputs and compare").click();
      visit("manager"); open(); choose("agreement"); compare();
      cy.get('[data-testid="classification-agreement"]').should("contain.text", "100.00%");
      choose("split");
      cy.intercept({ method: "GET", pathname: `${base}tasks/${scope.cases.split.task}` }).as("report");
      compare();
      cy.wait("@report").then(({ response }) => {
        expect(response?.statusCode).to.eq(200);
        initial = response!.body; receipt = initial.frozen;
        expect(initial.matching_pairs).to.eq(1);
        expect(initial.total_pairs).to.eq(3);
        expect(initial.manifest.inputs.map((row: any) => row.submission_id).sort()).to.deep.eq([...scope.cases.split.sources].sort());
      });
      cy.get('[data-testid="classification-agreement"]').should("contain.text", "33.33%");
      cy.get('[data-testid="classification-location"]').should("contain.text", "sentiment").and("contain.text", "Positive").and("contain.text", "Negative");
      cy.contains("button", zh ? "重验冻结输入" : "Recheck frozen inputs").click();
      cy.wait("@report").then(({ response, request }) => {
        expect(request.query.frozen).to.eq(receipt);
        expect(response!.body.manifest).to.deep.eq(initial.manifest);
        expect(response!.body.manifest_hash).to.eq(initial.manifest_hash);
        expect(response!.body.locations).to.deep.eq(initial.locations);
      });
      choose("missing"); compare();
      cy.get('[data-testid="classification-unavailable"]').should("contain.text", zh ? "分类值缺失" : "Classification values are missing");
      cy.get('[data-testid="classification-agreement"]').should("not.exist");
      choose("config"); compare();
      cy.get('[data-testid="classification-unavailable"]').should("contain.text", zh ? "标签配置不同" : "configurations differ");
      choose("remote"); compare();
      cy.get('[data-testid="classification-unavailable"]').should("contain.text", zh ? "静态全局 Choices" : "static global Choices");
      cy.get('[data-testid="classification-agreement"]').should("not.exist");
      for (const unsupported of ["resolver", "dynamic"]) {
        choose(unsupported); compare();
        cy.get('[data-testid="classification-unavailable"]').should("contain.text", zh ? "静态全局 Choices" : "static global Choices");
        cy.get('[data-testid="classification-agreement"]').should("not.exist");
      }
      visit("a");
      cy.get('[data-testid="open-classification-disagreement"]').should("not.exist");
      cy.then(() => cy.request({ url: `${base}tasks/${scope.cases.split.task}/`, qs: { frozen: receipt }, failOnStatusCode: false }))
        .its("status").should("be.oneOf", [403, 404]);
      visit("reviewer"); open(); choose("split"); compare();
      cy.get('[data-testid="classification-agreement"]').should("contain.text", "33.33%");
      visit("manager");
      cy.request(`/api/projects/${scope.id}/members/?limit=100`).then(({ body }) => {
        const member = (body.results ?? body).find((row: any) => row.user.id === fixture.users.reviewer.id);
        cy.request("DELETE", `/api/projects/${scope.id}/members/${member.id}/`);
      });
      visit("reviewer");
      cy.get('[data-testid="open-classification-disagreement"]').should("not.exist");
      cy.then(() => cy.request({ url: `${base}tasks/${scope.cases.split.task}/`, qs: { frozen: receipt }, failOnStatusCode: false }))
        .its("status").should("be.oneOf", [403, 404]);
      visit("manager"); open(); choose("split"); compare();
      // Scroll to the metric a reviewer reads in the constrained modal viewport.
      cy.get('[data-testid="classification-agreement"]').scrollIntoView()
        .should("be.visible").and("contain.text", "33.33%");
      cy.request("DELETE", `/api/tasks/${scope.cases.split.task}/`);
      cy.contains("button", zh ? "重验冻结输入" : "Recheck frozen inputs").click();
      cy.get('[data-testid="classification-workspace"] [role="alert"]').should("be.visible");
      cy.get('[data-testid="classification-report"]').should("not.exist");
    });
  }
});
