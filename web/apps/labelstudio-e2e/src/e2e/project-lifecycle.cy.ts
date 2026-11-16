/// <reference types="cypress" />

const projectId = Number(Cypress.env("issue86_project_id"));
const password = "synthetic-issue86-browser-password";
const path = () => `/projects/${projectId}/settings/lifecycle`;

for (const [locale, title, pause, active, reason, action] of [
  ["en-US", "Project lifecycle", "Paused", "Active", "Reason", "Change state"],
  ["zh-CN", "项目生命周期", "已暂停", "进行中", "原因", "更改状态"],
] as const) {
  it(`enforces a stale task write after a ${locale} lifecycle transition`, () => {
    expect(projectId).to.be.a("number").and.be.greaterThan(0);
    Cypress.env("loginLocale", locale);
    cy.loginAs("issue86-manager@example.test", password, path());
    cy.request(`/api/projects/${projectId}/lifecycle/`).then(({ body }) => {
      if (body.state === "paused") {
        cy.getCookie("csrftoken", { log: false }).then((cookie) => {
          cy.request({
            method: "POST", url: `/api/projects/${projectId}/lifecycle/`,
            headers: { "X-CSRFToken": cookie?.value ?? "" },
            body: { target: "active", expected_revision: body.revision, reason: "synthetic test reset" },
          }).its("status").should("eq", 200);
        });
        cy.visit(path());
      } else {
        expect(body.state).to.eq("active");
      }
    });
    cy.contains("h1", title).should("be.visible");
    cy.contains("strong", active).should("be.visible");
    cy.intercept({ method: "POST", pathname: new RegExp(`^/api/projects/${projectId}/lifecycle/?$`) }).as("changeLifecycle");
    cy.get("#lifecycle-target").select("paused");
    cy.get("#lifecycle-reason").type("synthetic pause");
    cy.contains("button", action).should("be.enabled").click();
    cy.wait("@changeLifecycle").its("response.statusCode").should("eq", 200);
    cy.contains("strong", pause).should("be.visible");
    cy.getCookie("csrftoken", { log: false }).then((cookie) => {
      cy.request({
        method: "POST", url: `/api/projects/${projectId}/tasks/`, failOnStatusCode: false,
        headers: { "X-CSRFToken": cookie?.value ?? "" }, body: { data: { text: "stale write" } },
      }).its("status").should("eq", 403);
    });
    cy.get("#lifecycle-target").select("active");
    cy.get("#lifecycle-reason").clear().type("synthetic resume");
    cy.contains("button", action).should("be.enabled").click();
    cy.wait("@changeLifecycle").its("response.statusCode").should("eq", 200);
    cy.contains("strong", active).should("be.visible");
    cy.get('label[for="lifecycle-reason"]').should("have.text", reason);
  });
}

it("denies the lifecycle API to a non-manager", () => {
  Cypress.env("loginLocale", "en-US");
  cy.loginAs("issue86-annotator@example.test", password, `/projects/${projectId}/data`);
  cy.request({ url: `/api/projects/${projectId}/lifecycle/`, failOnStatusCode: false })
    .its("status").should("be.oneOf", [403, 404]);
});
