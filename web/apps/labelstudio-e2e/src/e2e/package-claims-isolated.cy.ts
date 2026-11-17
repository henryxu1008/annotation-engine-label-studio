/// <reference types="cypress" />

const project = Number(Cypress.env("issue86_project_id"));
const taskId = Number(Cypress.env("issue83_task_id"));
const password = "synthetic-issue86-browser-password";
const root = () => `/api/projects/${project}/task-packages/`;

for (const locale of ["en-US", "zh-CN"] as const) {
  it(`configures, claims, cancels and reopens a fixed package in ${locale}`, () => {
    const zh = locale === "zh-CN";
    const name = `Synthetic pool ${locale}`;
    let packageId: number;
    let assignmentIds: number[] = [];
    Cypress.env("loginLocale", locale);
    cy.loginAs("issue86-manager@example.test", password, `/projects/${project}/data`);
    cy.getCookie("csrftoken", { log: false }).then((cookie) => cy.request({
      method: "POST", url: root(), headers: { "X-CSRFToken": cookie?.value ?? "", "Idempotency-Key": crypto.randomUUID() },
      body: { name, task_ids: [taskId] },
    })).then(({ body }) => { packageId = body.id; });
    cy.reload();
    cy.get('[data-testid="open-task-packages"]').should("be.visible").click();
    cy.contains("button", name).click();
    cy.contains("button", zh ? "管理分配与领取" : "Manage assignment and claims").click();
    cy.contains("button", zh ? "保存领取设置" : "Save claim settings").closest("form").within(() => {
      cy.contains("label", "issue86-annotator@example.test").find('input[type="checkbox"]').check();
      cy.contains("label", zh ? "优先级（0–100）" : "Priority (0–100)").find("input").clear().type("8");
      cy.contains("label", zh ? "开放领取" : "Open for claims").find('input[type="checkbox"]').check();
    });
    cy.intercept({ method: "PUT", pathname: new RegExp(`/api/projects/${project}/task-packages/\\d+/pool/?$`) }).as("savePool");
    cy.contains("button", zh ? "保存领取设置" : "Save claim settings").click();
    cy.wait("@savePool").then(({ response }) => expect(response?.statusCode, JSON.stringify(response?.body)).to.eq(200));
    cy.loginAs("issue86-annotator@example.test", password, `/projects/${project}/data`);
    cy.get('[data-testid="open-task-packages"]').should("be.visible").click();
    cy.intercept({ method: "POST", pathname: new RegExp(`/api/projects/${project}/task-packages/\\d+/claim/?$`) }).as("claimPool");
    cy.contains("li", name).within(() => {
      cy.contains(zh ? "优先级：8" : "Priority: 8").should("be.visible");
      cy.contains("button", zh ? "领取任务包／重试" : "Claim package / retry").click();
    });
    cy.wait("@claimPool").its("response.statusCode").should("eq", 201);
    cy.then(() => cy.request(`${root()}${packageId}/`)).its("body.results").then((rows) => {
      assignmentIds = rows.map((row: any) => row.assignment_id);
      expect(assignmentIds).to.have.length(1);
    });
    cy.loginAs("issue86-manager@example.test", password, `/projects/${project}/data`);
    const setDeadline = (dueAt: string | null) => {
      cy.then(() => cy.request(`${root()}${packageId}/pool/`)).then(({ body }) => {
        cy.getCookie("csrftoken", { log: false }).then((cookie) => cy.request({
          method: "PUT", url: `${root()}${packageId}/pool/`, headers: { "X-CSRFToken": cookie?.value ?? "" },
          body: { expected_revision: body.revision, candidate_ids: body.candidate_ids,
            enabled: body.enabled, priority: body.priority, due_at: dueAt },
        })).its("status").should("eq", 200);
      });
    };
    setDeadline(new Date(Date.now() - 60_000).toISOString());
    cy.loginAs("issue86-annotator@example.test", password, `/projects/${project}/data`);
    cy.get('[data-testid="open-task-packages"]').should("be.visible").click();
    cy.contains(zh ? "逾期工作提醒" : "Overdue work notices").should("be.visible");
    cy.contains(zh ? "逾期的已领取任务：1" : "Overdue claimed tasks: 1").should("be.visible");
    cy.loginAs("issue86-manager@example.test", password, `/projects/${project}/data`);
    setDeadline(null);
    cy.getCookie("csrftoken", { log: false }).then((cookie) => {
      for (const id of assignmentIds) cy.request({ method: "DELETE", url: `/api/task-assignments/${id}/`,
        headers: { "X-CSRFToken": cookie?.value ?? "" } }).its("status").should("eq", 204);
    });
    cy.get('[data-testid="open-task-packages"]').should("be.visible").click();
    cy.contains("button", name).click();
    cy.contains("button", zh ? "管理分配与领取" : "Manage assignment and claims").click();
    cy.intercept({ method: "POST", pathname: new RegExp(`/api/projects/${project}/task-packages/\\d+/reopen/?$`) }).as("reopenPool");
    cy.contains("button", zh ? "取消原分配后重新开放／重试" : "Reopen after cancellation / retry").click();
    cy.wait("@reopenPool").its("response.statusCode").should("eq", 200);
    cy.contains("button", zh ? "管理分配与领取" : "Manage assignment and claims").click();
    cy.intercept({ method: "POST", pathname: new RegExp(`/api/projects/${project}/task-packages/\\d+/balance/?$`) }).as("balancePool");
    cy.contains("button", zh ? "分配给负载最低的成员／重试" : "Assign to least-loaded member / retry").click();
    cy.wait("@balancePool").then(({ response }) => {
      expect(response?.statusCode).to.eq(201);
      assignmentIds = response!.body.assignment_ids;
      expect(assignmentIds).to.have.length(1);
    });
    cy.getCookie("csrftoken", { log: false }).then((cookie) => {
      for (const id of assignmentIds) cy.request({ method: "DELETE", url: `/api/task-assignments/${id}/`,
        headers: { "X-CSRFToken": cookie?.value ?? "" } }).its("status").should("eq", 204);
    });
  });
}
