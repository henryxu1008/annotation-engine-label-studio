/// <reference types="cypress" />

type Fixture = {
  password: string;
  project_id: number;
  users: { annotator_a: { email: string } };
  tasks: { a: { id: number } };
};

describe("Data Manager and editor display locale", () => {
  let fixture: Fixture;
  let releasePendingImage: (() => void) | null = null;

  afterEach(() => {
    // A failed readiness assertion must not leave response promises in flight
    // when Cypress restores the next test's default timeout and page.
    releasePendingImage?.();
    releasePendingImage = null;
  });

  const waitForEditableTask = (taskId: number) => cy.window({ timeout: 30000 }).should((win: any) => {
    expect(Number(win.Htx?.task?.id)).to.eq(taskId);
    expect(win.Htx.isLoading).to.eq(false);
    expect(win.dataManager.store.loadingData).to.eq(false);
    expect(win.Htx.annotationStore.selected.history.isFrozen).to.eq(false);
    expect(win.Htx.annotationStore.selected.autosave).to.be.a("function");
    expect(win.Htx.annotationStore.selected.autosave.paused).not.to.eq(true);
  });

  before(() => {
    cy.readFile(".enterprise-e2e.json").then((data) => { fixture = data as Fixture; });
  });

  for (const locale of ["en-US", "zh-CN"]) {
    it(`preserves edits and their pending autosave when an image finishes loading in ${locale}`, { defaultCommandTimeout: 30000 }, () => {
      cy.task("createEnterpriseE2EUndoScope", { delayedImage: true }).then((scope: any) => {
        cy.loginAs(fixture.users.annotator_a.email, fixture.password, "/user/account/personal-info");
        cy.get('[data-testid="language-preference-select"]').select(locale);
        cy.get('[data-testid="language-preference-select"]').should("have.value", locale).and("not.be.disabled");
        let releaseImage: () => void;
        // The controlled response spans editor bootstrap, which can exceed
        // Cypress's default 4s response-handler budget on a cold CI browser.
        const imageGate = new Promise<void>((resolve) => { releaseImage = resolve; releasePendingImage = resolve; });
        let holdImage = false;
        let heldEditorImages = 0;
        cy.readFile("../../libs/editor/public/images/logo.png", null).then((image) => {
          // StaticResponse serializes other objects (including Buffer) as JSON.
          const imageBody = Uint8Array.from(image).buffer;
          cy.intercept("GET", "**/ls76-delayed-image.png", (request) => {
            // Only ImageCache's editor XHR is delayed. Late list thumbnails
            // use resourceType=image and are independent of this gate.
            if (holdImage && request.resourceType === "xhr") {
              heldEditorImages += 1;
              request.alias = "delayedImage";
              request.on("before:response", () => imageGate);
            }
            request.reply({ statusCode: 200, headers: { "content-type": "image/png", "cache-control": "no-store" }, body: imageBody });
          });
        });
        // The list's direct <img> thumbnail blocks window.load if held during
        // cy.visit. Finish that navigation first, then open the editor through
        // the real SPA task-row action so editing can release the image gate.
        cy.visit(`/projects/${scope.project_id}/data`);
        cy.contains('[data-testid="table-row"]', "Independent undo regression.").should("be.visible");
        cy.then(() => { holdImage = true; });
        cy.contains('[data-testid="table-row"]', "Independent undo regression.").click();
        cy.location("search").should("contain", `task=${scope.task_id}`);
        // Ant Design makes the native input transparent; its enclosing label
        // is the visible user control. Keep the input enabled/actionable check.
        cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]', { timeout: 30000 }).should("not.be.disabled").closest("label").should("be.visible");
        waitForEditableTask(scope.task_id);
        cy.wrap(null).should(() => expect(heldEditorImages, "editor image response is pending").to.be.greaterThan(0));
        cy.intercept({ method: /POST|PATCH/, url: /\/api\/(?:tasks\/\d+\/drafts|drafts\/\d+)\/?(?:\?.*)?$/ }).as("mediaDraft");
        // Choice re-renders replace the native input. Assert through a fresh
        // query instead of retrying against check()'s detached old subject.
        cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]').should("not.be.checked").check();
        cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]').should("be.checked");
        cy.then(() => { holdImage = false; releaseImage(); releasePendingImage = null; });
        cy.wait("@delayedImage");
        // ImageCache renders the fetched resource through a blob URL.
        cy.get('#label-studio-dm img[alt="image"]').should(($image) => expect(($image[0] as HTMLImageElement).naturalWidth).to.be.greaterThan(0));
        cy.window().should((win: any) => {
          expect(win.Htx.annotationStore.selected.history.hasChanges).to.eq(false);
          expect(win.Htx.annotationStore.selected.hasUnsavedDraft).to.eq(true);
          expect(win.dispatchEvent(new win.Event("beforeunload", { cancelable: true }))).to.eq(false);
        });
        cy.wait("@mediaDraft", { timeout: 20000 }).then(({ request, response }) => {
          expect(response?.statusCode).to.be.oneOf([200, 201]);
          expect(request.body.result[0].value.choices).to.deep.eq(["Positive"]);
        });
        cy.window().should((win: any) => expect(win.Htx.annotationStore.selected.hasUnsavedDraft).to.eq(false));
        cy.reload();
        cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]', { timeout: 30000 }).should("be.checked");
        cy.get('[data-testid="draft-save-status"]').should("not.exist");
      });
    });

    it(`protects an unsaved tab and restores the confirmed retry after reload in ${locale}`, () => {
      cy.task("createEnterpriseE2EUndoScope").then((scope: any) => {
        const path = `/projects/${scope.project_id}/data?task=${scope.task_id}`;
        cy.loginAs(fixture.users.annotator_a.email, fixture.password, "/user/account/personal-info");
        cy.get('[data-testid="language-preference-select"]').select(locale);
        cy.get('[data-testid="language-preference-select"]').should("have.value", locale).and("not.be.disabled");
        cy.get("html").should("have.attr", "lang", locale);
        cy.visit(path);
        cy.get('[data-testid="bottombar-submit-button"]', { timeout: 30000 }).should("be.visible");
        waitForEditableTask(scope.task_id);
        let offline = true;
        cy.intercept({ method: /POST|PATCH/, url: /\/api\/(?:tasks\/\d+\/drafts|drafts\/\d+)\/?(?:\?.*)?$/ }, (request) => {
          request.alias = offline ? "unloadSaveFailed" : "unloadSaveRetry";
          if (offline) request.reply({ forceNetworkError: true });
        });
        cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]').should("not.be.checked").check();
        cy.wait("@unloadSaveFailed", { timeout: 20000 });
        cy.get('[data-testid="draft-save-status"]').should("contain.text", locale === "zh-CN" ? "草稿保存失败" : "Draft not saved");
        // Cypress accepts native unload dialogs automatically. Test the event
        // cancellation here; real Edge refresh/close cancellation is a separate
        // local acceptance step, followed by a genuine clean reload below.
        cy.window().then((win: any) => {
          const event = new win.Event("beforeunload", { cancelable: true });
          expect(win.dispatchEvent(event)).to.eq(false);
          expect(event.defaultPrevented).to.eq(true);
        });
        cy.then(() => { offline = false; });
        cy.get('[data-testid="draft-save-retry"]').should("contain.text", locale === "zh-CN" ? "重试保存" : "Retry save").click();
        cy.wait("@unloadSaveRetry").then(({ request, response }) => {
          expect(response?.statusCode).to.be.oneOf([200, 201]);
          expect(request.body.result[0].value.choices).to.deep.eq(["Positive"]);
        });
        cy.get('[data-testid="draft-save-status"]').should("not.exist");
        cy.window().then((win: any) => {
          expect(win.dispatchEvent(new win.Event("beforeunload", { cancelable: true }))).to.eq(true);
        });
        cy.reload();
        cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]', { timeout: 30000 }).should("be.checked");
        cy.get('[data-testid="draft-save-status"]').should("not.exist");
      });
    });

    it(`recovers drafts, departure protection and submit retry after failures in ${locale}`, () => {
      cy.task("createEnterpriseE2EUndoScope", { required: true }).then((scope: any) => {
        const path = `/projects/${scope.project_id}/data?task=${scope.task_id}`;
        cy.loginAs(fixture.users.annotator_a.email, fixture.password, "/user/account/personal-info");
        cy.get('[data-testid="language-preference-select"]').select(locale);
        cy.get('[data-testid="language-preference-select"]').should("have.value", locale).and("not.be.disabled");
        cy.get("html").should("have.attr", "lang", locale);
        cy.visit(path);
        cy.get('[data-testid="bottombar-submit-button"]', { timeout: 30000 }).should("be.visible").click();
        cy.contains(locale === "zh-CN" ? '提交前请为“sentiment”选择一个选项。' : 'Select an option for "sentiment" before submitting.').should("be.visible");
        cy.get(".ant-modal-confirm .ant-btn-primary").click();
        cy.window().should((win: any) => expect(win.Htx.annotationStore.selected.submissionStarted).to.eq(0));
        const drafts = { method: /POST|PATCH/, url: /\/api\/(?:tasks\/\d+\/drafts|drafts\/\d+)\/?(?:\?.*)?$/ };
        cy.intercept(drafts).as("initialRecoveryDraft");
        cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]').then(($input) => $input[0].click());
        cy.wait("@initialRecoveryDraft", { timeout: 20000 }).its("response.statusCode").should("be.oneOf", [200, 201]);
        cy.intercept({ method: "POST", url: `**/api/tasks/${scope.task_id}/annotations*`, times: 1 }, { statusCode: 503, body: { detail: "Synthetic submission failure" } }).as("failedSubmit");
        cy.get('[data-testid="bottombar-submit-button"]').click();
        cy.wait("@failedSubmit");
        cy.contains(locale === "zh-CN" ? "标注未能保存" : "Annotation is not saved").should("be.visible");
        cy.window().should((win: any) => {
          expect(win.Htx.isSubmitting).to.eq(false);
          expect(win.Htx.annotationStore.selected.submissionStarted).to.eq(0);
          expect(win.Htx.annotationStore.selected.draftId).to.be.greaterThan(0);
        });
        // Chromium can retry an aborted request. Keep the outage active for
        // every attempt until the user explicitly retries; a one-shot abort
        // can otherwise be followed by a real successful save and navigation.
        let draftOffline = true;
        cy.intercept(drafts, (request) => {
          request.alias = draftOffline ? "failedRecoveryDraft" : "retryRecoveryDraft";
          if (draftOffline) request.reply({ forceNetworkError: true });
        });
        cy.get('#label-studio-dm input[type="checkbox"][name="Negative"]').then(($input) => $input[0].click());
        cy.get('a[href="/projects"]').click();
        cy.wait("@failedRecoveryDraft");
        cy.contains(locale === "zh-CN" ? "草稿未能保存，请重试后再离开此页。" : "There was an error saving your draft").should("be.visible");
        cy.location("pathname").should("eq", `/projects/${scope.project_id}/data`);
        cy.then(() => { draftOffline = false; });
        cy.get('a[href="/projects"]').click();
        cy.wait("@retryRecoveryDraft").then(({ request, response }) => {
          expect(response?.statusCode).to.be.oneOf([200, 201]);
          expect(request.body.result[0].value.choices).to.deep.eq(["Negative"]);
        });
        cy.location("pathname").should("eq", "/projects");
        cy.visit(path);
        cy.get('[data-testid="bottombar-submit-button"]', { timeout: 30000 }).should("be.visible");
        cy.get('#label-studio-dm input[type="checkbox"][name="Negative"]').should("be.checked");
        cy.intercept("POST", `**/api/tasks/${scope.task_id}/annotations*`).as("submitRecoveryRetry");
        cy.get('[data-testid="bottombar-submit-button"]').click();
        cy.wait("@submitRecoveryRetry").then(({ request, response }) => {
          expect(response?.statusCode).to.be.oneOf([200, 201]);
          expect(request.body.result[0].value.choices).to.deep.eq(["Negative"]);
        });
      });
    });

    it(`saves edits made while a departure save is pending in ${locale}`, () => {
      cy.task("createEnterpriseE2EUndoScope").then((scope: any) => {
        const path = `/projects/${scope.project_id}/data?task=${scope.task_id}`;
        cy.loginAs(fixture.users.annotator_a.email, fixture.password, "/user/account/personal-info");
        cy.get('[data-testid="language-preference-select"]').select(locale);
        cy.get('[data-testid="language-preference-select"]').should("have.value", locale).and("not.be.disabled");
        cy.get("html").should("have.attr", "lang", locale);
        cy.visit(path);
        cy.get('[data-testid="bottombar-submit-button"]', { timeout: 30000 }).should("be.visible");
        cy.window().should((win: any) => {
          expect(win.Htx?.isLoading).to.eq(false);
          expect(win.dataManager.store.loadingData).to.eq(false);
          expect(win.Htx.annotationStore.selected.history.isFrozen).to.eq(false);
          expect(win.Htx.annotationStore.selected.autosave).to.be.a("function");
        });
        // Keep the five-second autosave from becoming the first request. This
        // scenario must edit after the explicit departure flush has started.
        cy.clock(Date.now(), ["setTimeout", "clearTimeout"]).as("autosaveClock");
        let release: () => void;
        const heldResponse = new Cypress.Promise<void>((resolve) => { release = resolve; });
        let requests = 0;
        let latest: any[];
        cy.intercept({ method: /POST|PATCH/, url: /\/api\/(?:tasks\/\d+\/drafts|drafts\/\d+)\/?(?:\?.*)?$/ }, (request) => {
          requests += 1;
          request.alias = requests === 1 ? "departureSave" : "latestSave";
          if (requests === 1) request.on("before:response", () => heldResponse);
        });
        cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]').then(($input) => $input[0].click());
        cy.get('a[href="/projects"]').click();
        cy.wrap(null).should(() => expect(requests).to.eq(1));
        cy.location("pathname").should("eq", `/projects/${scope.project_id}/data`);
        cy.get('#label-studio-dm input[type="checkbox"][name="Negative"]').then(($input) => $input[0].click());
        cy.window().then((win: any) => {
          latest = JSON.parse(JSON.stringify(win.Htx.annotationStore.selected.serializeAnnotation()));
          expect(latest[0].value.choices).to.deep.eq(["Negative"]);
        });
        cy.get("@autosaveClock").invoke("restore");
        cy.then(() => release());
        cy.wait("@departureSave").its("response.statusCode").should("be.oneOf", [200, 201]);
        cy.wait("@latestSave").then(({ request, response }) => {
          expect(response?.statusCode).to.be.oneOf([200, 201]);
          expect(request.body.result).to.deep.eq(latest);
        });
        cy.location("pathname").should("eq", "/projects");
        cy.visit(path);
        cy.get('[data-testid="bottombar-submit-button"]', { timeout: 30000 }).should("be.visible");
        cy.window().should((win: any) => {
          expect(win.Htx.annotationStore.selected.serializeAnnotation()).to.deep.eq(latest);
        });
      });
    });

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
