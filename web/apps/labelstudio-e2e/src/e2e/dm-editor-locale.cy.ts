/// <reference types="cypress" />

type Fixture = {
  password: string;
  project_id: number;
  users: { annotator_a: { email: string } };
  tasks: { a: { id: number } };
};

describe("Data Manager and editor display locale", () => {
  let fixture: Fixture;

  before(() => {
    cy.readFile(".enterprise-e2e.json").then((data) => { fixture = data as Fixture; });
  });

  for (const locale of ["en-US", "zh-CN"]) {
    it(`persists undo to the initial result before leaving in ${locale}`, () => {
      // Backend data is not reset by Cypress browser isolation. Each regression
      // owns its project/task, preserving the existing cold-bootstrap fixture.
      cy.task("createEnterpriseE2EUndoScope").then((scope: any) => {
        const path = `/projects/${scope.project_id}/data?task=${scope.task_id}`;
        cy.loginAs(fixture.users.annotator_a.email, fixture.password, "/user/account/personal-info");
        cy.get('[data-testid="language-preference-select"]').select(locale);
        // The provider applies locale after the server acknowledges the save.
        // A full cy.visit before that response can unload the pending change.
        cy.get('[data-testid="language-preference-select"]').should("have.value", locale).and("not.be.disabled");
        cy.get("html").should("have.attr", "lang", locale);
        cy.visit(path);
        cy.get('[data-testid="bottombar-submit-button"]', { timeout: 30000 }).should("be.visible");
        cy.window().should((win: any) => {
          expect(win.Htx?.isLoading).to.eq(false);
          expect(win.dataManager.store.loadingData).to.eq(false);
          expect(win.Htx.annotationStore.selected.history.isFrozen).to.eq(false);
          expect(win.Htx.annotationStore.selected.autosave).to.be.a("function");
          expect(win.Htx.annotationStore.selected.autosave.paused).not.to.eq(true);
        });
        let initial: any[];
        cy.window().then((win: any) => {
          initial = JSON.parse(JSON.stringify(win.Htx.annotationStore.selected.serializeAnnotation()));
          expect(win.Htx.annotationStore.selected.history.undoIdx).to.eq(0);
        });
        const drafts = { method: /POST|PATCH/, url: /\/api\/(?:tasks\/\d+\/drafts|drafts\/\d+)\/?(?:\?.*)?$/ };
        cy.intercept(drafts).as("savedEdit");
        cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]').then(($input) => $input[0].click());
        cy.wait("@savedEdit", { timeout: 20000 }).then(({ request, response }) => {
          expect(response?.statusCode).to.be.oneOf([200, 201]);
          expect(request.body.result).not.to.deep.eq(initial);
        });
        cy.intercept(drafts).as("savedUndo");
        cy.get(`button[aria-label="${locale === "zh-CN" ? "撤销" : "Undo"}"]`).click();
        cy.window().should((win: any) => {
          expect(win.Htx.annotationStore.selected.history.undoIdx).to.eq(0);
          expect(win.Htx.annotationStore.selected.serializeAnnotation()).to.deep.eq(initial);
        });
        cy.get('a[href="/projects"]').click();
        cy.wait("@savedUndo").then(({ request, response }) => {
          expect(request.body.result).to.deep.eq(initial);
          expect(response?.statusCode).to.be.oneOf([200, 201]);
        });
        cy.location("pathname").should("eq", "/projects");
        cy.visit(path);
        cy.get('[data-testid="bottombar-submit-button"]', { timeout: 30000 }).should("be.visible");
        cy.window().should((win: any) => {
          expect(win.Htx.annotationStore.selected.serializeAnnotation()).to.deep.eq(initial);
        });
      });
    });
  }

  it("reports an interrupted bootstrap in the active language and allows returning to projects", () => {
    cy.loginAs(fixture.users.annotator_a.email, fixture.password, "/user/account/personal-info");
    cy.get('[data-testid="language-preference-select"]').select("zh-CN");
    cy.get("html").should("have.attr", "lang", "zh-CN");
    // Django returns an HTML 400 if an in-flight session save loses a race
    // with logout. No exception handler or fake successful columns are used.
    cy.intercept("GET", "**/api/dm/columns*", {
      statusCode: 400,
      headers: { "content-type": "text/html" },
      body: "<html>synthetic interrupted session</html>",
    }).as("interruptedColumns");
    cy.visit(`/projects/${fixture.project_id}/data`);
    cy.wait("@interruptedColumns");
    cy.get("body").should("contain.text", "无法加载任务工作区，请重试或返回项目列表。")
      .and("not.contain.text", "API_GATEWAY")
      .and("not.contain.text", "synthetic interrupted session");
    cy.window().should((win: any) => {
      expect(win.dataManager?.store, "no store published after failed bootstrap").to.be.null;
    });
    cy.get('a[aria-label="返回项目列表"]').should("be.visible").click();
    cy.location("pathname").should("eq", "/projects");
  });

  it("updates three mounted roots without replacing the task or issuing an annotation write", () => {
    const path = `/projects/${fixture.project_id}/data?task=${fixture.tasks.a.id}`;
    cy.loginAs(fixture.users.annotator_a.email, fixture.password, "/user/account/personal-info");
    cy.get('[data-testid="language-preference-select"]').select("en-US");
    cy.get("html").should("have.attr", "lang", "en-US");
    cy.intercept("GET", "**/api/projects/*/label-stream-history*").as("labelStreamHistory");
    cy.intercept("GET", "**/api/label_links*").as("labelLinks");
    let taskRequests = 0;
    let releaseTaskRefresh: () => void;
    const taskRefresh = new Promise<void>((resolve) => { releaseTaskRefresh = resolve; });
    cy.intercept("GET", `**/api/tasks/${fixture.tasks.a.id}?*`, (request) => {
      taskRequests += 1;
      if (taskRequests === 2) {
        request.alias = "taskRefresh";
        request.on("before:response", () => taskRefresh);
      }
    });
    cy.visit(path);
    cy.wait("@labelStreamHistory");
    cy.wait("@labelLinks");
    // The editor bootstrap may finish before the refreshed task arrives. It
    // must remain closed to edits until that response and its reset settle.
    cy.window().should((win: any) => {
      expect(taskRequests, "initial task and refresh requested").to.eq(2);
      expect(win.dataManager.store.loadingData, "task refresh pending").to.eq(true);
      expect(win.Htx?.isLoading, "edits blocked during task refresh").to.eq(true);
    });
    cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]').should("not.exist");
    cy.then(() => releaseTaskRefresh());
    cy.wait("@taskRefresh");
    cy.get('[data-testid="bottombar-submit-button"]', { timeout: 30000 }).should("be.visible");
    cy.window().should((win: any) => {
      expect(win.dataManager?.localeRuntime?.locale).to.eq("en-US");
      expect(win.dataManager?.lsf?.lsfInstance?.localeRuntime?.locale).to.eq("en-US");
      expect(Number(win.Htx?.task?.id)).to.eq(fixture.tasks.a.id);
      expect(Number(win.dataManager?.lsf?.task?.id), "Data Manager task selection started").to.eq(fixture.tasks.a.id);
      const annotation = win.Htx?.annotationStore?.selected;
      expect(win.Htx?.isLoading, "editor initialization complete").to.eq(false);
      expect(win.dataManager.store.loadingData, "task refresh and reset complete").to.eq(false);
      expect(annotation?.editable, "editable annotation").to.eq(true);
      expect(annotation?.isReadOnly(), "annotation is not read-only").to.eq(false);
      expect(annotation?.history?.isFrozen, "annotation history ready").to.eq(false);
      expect(annotation?.autosave, "autosave listener attached").to.be.a("function");
      expect(annotation?.autosave?.paused, "autosave active").not.to.eq(true);
    });
    let chosen: "Positive" | "Negative" = "Positive";
    cy.intercept({
      method: /POST|PATCH/,
      url: /\/api\/(?:tasks\/\d+\/drafts|drafts\/\d+)\/?(?:\?.*)?$/,
    }).as("initialDraft");
    cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]').should("not.be.disabled");
    cy.window().then((win: any) => {
      const positive = win.document.querySelector('#label-studio-dm input[type="checkbox"][name="Positive"]') as HTMLInputElement | null;
      chosen = positive?.checked ? "Negative" : "Positive";
      const input = win.document.querySelector(`#label-studio-dm input[type="checkbox"][name="${chosen}"]`) as HTMLInputElement | null;
      expect(input?.isConnected, "mounted choice input").to.eq(true);
      input!.click();
    });
    cy.then(() => cy.get(`#label-studio-dm input[type="checkbox"][name="${chosen}"]`).should("be.checked"));
    cy.window().should((win: any) => {
      const results = win.Htx.annotationStore.selected.serializeAnnotation();
      expect(results.some((result: any) => result.value?.choices?.includes(chosen))).to.eq(true);
    });
    cy.window().then(async (win: any) => {
      const annotation = win.Htx.annotationStore.selected;
      annotation.autosave?.cancel?.();
      await annotation.saveDraftImmediatelyWithResults();
    });
    cy.wait("@initialDraft").its("response.statusCode").should("be.oneOf", [200, 201]);
    cy.window().should((win: any) => {
      expect(win.Htx.annotationStore.selected.isDraftSaving).to.eq(false);
    });

    let dm: any;
    let editor: any;
    let editorStore: any;
    let annotationResult: string;
    let selectedId: string | number;
    let canUndo: boolean;
    let assignmentId: number | null;
    let assignmentVersion: number | null;
    let annotationWrites = 0;
    cy.window().then((win: any) => {
      dm = win.dataManager;
      editor = dm.lsf.lsfInstance;
      editorStore = editor.store;
      expect(win.Htx).to.eq(editorStore);
      const selectedAnnotation = editorStore.annotationStore.selected;
      selectedId = selectedAnnotation.id;
      annotationResult = JSON.stringify(selectedAnnotation.serializeAnnotation());
      canUndo = selectedAnnotation.history.canUndo;
      expect(annotationResult).to.contain(chosen);
      assignmentId = dm.store.taskStore.selected.assignment_id;
      assignmentVersion = dm.store.taskStore.selected.assignment_version;
      expect(assignmentId).to.be.a("number");
      expect(assignmentVersion).to.be.a("number");
      expect(dm.taskSelectionPromise, "initial task selection finished").to.be.null;
    });
    cy.intercept({ method: /POST|PATCH|PUT|DELETE/, url: "**/api/**" }, (request) => {
      if (/\/(annotations|drafts|submissions|tasks)\b/.test(request.url)) annotationWrites += 1;
    });
    cy.get('[data-testid="user-menu-trigger"]').click();
    cy.window().should((win: any) => {
      expect(win.dataManager, "DM after opening menu").to.eq(dm);
      expect(win.dataManager.lsf.lsfInstance, "editor after opening menu").to.eq(editor);
      expect(win.Htx, "editor store after opening menu").to.eq(editorStore);
      expect(dm.taskSelectionPromise, "no later task selection").to.be.null;
      expect(win.Htx.annotationStore.selected.id, "annotation id after opening menu").to.eq(selectedId);
      expect(JSON.stringify(win.Htx.annotationStore.selected.serializeAnnotation()), "result after opening menu").to.eq(annotationResult);
    });
    cy.get('[data-testid="menu-language-select"]').select("zh-CN");
    cy.get("html").should("have.attr", "lang", "zh-CN");
    cy.get('[data-testid="bottombar-submit-button"]').should("contain.text", "提交");
    cy.get('[data-testid="columns-picker-quickview"]').should("contain.text", "列");
    cy.get("body").should("contain.text", "标注区域将在这里显示");
    cy.get("#Regions-draggable").should("contain.text", "区域");
    cy.window().should((win: any) => {
      expect(win.dataManager).to.eq(dm);
      expect(win.dataManager.localeRuntime.locale).to.eq("zh-CN");
      expect(win.dataManager.lsf.lsfInstance).to.eq(editor);
      expect(editor.localeRuntime.locale).to.eq("zh-CN");
      expect(editor.store).to.eq(editorStore);
      expect(JSON.stringify(win.Htx.annotationStore.selected.serializeAnnotation())).to.eq(annotationResult);
      expect(win.Htx.annotationStore.selected.history.canUndo).to.eq(canUndo);
      expect(dm.store.taskStore.selected.assignment_id).to.eq(assignmentId);
      expect(dm.store.taskStore.selected.assignment_version).to.eq(assignmentVersion);
      expect(Number(win.Htx.task.id)).to.eq(fixture.tasks.a.id);
    });
    cy.then(() => expect(annotationWrites, "language switch must not write annotations or tasks").to.eq(0));
  });
});
