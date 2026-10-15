/// <reference types="cypress" />

type StreamFixture = {
  password: string;
  users: { annotator_a: { id: number; email: string }; annotator_b: { id: number; email: string }; manager: { email: string } };
  project_id: number;
  tasks: { a: { id: number } };
};

describe("Annotator labeling entry and queue failures", () => {
  let fixture: StreamFixture;
  before(() => cy.readFile(".enterprise-e2e.json").then((value) => { fixture = value as StreamFixture; }));

  const enterProject = (locale: "en-US" | "zh-CN", actor: "annotator_a" | "manager" = "annotator_a", projectId = fixture.project_id) => {
    cy.loginAs(fixture.users[actor].email, fixture.password, "/user/account/personal-info");
    cy.get('[data-testid="language-preference-select"]').select(locale);
    cy.get('[data-testid="language-preference-select"]').should("have.value", locale).and("not.be.disabled");
    cy.request("/api/current-user/locale/").its("body.preference").should("eq", locale);
    cy.visit(`/projects/${projectId}/data`);
    cy.get('[data-testid="dm-label-all-toolbar"]', { timeout: 30000 }).should("be.visible");
  };

  for (const locale of ["en-US", "zh-CN"] as const) {
    it(`leaves a completed single-task queue without an obsolete draft save in ${locale}`, () => {
      cy.task("createEnterpriseE2EUndoScope").then((scope: any) => {
        enterProject(locale, "annotator_a", scope.project_id);
        cy.get('[data-testid="dm-label-all-toolbar"]').click();
        cy.get('[data-testid="bottombar-submit-button"]', { timeout: 30000 }).should("be.visible");
        let submitted = false;
        let obsoleteDraftWrites = 0;
        cy.intercept({ method: /POST|PATCH/, url: /\/api\/(?:tasks\/\d+\/drafts|drafts\/\d+)\/?(?:\?.*)?$/ }, (request) => {
          if (submitted) obsoleteDraftWrites += 1;
        }).as("queueDraft");
        cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]').check();
        cy.wait("@queueDraft", { timeout: 20000 }).its("response.statusCode").should("be.oneOf", [200, 201]);
        cy.intercept({ method: "POST", pathname: new RegExp(`^/api/tasks/${scope.task_id}/annotations/?$`) }).as("queueSubmit");
        cy.get('[data-testid="bottombar-submit-button"]').click();
        cy.wait("@queueSubmit").its("response.statusCode").should("eq", 201);
        cy.contains(locale === "zh-CN" ? "当前队列中没有可标注的任务。" : "No tasks are currently available in this queue.").should("be.visible");
        cy.window().should((win: any) => {
          const annotation = win.Htx.annotationStore.selected;
          expect(annotation.submissionStarted).to.eq(0);
          expect(annotation.history.hasChanges).to.eq(true);
          expect(annotation.hasUnsavedDraft).to.eq(false);
        });
        cy.then(() => { submitted = true; });
        cy.get('a[href="/projects"]').click();
        cy.location("pathname").should("eq", "/projects");
        cy.then(() => expect(obsoleteDraftWrites).to.eq(0));
      });
    });

    it(`explains the unassigned Manager's read-only editor in ${locale}`, () => {
      enterProject(locale, "manager");
      cy.intercept({ method: "GET", pathname: `/api/tasks/${fixture.tasks.a.id}` }).as("managerTask");
      cy.visit(`/projects/${fixture.project_id}/data?task=${fixture.tasks.a.id}`);
      cy.wait("@managerTask").then(({ response }) => {
        expect(response?.statusCode).to.eq(200);
        expect(response?.body.can_label).to.eq(false);
      });
      cy.contains(locale === "zh-CN" ? "此任务为只读。请先联系项目管理员，将任务分配给你后再标注。"
        : "This task is read-only. Ask a project manager to assign it to you before labeling.").should("be.visible");
      cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]').should("be.disabled");
      cy.get('[data-testid="bottombar-submit-button"]').should("be.disabled");
      const nextLocale = locale === "zh-CN" ? "en-US" : "zh-CN";
      cy.get('[data-testid="user-menu-trigger"]').click();
      cy.get("#menu-display-language").select(nextLocale).should("have.value", nextLocale).and("not.be.disabled");
      cy.contains(nextLocale === "zh-CN" ? "此任务为只读。请先联系项目管理员，将任务分配给你后再标注。"
        : "This task is read-only. Ask a project manager to assign it to you before labeling.").should("be.visible");
      cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]').should("be.disabled");
      cy.get('[data-testid="bottombar-submit-button"]').should("be.disabled");
      cy.location("search").should("contain", `task=${fixture.tasks.a.id}`);
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

  for (const locale of ["en-US", "zh-CN"] as const) {
    it(`resumes and submits a shared assignment after peer completion and Unskip in ${locale}`, () => {
      let projectId: number;
      let taskId: number;
      const post = (url: string, body: Record<string, unknown>) => cy.getCookie("csrftoken").then((cookie) =>
        cy.request({ method: "POST", url, body, headers: { "X-CSRFToken": cookie?.value ?? "" } }));

      cy.loginAs(fixture.users.manager.email, fixture.password, "/projects/");
      post("/api/projects/", {
        title: `Assigned queue resume ${locale} ${Date.now()}`,
        label_config: '<View><Text name="text" value="$text"/><Choices name="sentiment" toName="text"><Choice value="Positive"/><Choice value="Negative"/></Choices></View>',
        maximum_annotations: 1, skip_queue: "REQUEUE_FOR_ME", is_published: true,
      }).then(({ body }) => { projectId = body.id; });
      cy.then(() => post(`/api/projects/${projectId}/members/`, { user_id: fixture.users.annotator_a.id, role: "annotator" }));
      cy.then(() => post(`/api/projects/${projectId}/members/`, { user_id: fixture.users.annotator_b.id, role: "annotator" }));
      cy.then(() => post("/api/tasks/", { project: projectId, data: { text: "Synthetic shared assignment" } }))
        .then(({ body }) => { taskId = body.id; });
      cy.then(() => post("/api/task-assignments/", { task: taskId, assignee: fixture.users.annotator_a.id }));
      cy.then(() => post("/api/task-assignments/", { task: taskId, assignee: fixture.users.annotator_b.id }));
      cy.loginAs(fixture.users.annotator_b.email, fixture.password, "/projects/");
      cy.then(() => cy.request(`/api/tasks/${taskId}/`)).then(({ body }) => post(`/api/tasks/${taskId}/annotations/`, {
        result: [{ from_name: "sentiment", to_name: "text", type: "choices", value: { choices: ["Positive"] } }],
        assignment_id: body.assignment_id, assignment_version: body.assignment_version,
      })).its("status").should("eq", 201);

      cy.then(() => enterProject(locale, "annotator_a", projectId));
      cy.intercept({ method: "POST", pathname: /\/api\/dm\/actions\/?$/, query: { id: "next_task" } }).as("sharedNext");
      cy.get('[data-testid="dm-label-all-toolbar"]').click();
      cy.wait("@sharedNext").then(({ response }) => {
        expect(response?.statusCode).to.eq(200);
        expect(response?.body.id).to.eq(taskId);
        expect(response?.body.can_label).to.eq(true);
        expect(response?.body.is_labeled).to.eq(true);
      });
      cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]').should("not.be.disabled").check();
      cy.get('[data-testid="bottombar-skip-button"]').should("not.be.disabled").click();
      cy.get('[data-testid="bottombar-unskip-button"]', { timeout: 30000 }).should("be.visible").and("not.be.disabled").click();
      cy.get('[data-testid="bottombar-submit-button"]', { timeout: 30000 }).should("be.visible").and("not.be.disabled");
      cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]').check();
      cy.then(() => cy.intercept({ method: "POST", pathname: new RegExp(`^/api/tasks/${taskId}/annotations/?$`) }).as("resumedSubmission"));
      cy.get('[data-testid="bottombar-submit-button"]').click();
      cy.wait("@resumedSubmission").then(({ response }) => {
        expect(response?.statusCode).to.eq(201);
        expect(response?.body.was_cancelled).to.eq(false);
        expect(response?.body.result[0].value.choices).to.deep.eq(["Positive"]);
      });
    });
  }
});
