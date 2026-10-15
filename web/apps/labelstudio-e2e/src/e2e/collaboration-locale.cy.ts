/// <reference types="cypress" />

type Fixture = {
  password: string;
  project_id: number;
  users: {
    manager: { id: number; email: string };
    annotator_a: { id: number; email: string };
    annotator_b: { id: number; email: string };
    reviewer: { id: number; email: string };
  };
  tasks: {
    c: { id: number };
    review: { id: number; submission_id: number };
    release: { id: number; revision_1_submission_id: number; revision_2_submission_id: number };
  };
};

describe("collaboration language and immutable workflow", () => {
  let fixture: Fixture;
  let activeEmail: string | null = null;
  let originalPreference: "auto" | "en-US" | "zh-CN" = "auto";

  before(() => {
    cy.readFile(".enterprise-e2e.json").then((data) => { fixture = data as Fixture; });
  });

  const loginAndVisit = (email: string, path: string) => {
    activeEmail = email;
    cy.loginAs(email, fixture.password, path);
    cy.request("GET", "/api/current-user/locale/").its("body").then((body) => {
      originalPreference = body.preference ?? "auto";
    });
    cy.visit(path);
    cy.location("pathname", { timeout: 30000 }).should("eq", path.split("?")[0]);
  };

  afterEach(() => {
    if (!activeEmail) return;
    const email = activeEmail;
    activeEmail = null;
    cy.loginAs(email, fixture.password, "/user/account/personal-info");
    cy.visit("/user/account/personal-info");
    cy.get('[data-testid="language-preference-select"]').then(($select) => {
      if ($select.val() === originalPreference) return;
      cy.intercept("PATCH", "**/api/current-user/locale*").as("restoreLocale");
      cy.get('[data-testid="language-preference-select"]').should('be.enabled').select(originalPreference);
      cy.wait("@restoreLocale").its("response.statusCode").should("eq", 200);
    });
    cy.get('[data-testid="language-preference-select"]').should("have.value", originalPreference);
  });

  const selectModalLocale = (value: "en-US" | "zh-CN") => {
    cy.intercept("PATCH", "**/api/current-user/locale*").as("saveLocale");
    cy.get('[data-testid="collaboration-language-select"]').select(value);
    cy.wait("@saveLocale").its("request.body").should("deep.equal", { preference: value });
    cy.get("html").should("have.attr", "lang", value);
  };

  const ensureEnglish = () => {
    cy.get('[data-testid="user-menu-trigger"]').click();
    cy.get('[data-testid="menu-language-select"]').then(($select) => {
      if ($select.val() !== "en-US") cy.get('[data-testid="menu-language-select"]').should('be.enabled').select("en-US");
    });
    cy.get("html").should("have.attr", "lang", "en-US");
    cy.get('[data-testid="user-menu-trigger"]').click();
  };

  it("keeps member role codes stable while translating the live roster", () => {
    const path = `/projects/${fixture.project_id}/settings/members`;
    loginAndVisit(fixture.users.manager.email, path);
    ensureEnglish();
    cy.get('[data-testid="project-members-settings"]', { timeout: 30000 }).should("be.visible");
    cy.get(`[data-testid="member-role-${fixture.users.annotator_a.id}"]`).should("have.value", "annotator");
    let writes = 0;
    cy.intercept({ method: /POST|PATCH|DELETE/, url: "**/api/projects/*/members*" }, () => { writes += 1; });
    cy.get('[data-testid="user-menu-trigger"]').click();
    cy.get('[data-testid="menu-language-select"]').select("zh-CN");
    cy.get("html").should("have.attr", "lang", "zh-CN");
    cy.contains("tr", fixture.users.annotator_a.email)
      .should("contain.text", "标注员").and("contain.text", "已启用");
    cy.get(`[data-testid="member-role-${fixture.users.annotator_a.id}"]`).should("have.value", "annotator");
    cy.then(() => expect(writes, "locale switch member writes").to.eq(0));
  });

  it("preserves selected assignee and assignment version when switching inside its modal", () => {
    const taskId = fixture.tasks.c.id;
    loginAndVisit(fixture.users.manager.email, `/projects/${fixture.project_id}/data?task=${taskId}`);
    ensureEnglish();
    cy.window({ timeout: 30000 }).should((win) => {
      expect(win.dataManager?.store?.taskStore?.selected?.id).to.eq(taskId);
    });
    cy.get('[data-testid="manage-task-assignments"]', { timeout: 30000 }).should("be.visible").and("not.be.disabled").click();
    cy.get('[data-testid="assignment-manager"]', { timeout: 30000 }).should("be.visible");
    cy.get('[data-testid="assignment-assignee-select"]').select(String(fixture.users.annotator_b.id));
    let writes = 0;
    cy.intercept({ method: /POST|DELETE/, url: "**/api/task-assignments*" }, () => { writes += 1; });
    selectModalLocale("zh-CN");
    cy.get('[data-testid="assignment-assignee-select"]').should("have.value", String(fixture.users.annotator_b.id));
    cy.get('[data-testid="assignment-empty"]').should("contain.text", "没有有效分配");
    cy.then(() => expect(writes, "switch must not assign").to.eq(0));
    cy.intercept("POST", "**/api/task-assignments*").as("assign");
    cy.get('[data-testid="assignment-submit"]').click();
    cy.wait("@assign").then(({ request, response }) => {
      expect(request.body.task).to.eq(taskId);
      expect(request.body.assignee).to.eq(fixture.users.annotator_b.id);
      expect(response?.statusCode).to.be.oneOf([200, 201]);
    });
    cy.contains('[data-testid^="assignment-row-"]', fixture.users.annotator_b.email)
      .should("contain.text", "已分配").and("contain.text", "v1");
    cy.contains('[data-testid^="assignment-row-"]', fixture.users.annotator_b.email)
      .find("button").click();
    cy.get('[data-testid="assignment-empty"]').should("contain.text", "没有有效分配");
  });

  it("keeps the typed rejection reason and exact pending hash through a modal switch", () => {
    loginAndVisit(fixture.users.reviewer.email, `/projects/${fixture.project_id}/data`);
    ensureEnglish();
    cy.get('[data-testid="open-review-workspace"]', { timeout: 30000 }).click();
    cy.get('[data-testid="reviewer-workspace"]', { timeout: 30000 }).should("be.visible");
    cy.get(`[data-testid="review-submission-${fixture.tasks.review.submission_id}"]`).click();
    let hash = "";
    cy.get('[data-testid="review-result-hash"]').invoke("text").then((value) => { hash = value; });
    cy.get('[data-testid="review-reject-reason"]').type("Keep user text 原样");
    let writes = 0;
    cy.intercept("POST", "**/api/submissions/*/review*", () => { writes += 1; }).as("review");
    selectModalLocale("zh-CN");
    cy.get('[data-testid="review-reject-reason"]').should("have.value", "Keep user text 原样");
    cy.get('[data-testid="review-status"]').should("contain.text", "待复核");
    cy.get('[data-testid="review-result-hash"]').should(($hash) => expect($hash.text()).to.eq(hash));
    cy.then(() => expect(writes, "switch must not review").to.eq(0));
    cy.get('[data-testid="review-reject"]').click();
    cy.wait("@review").then(({ request, response }) => {
      expect(request.body.decision).to.eq("rejected");
      expect(request.body.reason).to.eq("Keep user text 原样");
      expect(response?.statusCode).to.eq(200);
    });
    cy.get(`[data-testid="review-history-${fixture.tasks.review.submission_id}"]`, { timeout: 30000 })
      .should("contain.text", "已驳回");
  });

  it("releases only the selected approved revision after a live language change", () => {
    loginAndVisit(fixture.users.manager.email, `/projects/${fixture.project_id}/data`);
    ensureEnglish();
    cy.get('[data-testid="open-release-workspace"]', { timeout: 30000 }).click();
    cy.get('[data-testid="submission-release-workspace"]', { timeout: 30000 }).should("be.visible");
    cy.get(`[data-testid="release-submission-${fixture.tasks.release.revision_1_submission_id}"]`).click();
    cy.get('[data-testid="release-approved-submission"]').should("not.exist");
    cy.get(`[data-testid="release-submission-${fixture.tasks.release.revision_2_submission_id}"]`).click();
    let hash = "";
    let snapshot = "";
    cy.get('[data-testid="release-result-hash"]').invoke("text").then((value) => { hash = value; });
    cy.get('[data-testid="release-result-snapshot"]').invoke("text").then((value) => { snapshot = value; });
    let releaseReads = 0;
    cy.intercept("GET", "**/api/submissions/*/release*", () => { releaseReads += 1; }).as("release");
    selectModalLocale("zh-CN");
    cy.get('[data-testid="release-status"]').should("contain.text", "已通过");
    cy.get('[data-testid="release-result-hash"]').should(($hash) => expect($hash.text()).to.eq(hash));
    cy.get('[data-testid="release-result-snapshot"]').should(($snapshot) => expect($snapshot.text()).to.eq(snapshot));
    cy.then(() => expect(releaseReads, "switch must not retrieve a release snapshot").to.eq(0));
    cy.get('[data-testid="release-approved-submission"]').click();
    cy.wait("@release").then(({ request, response }) => {
      expect(request.url).to.include(String(fixture.tasks.release.revision_2_submission_id));
      expect(response?.statusCode).to.eq(200);
    });
    cy.get('[data-testid="release-success"]', { timeout: 30000 }).should("contain.text", "已发布修订版 2")
      .and("contain.text", hash);
  });
});

export {};
