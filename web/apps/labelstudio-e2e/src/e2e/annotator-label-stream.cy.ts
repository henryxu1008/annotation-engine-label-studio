/// <reference types="cypress" />

type StreamFixture = {
  password: string;
  users: { annotator_a: { email: string }; manager: { email: string } };
  project_id: number;
  tasks: { a: { id: number } };
};

describe("Annotator labeling entry and queue failures", () => {
  let fixture: StreamFixture;
  before(() => cy.readFile(".enterprise-e2e.json").then((value) => { fixture = value as StreamFixture; }));

  const enterProject = (locale: "en-US" | "zh-CN", actor: "annotator_a" | "manager" = "annotator_a") => {
    cy.loginAs(fixture.users[actor].email, fixture.password, "/user/account/personal-info");
    cy.get('[data-testid="language-preference-select"]').select(locale);
    cy.get('[data-testid="language-preference-select"]').should("have.value", locale).and("not.be.disabled");
    cy.request("/api/current-user/locale/").its("body.preference").should("eq", locale);
    cy.visit(`/projects/${fixture.project_id}/data`);
    cy.get('[data-testid="dm-label-all-toolbar"]', { timeout: 30000 }).should("be.visible");
  };

  for (const locale of ["en-US", "zh-CN"] as const) {
    it(`explains the unassigned Manager's read-only editor in ${locale}`, () => {
      enterProject(locale, "manager");
      cy.intercept({ method: "POST", pathname: /\/api\/dm\/actions\/?$/, query: { id: "next_task" } }).as("managerTask");
      cy.get('[data-testid="dm-label-all-toolbar"]').click();
      cy.wait("@managerTask").then(({ response }) => {
        expect(response?.statusCode).to.eq(200);
        expect(response?.body.can_label).to.eq(false);
      });
      cy.contains(locale === "zh-CN" ? "此任务为只读。请先联系项目管理员，将任务分配给你后再标注。"
        : "This task is read-only. Ask a project manager to assign it to you before labeling.").should("be.visible");
      cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]').should("be.disabled");
      cy.get('[data-testid="bottombar-submit-button"]').should("be.disabled");
    });

    it(`opens a real assigned task through the ${locale} labeling toolbar`, () => {
      enterProject(locale);
      cy.get("body").should("contain.text", locale === "zh-CN" ? "已提交标注:" : "Submitted annotations:");
      cy.intercept({ method: "POST", pathname: /\/api\/dm\/actions\/?$/, query: { id: "next_task" } }).as("nextTask");
      cy.get('[data-testid="dm-label-all-toolbar"]').click();
      cy.wait("@nextTask").then(({ response }) => {
        expect(response?.statusCode).to.eq(200);
        expect(response?.body.id).to.eq(fixture.tasks.a.id);
      });
      cy.get('[data-testid="bottombar-submit-button"]', { timeout: 30000 }).should("be.visible")
        .and("contain.text", locale === "zh-CN" ? "提交" : "Submit");
      cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]').should("not.be.disabled").check();
      cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]').should("be.checked");
      cy.get("body").should("not.contain.text", "Project manager role is required for this operation.");
    });

    for (const statusCode of [403, 404, 500]) {
      it(`shows ${locale} recovery controls instead of completion after queue HTTP ${statusCode}`, () => {
        enterProject(locale);
        cy.intercept({ method: "POST", pathname: /\/api\/dm\/actions\/?$/, query: { id: "next_task" } }).as("retryTask");
        cy.intercept({ method: "POST", pathname: /\/api\/dm\/actions\/?$/, query: { id: "next_task" }, times: 1 }, {
          statusCode, body: { detail: "Synthetic labeling queue unavailable" },
        }).as("queueFailure");
        cy.get('[data-testid="dm-label-all-toolbar"]').click();
        cy.wait("@queueFailure");
        cy.get("body").should("contain.text", locale === "zh-CN" ? "无法读取标注任务" : "Unable to load labeling tasks")
          .and("not.contain.text", locale === "zh-CN" ? "当前队列中没有可标注的任务。" : "No tasks are currently available in this queue.");
        cy.contains("button", locale === "zh-CN" ? "重试" : "Retry").should("be.visible");
        cy.contains("button", locale === "zh-CN" ? "返回任务列表" : "Back to task list").should("be.visible");
        if (statusCode === 500) {
          cy.contains("button", locale === "zh-CN" ? "重试" : "Retry").click();
          cy.wait("@retryTask").then(({ response }) => {
            expect(response?.statusCode).to.eq(200);
            expect(response?.body.id).to.eq(fixture.tasks.a.id);
          });
          cy.get('[data-testid="bottombar-submit-button"]').should("be.visible");
          cy.get("body").should("not.contain.text", locale === "zh-CN" ? "无法读取标注任务" : "Unable to load labeling tasks");
        } else {
          cy.contains("button", locale === "zh-CN" ? "返回任务列表" : "Back to task list").click();
          cy.location("search").should("not.contain", "labeling=1");
          cy.get('[data-testid="dm-label-all-toolbar"]').should("be.visible");
        }
      });
    }
  }
});
