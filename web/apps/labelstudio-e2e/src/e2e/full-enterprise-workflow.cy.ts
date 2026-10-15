/// <reference types="cypress" />

type Fixture = {
  password: string;
  users: {
    manager: { id: number; email: string };
    annotator_a: { id: number; email: string };
    annotator_b: { id: number; email: string };
    reviewer: { id: number; email: string };
  };
  full_flow: {
    project_id: number;
    tasks: {
      a: { id: number };
      b: { id: number };
      stale: { id: number };
    };
  };
};

describe("full enterprise collaboration browser workflow", () => {
  const locale = Cypress.env("fullFlowLocale") || "en-US";
  if (locale !== "en-US" && locale !== "zh-CN") throw new Error(`Unsupported full-flow locale: ${locale}`);
  const copy = locale === "zh-CN" ? {
    members: "项目成员",
    enabled: "已启用",
    disabled: "已停用",
    assigned: "已分配",
    rejected: "已驳回",
    approved: "已通过",
    releasedRevision2: "已发布修订版 2",
    closeModal: "关闭弹窗",
  } : {
    members: "Project Members",
    enabled: "Enabled",
    disabled: "Disabled",
    assigned: "Assigned",
    rejected: "Rejected",
    approved: "Approved",
    releasedRevision2: "Released revision 2",
    closeModal: "Close modal",
  };
  let fixture: Fixture;

  before(() => {
    cy.readFile(".enterprise-e2e.json").then((data) => {
      fixture = data as Fixture;
    });
  });

  const projectId = () => fixture.full_flow.project_id;
  const dataPage = () => `/projects/${projectId()}/data`;
  const settingsPage = () => `/projects/${projectId()}/settings/`;
  const membersPage = () => `/projects/${projectId()}/settings/members`;

  const loginActor = (email: string, nextPath: string) => {
    cy.loginAs(email, fixture.password, nextPath);
    cy.get('[data-testid="user-menu-trigger"]', { timeout: 30000 }).click();
    cy.get('[data-testid="menu-language-select"]').should('be.enabled').then(($select) => {
      if ($select.val() !== locale) cy.get('[data-testid="menu-language-select"]').should('be.enabled').select(locale);
    });
    cy.get("html").should("have.attr", "lang", locale);
    cy.get('[data-testid="user-menu-trigger"]').click();
  };

  const loginAndVisit = (email: string, path: string) => {
    loginActor(email, path);
    cy.visit(path);
    cy.location("pathname", { timeout: 30000 }).should("eq", path.split("?")[0]);
  };

  const openTaskPanel = (email: string, taskId: number) => {
    loginActor(email, dataPage());
    cy.visit(`${dataPage()}?task=${taskId}`);
    cy.location("pathname", { timeout: 30000 }).should("eq", dataPage());
    cy.window({ timeout: 30000 }).should((win) => {
      expect(win.dataManager?.store?.taskStore?.selected?.id).to.eq(taskId);
    });
  };

  const addMember = (userId: number, role: "annotator" | "reviewer") => {
    cy.get('[data-testid="member-user-select"]').select(String(userId));
    cy.get('[data-testid="member-role-select"]').select(role);
    cy.get('[data-testid="member-add"]').click();
  };

  const openAssignmentManager = (taskId: number) => {
    openTaskPanel(fixture.users.manager.email, taskId);
    cy.get('[data-testid="manage-task-assignments"]', { timeout: 30000 })
      .should("be.visible")
      .and("not.be.disabled")
      .click();
    cy.get('[data-testid="assignment-manager"]', { timeout: 30000 }).should("exist");
  };

  const assignTask = (taskId: number, assigneeId: number, email: string) => {
    openAssignmentManager(taskId);
    cy.get('[data-testid="assignment-assignee-select"]').select(String(assigneeId));
    cy.get('[data-testid="assignment-submit"]').click();
    cy.contains('[data-testid^="assignment-row-"]', email)
      .should("exist")
      .and("contain.text", copy.assigned);
    cy.get('[data-testid="modal-close-button"]').should("have.attr", "aria-label", copy.closeModal).first().click();
    cy.get('[data-testid="assignment-manager"]').should("not.exist");
  };

  const openAssignedEditor = (email: string, taskId: number, taskText: string) => {
    openTaskPanel(email, taskId);
    cy.contains(taskText, { timeout: 30000 }).should("be.visible");
    cy.get('[data-testid="bottombar-submit-button"]', { timeout: 30000 }).should("be.visible");
  };

  const reopenAssignedEditor = (email: string, taskId: number, taskText: string, annotationId: () => number) => {
    openTaskPanel(email, taskId);
    cy.contains(taskText, { timeout: 30000 }).should("be.visible");
    // Opening a task can select a new annotation or draft. Select the submitted
    // annotation explicitly before editing its next revision.
    cy.then(() => {
      cy.get(`[data-annotation-id="${annotationId()}"]`, { timeout: 30000 }).should("be.visible").click();
    });
    cy.window({ timeout: 30000 }).should((win) => {
      expect(String(win.Htx?.annotationStore?.selected?.pk), "selected submitted annotation").to.eq(
        String(annotationId()),
      );
    });
    cy.get('[data-testid="bottombar-update-button"]', { timeout: 30000 }).should("be.visible");
    // A visible editor can still be hydrating the previously submitted result.
    // Confirm revision 1 is loaded before changing it to revision 2.
    cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]', {
      timeout: 30000,
    }).should("be.checked");
  };

  const choose = (value: "Positive" | "Negative", taskId: number) => {
    // Controls can render before startAutosave attaches its next-tick snapshot
    // listener. Wait for the live editor, not just a visible Submit button.
    cy.window({ timeout: 30000 }).should((win) => {
      const editor = win.Htx;
      const annotation = editor?.annotationStore?.selected;
      expect(Number(editor?.task?.id), "editor task").to.eq(taskId);
      expect(editor?.isLoading, "editor initialization complete").to.eq(false);
      expect(annotation?.editable, "editable annotation").to.eq(true);
      expect(annotation?.isReadOnly(), "annotation is not read-only").to.eq(false);
      expect(annotation?.history?.isFrozen, "annotation history is ready").to.eq(false);
      expect(annotation?.autosave, "autosave listener attached").to.be.a("function");
      expect(annotation?.autosave?.paused, "autosave is not paused").not.to.eq(true);
    });

    const choice = `#label-studio-dm input[type="checkbox"][name="${value}"]`;
    cy.get(choice, { timeout: 30000 }).should("not.be.disabled");
    // React can replace the input between a Cypress query and its callback.
    // Resolve the mounted input at the instant of the native click, then
    // confirm the selection before attempting Draft/Submit/Update.
    cy.window().then((win) => {
      const input = win.document.querySelector<HTMLInputElement>(choice);
      expect(input, "current choice input").not.to.eq(null);
      expect(input?.isConnected, "choice input is mounted").to.eq(true);
      expect(input?.disabled, "choice input is enabled").to.eq(false);
      input!.click();
    });
    cy.get(choice, { timeout: 30000 }).should("be.checked");
    cy.window({ timeout: 30000 }).should((win) => {
      const results = win.Htx.annotationStore.selected.serializeAnnotation();
      expect(results.some((result) => result.value?.choices?.includes(value)), "editor selected choice").to.eq(true);
    });
  };

  const closeModal = () => {
    cy.get('[data-testid="modal-close-button"]').should("have.attr", "aria-label", copy.closeModal).first().click();
  };

  const waitForDraft = (taskId: number, attempts = 60): Cypress.Chainable<Cypress.Response<unknown>> => {
    return cy.request({
      url: `/api/tasks/${taskId}/drafts`,
      failOnStatusCode: false,
    }).then((response) => {
      if (response.status === 200 && Array.isArray(response.body) && response.body.length > 0) {
        return cy.wrap(response, { log: false });
      }
      if (attempts <= 1) {
        throw new Error(`Draft for task ${taskId} did not become visible through the API`);
      }
      return cy.wait(250, { log: false }).then(() => waitForDraft(taskId, attempts - 1));
    });
  };

  const saveDraftImmediately = (taskId: number) => {
    cy.window({ timeout: 30000 }).then(async (win) => {
      const editor = win.Htx;
      const annotation = editor?.annotationStore?.selected;
      expect(Number(editor?.task?.id), "editor task before draft save").to.eq(taskId);
      expect(annotation?.editable, "editable annotation before draft save").to.eq(true);

      annotation.autosave?.cancel?.();
      if (annotation.isDraftSaving) {
        return;
      }
      await annotation.saveDraftImmediatelyWithResults();
    });

    cy.window({ timeout: 30000 }).should((win) => {
      const annotation = win.Htx?.annotationStore?.selected;
      expect(annotation?.isDraftSaving, "editor draft save settled").to.eq(false);
    });
  };


  const assertRestrictedManagerWrites = () => {
    cy.request({
      url: `/api/projects/${projectId()}/export`,
      failOnStatusCode: false,
    }).its("status").should("be.oneOf", [403, 404]);

    cy.request({
      url: "/api/storages/s3/",
      method: "POST",
      failOnStatusCode: false,
      body: {
        project: projectId(),
        bucket: "full-flow-denied",
        title: "should-not-create",
      },
    }).its("status").should("be.oneOf", [403, 404]);

    cy.request({
      url: "/api/webhooks/",
      method: "POST",
      failOnStatusCode: false,
      body: {
        project: projectId(),
        url: "http://127.0.0.1:9/full-flow-denied",
      },
    }).its("status").should("be.oneOf", [403, 404]);

    cy.request({
      url: `/api/dm/actions/?project=${projectId()}`,
      method: "POST",
      failOnStatusCode: false,
      body: {},
    }).its("status").should("be.oneOf", [403, 404]);
  };

  it(`runs the full Manager to Reviewer to release workflow in ${locale}`, () => {
    // 1-4: Manager adds isolated project members and roles through UI.
    loginAndVisit(fixture.users.manager.email, settingsPage());
    cy.get('a[href*="/settings/members"]', { timeout: 30000 }).first().click();
    cy.location("pathname", { timeout: 30000 }).should("eq", membersPage());
    cy.get('[data-testid="project-members-settings"]', { timeout: 30000 }).should("be.visible");
    cy.get('[data-testid="project-members-settings"]').should("contain.text", copy.members);

    addMember(fixture.users.annotator_a.id, "annotator");
    cy.contains("tr", fixture.users.annotator_a.email).should("contain.text", copy.enabled);

    addMember(fixture.users.annotator_b.id, "annotator");
    cy.contains("tr", fixture.users.annotator_b.email).should("contain.text", copy.enabled);

    addMember(fixture.users.reviewer.id, "reviewer");
    cy.contains("tr", fixture.users.reviewer.email).should("contain.text", copy.enabled);

    // 5-6: Manager assigns tasks through the product UI.
    assignTask(fixture.full_flow.tasks.a.id, fixture.users.annotator_a.id, fixture.users.annotator_a.email);
    assignTask(fixture.full_flow.tasks.b.id, fixture.users.annotator_b.id, fixture.users.annotator_b.email);

    // 7-11: task isolation and reviewer labeling denial.
    loginAndVisit(fixture.users.annotator_a.email, dataPage());
    cy.request(`/api/tasks/${fixture.full_flow.tasks.a.id}/`).its("status").should("eq", 200);
    cy.request({ url: `/api/tasks/${fixture.full_flow.tasks.b.id}/`, failOnStatusCode: false })
      .its("status").should("eq", 404);
    assertRestrictedManagerWrites();

    loginAndVisit(fixture.users.annotator_b.email, dataPage());
    cy.request(`/api/tasks/${fixture.full_flow.tasks.b.id}/`).its("status").should("eq", 200);
    cy.request({ url: `/api/tasks/${fixture.full_flow.tasks.a.id}/`, failOnStatusCode: false })
      .its("status").should("eq", 404);

    loginAndVisit(fixture.users.reviewer.email, dataPage());
    cy.request({ url: `/api/tasks/${fixture.full_flow.tasks.a.id}/`, failOnStatusCode: false })
      .its("status").should("eq", 404);
    cy.get('[data-testid="dm-label-all-toolbar"], [data-testid="dm-label-all-tasks-button"]').should("not.exist");

    // 12-14: Annotator A edits in the real Editor; draft exists before explicit Submit.
    openAssignedEditor(
      fixture.users.annotator_a.email,
      fixture.full_flow.tasks.a.id,
      "Full flow Annotator A task",
    );
    choose("Positive", fixture.full_flow.tasks.a.id);
    saveDraftImmediately(fixture.full_flow.tasks.a.id);

    let savedDraftId: number;
    let annotationId: number;
    waitForDraft(fixture.full_flow.tasks.a.id).then((drafts) => {
      expect(drafts.status).to.eq(200);
      expect(drafts.body.length).to.be.greaterThan(0);
      savedDraftId = drafts.body[0].id;
      cy.window({ timeout: 30000 }).should((win) => {
        expect(win.Htx?.annotationStore?.selected?.draftId, "current editor saved draft").to.eq(savedDraftId);
      });
    });
    cy.request(`/api/submissions/?project=${projectId()}`).then((submissions) => {
      expect(submissions.status).to.eq(200);
      expect(submissions.body).to.have.length(0);
    });

    cy.intercept("POST", `**/api/tasks/${fixture.full_flow.tasks.a.id}/annotations*`).as("submitRevision1");
    cy.get('[data-testid="bottombar-submit-button"]', { timeout: 30000 }).should("be.visible").click();
    cy.wait("@submitRevision1").then((submission) => {
      expect(submission.request.body.draft_id, "submitted draft id").to.eq(savedDraftId);
      expect(submission.response?.statusCode).to.be.oneOf([200, 201]);
      annotationId = submission.response?.body.id;
      expect(annotationId, "submitted annotation id").to.be.a("number");
    });
    cy.request(`/api/tasks/${fixture.full_flow.tasks.a.id}/drafts`).its("body").should("have.length", 0);

    let revision1Id: number;
    let revision1Hash: string;
    cy.request(`/api/submissions/?project=${projectId()}`).then((submissions) => {
      expect(submissions.status).to.eq(200);
      expect(submissions.body).to.have.length(1);
      expect(submissions.body[0].revision).to.eq(1);
      expect(submissions.body[0].status).to.eq("pending");
      expect(submissions.body[0].submitted_by.id).to.eq(fixture.users.annotator_a.id);
      revision1Id = submissions.body[0].id;
      revision1Hash = submissions.body[0].result_hash;
    });

    // 15-16: Reviewer rejects revision 1 in the browser workspace.
    loginAndVisit(fixture.users.reviewer.email, dataPage());
    cy.get('[data-testid="open-review-workspace"]', { timeout: 30000 }).click();
    cy.get('[data-testid="reviewer-workspace"]', { timeout: 30000 }).should("exist");
    cy.then(() => {
      cy.get(`[data-testid="review-submission-${revision1Id}"]`).click();
    });
    cy.get('[data-testid="review-result-snapshot"]').should("contain.text", "Positive");
    cy.get('[data-testid="review-reject-reason"]').type("Full flow correction required");
    cy.get('[data-testid="review-reject"]').click();
    cy.then(() => {
      cy.get(`[data-testid="review-history-${revision1Id}"]`, { timeout: 30000 })
        .should("contain.text", copy.rejected);
    });
    closeModal();

    // 17-20: Annotator A changes the real annotation and submits revision 2 through Editor Update.
    reopenAssignedEditor(
      fixture.users.annotator_a.email,
      fixture.full_flow.tasks.a.id,
      "Full flow Annotator A task",
      () => annotationId,
    );
    choose("Negative", fixture.full_flow.tasks.a.id);
    cy.intercept("PATCH", `**/api/annotations/**`).as("submitRevision2");
    cy.get('[data-testid="bottombar-update-button"]', { timeout: 30000 }).should("be.visible").click();
    cy.wait("@submitRevision2").its("response.statusCode").should("eq", 200);

    let revision2Id: number;
    let revision2Hash: string;
    cy.request(`/api/submissions/?project=${projectId()}`).then((submissions) => {
      expect(submissions.status).to.eq(200);
      const rows = submissions.body;
      expect(rows).to.have.length(2);
      const rev1 = rows.find((item) => item.revision === 1);
      const rev2 = rows.find((item) => item.revision === 2);
      expect(rev1.status).to.eq("rejected");
      expect(rev1.result_hash).to.eq(revision1Hash);
      expect(JSON.stringify(rev1.result_snapshot)).to.contain("Positive");
      expect(rev2.status).to.eq("pending");
      expect(JSON.stringify(rev2.result_snapshot)).to.contain("Negative");
      revision2Id = rev2.id;
      revision2Hash = rev2.result_hash;
      expect(revision2Hash).not.to.eq(revision1Hash);
    });

    loginAndVisit(fixture.users.reviewer.email, dataPage());
    cy.get('[data-testid="open-review-workspace"]', { timeout: 30000 }).click();
    cy.then(() => {
      cy.get(`[data-testid="review-submission-${revision2Id}"]`).click();
    });
    cy.get('[data-testid="review-result-snapshot"]').should("contain.text", "Negative");
    cy.get('[data-testid="review-approve"]').click();
    cy.get('[data-testid="review-status"]', { timeout: 30000 }).should("contain.text", copy.approved);
    closeModal();

    // 21-24: Manager sees immutable history and releases approved revision 2.
    loginAndVisit(fixture.users.manager.email, dataPage());
    cy.get('[data-testid="open-release-workspace"]', { timeout: 30000 }).click();
    cy.get('[data-testid="submission-release-workspace"]', { timeout: 30000 }).should("exist");

    cy.then(() => {
      cy.get(`[data-testid="release-submission-${revision1Id}"]`)
        .should("contain.text", copy.rejected)
        .click();
    });
    cy.get('[data-testid="release-approved-submission"]').should("not.exist");

    cy.then(() => {
      cy.get(`[data-testid="release-submission-${revision2Id}"]`)
        .should("contain.text", copy.approved)
        .click();
    });
    cy.then(() => {
      cy.get('[data-testid="release-result-hash"]').should("contain.text", revision2Hash);
      cy.get('[data-testid="release-result-snapshot"]').should("contain.text", "Negative");
      cy.get('[data-testid="release-approved-submission"]').click();
      cy.get('[data-testid="release-success"]', { timeout: 30000 })
        .should("contain.text", copy.releasedRevision2)
        .and("contain.text", revision2Hash);
    });
    closeModal();

    // 25-29: prove Manager UI revocation, then exercise a truly stale writable Editor.
    loginAndVisit(fixture.users.manager.email, membersPage());
    cy.contains("tr", fixture.users.annotator_a.email).within(() => {
      cy.get('[data-testid^="member-toggle-"]').click();
    });
    cy.contains("tr", fixture.users.annotator_a.email).should("contain.text", copy.disabled);
    cy.contains("tr", fixture.users.annotator_a.email).within(() => {
      cy.get('[data-testid^="member-toggle-"]').click();
    });
    cy.contains("tr", fixture.users.annotator_a.email).should("contain.text", copy.enabled);

    // Use a fresh, never-annotated task so the active assignment is definitely writable before revocation.
    assignTask(
      fixture.full_flow.tasks.stale.id,
      fixture.users.annotator_a.id,
      fixture.users.annotator_a.email,
    );

    openAssignedEditor(
      fixture.users.annotator_a.email,
      fixture.full_flow.tasks.stale.id,
      "Full flow stale editor task",
    );

    choose("Positive", fixture.full_flow.tasks.stale.id);
    saveDraftImmediately(fixture.full_flow.tasks.stale.id);
    waitForDraft(fixture.full_flow.tasks.stale.id);

    cy.request(`/api/tasks/${fixture.full_flow.tasks.stale.id}/`).then((taskResponse) => {
      expect(taskResponse.status).to.eq(200);
      expect(taskResponse.body.assignment_id).to.be.a("number");
      expect(taskResponse.body.assignment_version).to.be.a("number");
    });

    // Keep this confirmed-writable real Editor open while membership is revoked out-of-band.
    cy.task("setEnterpriseE2EMember", {
      actor: "annotator_a",
      enabled: false,
      projectId: projectId(),
    });

    cy.intercept("POST", `**/api/tasks/${fixture.full_flow.tasks.stale.id}/annotations*`).as("staleSubmit");
    cy.get('[data-testid="bottombar-submit-button"]', { timeout: 30000 }).should("be.visible").click();
    cy.wait("@staleSubmit").then((interception) => {
      expect(interception.response?.statusCode).to.be.oneOf([403, 404, 409]);
      expect(interception.response?.statusCode).not.to.eq(500);
    });

    cy.request({
      url: `/api/tasks/${fixture.full_flow.tasks.stale.id}/`,
      failOnStatusCode: false,
    }).its("status").should("eq", 404);

    cy.reload();
    cy.get('[data-testid="bottombar-submit-button"]').should("not.exist");

    // 30-34: role separation / hardened mutation surfaces remain server-authoritative.
    cy.then(() => {
      cy.request({
        url: `/api/submissions/${revision2Id}/review/`,
        method: "POST",
        failOnStatusCode: false,
        body: { decision: "approved" },
      }).its("status").should("eq", 403);
    });
    cy.request({
      url: `/api/projects/${projectId()}/members/`,
      failOnStatusCode: false,
    }).its("status").should("be.oneOf", [403, 404]);
    loginAndVisit(fixture.users.reviewer.email, dataPage());
    cy.get('[data-testid="manage-task-assignments"]').should("not.exist");
    cy.get('[data-testid="open-release-workspace"]').should("not.exist");
    cy.get('[data-testid="dm-label-all-toolbar"], [data-testid="dm-label-all-tasks-button"]').should("not.exist");
    cy.request({
      url: `/api/task-assignments/?project=${projectId()}`,
      method: "POST",
      failOnStatusCode: false,
      body: { task: fixture.full_flow.tasks.a.id, assignee: fixture.users.annotator_b.id },
    }).its("status").should("eq", 403);
    cy.request({
      url: `/api/projects/${projectId()}/members/`,
      failOnStatusCode: false,
    }).its("status").should("eq", 403);
    assertRestrictedManagerWrites();

    // Restore membership for repeatable local reruns; assignment intentionally remains cancelled.
    cy.task("setEnterpriseE2EMember", {
      actor: "annotator_a",
      enabled: true,
      projectId: projectId(),
    });
  });
});

export {};
