/// <reference types="cypress" />

describe("import waits for its project", () => {
  let release: (() => void) | undefined;
  afterEach(() => release?.());

  for (const locale of ["en-US", "zh-CN"]) {
    it(`waits for a slow project before accepting an upload in ${locale}`, () => {
      cy.readFile(".enterprise-e2e.json").then((fixture) => {
        cy.loginAs(fixture.users.manager.email, fixture.password, "/user/account/personal-info");
        // Establish the language through its real persisted preference before
        // navigation. Merely selecting an option can abandon the pending save.
        cy.intercept({ method: "PATCH", pathname: /^\/api\/current-user\/locale\/?$/ }, (request) => {
          return new Cypress.Promise<void>((resolve) => setTimeout(resolve, 500)).then(() => request.continue());
        }).as("localeSaved");
        cy.get('[data-testid="language-preference-select"]').select(locale);
        cy.wait("@localeSaved").then(({ request, response }) => {
          expect(request.body.preference).to.eq(locale);
          expect(response?.statusCode).to.eq(200);
          expect(response?.body.preference).to.eq(locale);
        });
        cy.get('[data-testid="language-preference-select"]').should("have.value", locale).and("not.be.disabled");
        cy.get("html").should("have.attr", "lang", locale);
        let pending = 0;
        const responseGate = new Promise<void>((resolve) => { release = resolve; });
        cy.intercept({ method: "GET", pathname: new RegExp(`^/api/projects/${fixture.project_id}/?$`) }, (request) => {
          request.on("before:response", () => { pending += 1; return responseGate; });
        }).as("projectReady");
        cy.visit(`/projects/${fixture.project_id}/data/import`);
        cy.get('[aria-label="' + (locale === "zh-CN" ? "完成导入" : "Finish import") + '"]').should("exist");
        cy.wrap(null).should(() => expect(pending, "project response is deliberately held").to.be.greaterThan(0));
        cy.get('[aria-label="' + (locale === "zh-CN" ? "完成导入" : "Finish import") + '"]').should("be.disabled");
        // Inspect synchronously: retrying a deliberately broken assertion would
        // let Cypress's response-handler timeout obscure the product failure.
        cy.get("body").then(($body) => expect($body.find("#file-input"), "upload stays unavailable before project identity").to.have.length(0));
        cy.then(() => release?.());
        cy.wait("@projectReady");
        cy.intercept("POST", `**/api/projects/${fixture.project_id}/import*`).as("imported");
        cy.get("#file-input").selectFile({
          contents: Cypress.Buffer.from(JSON.stringify([{ text: "Slow project 日本語" }])),
          fileName: "slow-project.json", mimeType: "application/json",
        }, { force: true });
        cy.wait("@imported").its("response.statusCode").should("eq", 201);
        cy.get('[aria-label="' + (locale === "zh-CN" ? "完成导入" : "Finish import") + '"]').click();
        cy.location("pathname").should("eq", `/projects/${fixture.project_id}/data`);
        cy.request(`/api/tasks/?project=${fixture.project_id}`).then((response) => {
          expect(JSON.stringify(response.body)).to.contain("Slow project 日本語");
        });
      });
    });
  }
});
