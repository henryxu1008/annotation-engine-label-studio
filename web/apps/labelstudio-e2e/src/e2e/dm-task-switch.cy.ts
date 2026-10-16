/// <reference types="cypress" />

describe("Cross-task draft protection", () => {
  let fixture: any;
  let releasePending: (() => void) | undefined;
  const drafts = { method: /POST|PATCH/, url: /\/api\/(?:tasks\/\d+\/drafts|drafts\/\d+)\/?(?:\?.*)?$/ };
  const choice = (value: string) => cy.get(`#label-studio-dm input[name="${value}"]`);
  const row = (index: number) => cy.contains('[data-testid="table-row"]', `Independent task ${index}`);
  const ready = (id: number) => cy.window({ timeout: 30000 }).should((win: any) => {
    expect(Number(win.Htx?.task?.id)).to.eq(id);
    expect(win.Htx.isLoading).to.eq(false);
    expect(win.dataManager.store.loadingData).to.eq(false);
    expect(win.dataManager.store.taskNavigationPending).to.eq(false);
    expect(win.Htx.annotationStore.selected.autosave).to.be.a("function");
  });
  const open = (scope: any, locale: string) => {
    cy.loginAs(fixture.users.annotator_a.email, fixture.password, "/user/account/personal-info");
    cy.get('[data-testid="language-preference-select"]').select(locale);
    cy.get('[data-testid="language-preference-select"]').should("have.value", locale).and("not.be.disabled");
    cy.visit(`/projects/${scope.project_id}/data?task=${scope.task_ids[0]}`);
    ready(scope.task_ids[0]);
  };
  before(() => cy.readFile(".enterprise-e2e.json").then((data) => { fixture = data; }));
  afterEach(() => { releasePending?.(); releasePending = undefined; });

  for (const locale of ["en-US", "zh-CN"]) {
    it(`retains failed drafts on task A and saves separate task results on retry in ${locale}`, { defaultCommandTimeout: 30000 }, () => {
      cy.task("createEnterpriseE2EUndoScope", { taskCount: 3 }).then((scope: any) => {
        open(scope, locale);
        let offline = true;
        cy.intercept(drafts, (request) => {
          request.alias = offline ? "failedTaskSave" : "confirmedTaskSave";
          if (offline) request.reply({ forceNetworkError: true });
        });
        choice("Positive").check();
        row(2).click();
        cy.wait("@failedTaskSave");
        ready(scope.task_ids[0]);
        cy.location("search").should("contain", `task=${scope.task_ids[0]}`);
        choice("Positive").should("be.checked");
        cy.get('[data-testid="draft-save-status"]').should("contain.text", locale === "zh-CN" ? "草稿保存失败" : "Draft not saved");
        cy.get('[data-testid="draft-save-retry"]').should("contain.text", locale === "zh-CN" ? "重试保存" : "Retry save");
        cy.then(() => { offline = false; });
        row(2).click();
        cy.wait("@confirmedTaskSave").then(({ request, response }) => {
          expect(response?.statusCode).to.be.oneOf([200, 201]);
          expect(request.url).to.contain(`/tasks/${scope.task_ids[0]}/drafts`);
          expect(request.body.result[0].value.choices).to.deep.eq(["Positive"]);
        });
        ready(scope.task_ids[1]);
        choice("Positive").should("not.be.checked");
        choice("Negative").should("not.be.checked").check();
        row(1).click();
        cy.wait("@confirmedTaskSave").then(({ request, response }) => {
          expect(response?.statusCode).to.be.oneOf([200, 201]);
          expect(request.url).to.contain(`/tasks/${scope.task_ids[1]}/drafts`);
          expect(request.body.result[0].value.choices).to.deep.eq(["Negative"]);
        });
        ready(scope.task_ids[0]);
        choice("Positive").should("be.checked");
        choice("Negative").should("not.be.checked");
        row(2).click();
        ready(scope.task_ids[1]);
        cy.reload();
        ready(scope.task_ids[1]);
        choice("Negative").should("be.checked");
        choice("Positive").should("not.be.checked");
      });
    });

    it(`waits for pending saves and newer edits before selecting another task in ${locale}`, { defaultCommandTimeout: 30000 }, () => {
      cy.task("createEnterpriseE2EUndoScope", { taskCount: 3 }).then((scope: any) => {
        open(scope, locale);
        let requests = 0;
        let release: () => void;
        const held = new Cypress.Promise<void>((resolve) => { release = resolve; releasePending = resolve; });
        cy.intercept(drafts, (request) => {
          requests += 1;
          request.alias = requests === 1 ? "pendingTaskSave" : "latestTaskSave";
          if (requests === 1) request.on("before:response", () => held);
          expect(request.url).to.match(new RegExp(`/tasks/${scope.task_ids[0]}/drafts|/drafts/\\d+`));
        });
        choice("Positive").check();
        row(2).click();
        cy.wrap(null).should(() => expect(requests).to.eq(1));
        cy.location("search").should("contain", `task=${scope.task_ids[0]}`);
        // Editing remains possible while an outgoing save is pending. A newer
        // snapshot must also be confirmed before the original click can finish.
        choice("Negative").check();
        row(3).click();
        cy.location("search").should("contain", `task=${scope.task_ids[0]}`);
        cy.then(() => { release(); releasePending = undefined; });
        cy.wait("@pendingTaskSave").its("response.statusCode").should("be.oneOf", [200, 201]);
        cy.wait("@latestTaskSave").then(({ request, response }) => {
          expect(response?.statusCode).to.be.oneOf([200, 201]);
          expect(request.body.result[0].value.choices).to.deep.eq(["Negative"]);
        });
        ready(scope.task_ids[1]);
        choice("Positive").should("not.be.checked");
        choice("Negative").should("not.be.checked");
        row(1).click();
        ready(scope.task_ids[0]);
        choice("Negative").should("be.checked");
        cy.reload();
        ready(scope.task_ids[0]);
        choice("Negative").should("be.checked");
      });
    });

    it(`restores the current task URL when browser Back cannot save in ${locale}`, { defaultCommandTimeout: 30000 }, () => {
      cy.task("createEnterpriseE2EUndoScope", { taskCount: 3 }).then((scope: any) => {
        open(scope, locale);
        row(2).click();
        ready(scope.task_ids[1]);
        cy.intercept(drafts, { forceNetworkError: true }).as("failedBackSave");
        choice("Negative").check();
        cy.go("back");
        cy.wait("@failedBackSave");
        ready(scope.task_ids[1]);
        cy.location("search").should("contain", `task=${scope.task_ids[1]}`);
        choice("Negative").should("be.checked");
      });
    });
  }
});
