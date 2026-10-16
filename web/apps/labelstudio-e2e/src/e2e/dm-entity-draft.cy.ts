/// <reference types="cypress" />

describe("Restored text entity drafts", () => {
  let fixture: any;
  let releaseDraftResponse: (() => void) | undefined;
  afterEach(() => { releaseDraftResponse?.(); releaseDraftResponse = undefined; });
  const draftRoute = { method: /POST|PATCH/, url: /\/api\/(?:tasks\/\d+\/drafts|drafts\/\d+)/ };
  const state = (dirty: boolean, note = "原始备注") => cy.window().should((win: any) => {
    const annotation = win.Htx?.annotationStore?.selected;
    expect(annotation, "loaded annotation").to.exist;
    expect(annotation.hasUnsavedDraft, "actual unsaved state").to.eq(dirty);
    const results = annotation.serializeAnnotation();
    expect(results.find((r: any) => r.type === "labels").value).to.include({ start: 0, end: 11 });
    expect(results.find((r: any) => r.from_name === "entity_note").value.text).to.deep.eq([note]);
  });
  const edit = (text: string) => {
    // The note must remain displayed. Its action icons use opacity: 0 until
    // hover, so check the content and keep Cypress's normal click checks.
    cy.get('[data-testid="htx-textbox-view"]').should("be.visible");
    cy.get('[data-testid="htx-textbox-edit-button"]').click();
    cy.get('[data-testid="htx-textbox-input"]').should("be.visible").clear().type(`${text}{enter}`);
  };
  before(() => cy.readFile(".enterprise-e2e.json").then((data) => { fixture = data; }));

  for (const locale of ["en-US", "zh-CN"]) {
    it(`submits the latest local edit after an in-flight draft save in ${locale}`, () => {
      cy.task("createEnterpriseE2EUndoScope", { entityDraft: true }).then((scope: any) => {
        cy.loginAs(fixture.users.annotator_a.email, fixture.password, "/user/account/personal-info");
        cy.get('[data-testid="language-preference-select"]').select(locale).should("have.value", locale).and("not.be.disabled");
        let responseHeld = false;
        cy.intercept({ ...draftRoute, times: 1 }, req => req.continue(() => new Promise<void>(resolve => {
          responseHeld = true;
          releaseDraftResponse = resolve;
        }))).as("pendingOwnDraft");
        cy.intercept("POST", `**/api/tasks/${scope.task_id}/annotations*`).as("submitLatest");
        cy.visit(`/projects/${scope.project_id}/data?task=${scope.task_id}`);
        cy.get('[role="treeitem"]').contains("Entity").should("be.visible").click();
        edit("First pending save");
        cy.get('[data-testid="draft-save-retry"]').click();
        cy.wrap(null).should(() => expect(responseHeld, "server committed; response held").to.eq(true));
        edit("Newest edit while saving 中文");
        cy.get('[data-testid="bottombar-submit-button"]').click();
        cy.then(() => { releaseDraftResponse!(); releaseDraftResponse = undefined; });
        cy.wait("@pendingOwnDraft").its("response.statusCode").should("eq", 200);
        cy.wait("@submitLatest").then(({ request, response }) => {
          expect(response!.statusCode).to.eq(201);
          expect(request.body.draft_revision).to.eq(2);
          expect(request.body.result.find((r: any) => r.from_name === "entity_note").value.text)
            .to.deep.eq(["Newest edit while saving 中文"]);
          cy.request(`/api/annotations/${response!.body.id}/`).its("body.result").then(result => {
            expect(result.find((r: any) => r.from_name === "entity_note").value.text)
              .to.deep.eq(["Newest edit while saving 中文"]);
          });
        });
        cy.get('[data-testid="draft-conflict"]').should("not.exist");
      });
    });

    it(`preserves a newer remote draft and recovers conflicted offline edits in ${locale}`, () => {
      cy.task("createEnterpriseE2EUndoScope", { entityDraft: true }).then((scope: any) => {
        cy.loginAs(fixture.users.annotator_a.email, fixture.password, "/user/account/personal-info");
        cy.get('[data-testid="language-preference-select"]').select(locale).should("have.value", locale).and("not.be.disabled");
        let offline = true;
        cy.intercept(draftRoute, (req) => {
          req.alias = offline ? "offlineDraftWrite" : "onlineDraftWrite";
          if (offline) req.destroy();
          else req.continue();
        });
        cy.visit(`/projects/${scope.project_id}/data?task=${scope.task_id}`);
        cy.get('[role="treeitem"]').contains("Entity").should("be.visible").click();
        state(false);
        edit("My offline edit");
        cy.get('[data-testid="draft-save-retry"]').click();
        cy.wait("@offlineDraftWrite").should("have.property", "error");
        // An independent writer saves through the real endpoint, using its own
        // current server snapshot. Real two-tab transport is covered in Edge.
        cy.request(`/api/tasks/${scope.task_id}/`).then(({ body: task }) => {
          const draft = task.drafts[0];
          cy.getCookie("csrftoken").then((cookie) => cy.request({
            method: "PATCH", url: `/api/drafts/${draft.id}`,
            headers: { "X-CSRFToken": cookie!.value },
            body: {
              assignment_id: task.assignment_id, assignment_version: task.assignment_version,
              draft_revision: draft.revision,
              result: draft.result.map((r: any) => r.from_name === "entity_note"
                ? { ...r, value: { ...r.value, text: ["Newer remote edit"] } } : r),
            },
          })).its("status").should("eq", 200);
        });
        cy.then(() => { offline = false; });
        edit("My latest local edit 中文");
        // Let the actual scheduled autosave discover the stale content token.
        cy.wait("@onlineDraftWrite", { timeout: 15000 }).its("response.statusCode").should("eq", 409);
        cy.get('[data-testid="draft-conflict"]').should("be.visible")
          .and("contain", locale === "zh-CN" ? "草稿已在另一页面更新" : "Draft changed in another page");
        cy.get('[data-testid="draft-conflict"] summary').should("be.visible");
        state(true, "My latest local edit 中文");
        cy.get('[data-testid="draft-save-retry"]').should("not.exist");
        cy.get('[data-testid="draft-reload-latest"]').should("be.disabled");
        cy.request(`/api/tasks/${scope.task_id}/`).its("body.drafts.0.result").then((result) => {
          expect(result.find((r: any) => r.from_name === "entity_note").value.text).to.deep.eq(["Newer remote edit"]);
        });
        cy.get('[data-testid="draft-backup"]').click();
        cy.readFile(`${Cypress.config("downloadsFolder")}/task-${scope.task_id}-unsaved-draft.json`).then((backup) => {
          expect(String(backup.task_id)).to.eq(String(scope.task_id));
          expect(backup.result.find((r: any) => r.from_name === "entity_note").value.text).to.deep.eq(["My latest local edit 中文"]);
        });
        edit("Not yet backed up");
        cy.get('[data-testid="draft-reload-latest"]').should("be.disabled");
        edit("My latest local edit 中文");
        cy.get('[data-testid="draft-reload-latest"]').should("be.enabled");
        cy.intercept({ method: "GET", url: new RegExp(`/api/tasks/${scope.task_id}/?(?:\\?.*)?$`), times: 1 },
          { statusCode: 503, body: { detail: "Synthetic recovery load failure" } }).as("failedConflictReload");
        cy.get('[data-testid="draft-reload-latest"]').click();
        cy.wait("@failedConflictReload").its("response.statusCode").should("eq", 503);
        cy.get('[data-testid="modal-close-button"]').should("be.visible").click();
        state(true, "My latest local edit 中文");
        cy.contains(locale === "zh-CN" ? "未能载入最新草稿。本页修改仍然保留，请检查网络后重试。"
          : "Could not load the latest draft. Your edits remain here; check the connection and try again.").should("be.visible");
        cy.get('[data-testid="draft-reload-latest"]').click();
        cy.get('[role="treeitem"]').contains("Entity").should("be.visible");
        state(false, "Newer remote edit");
        cy.get('[data-testid="draft-conflict"]').should("not.exist");
        cy.get('[role="treeitem"]').contains("Entity").click();
        edit("Reapplied local edit 中文");
        cy.get('[data-testid="draft-save-retry"]').click();
        cy.wait("@onlineDraftWrite").its("response.statusCode").should("eq", 200);
        state(false, "Reapplied local edit 中文");
        cy.reload();
        cy.get('[role="treeitem"]').contains("Entity").should("be.visible");
        state(false, "Reapplied local edit 中文");
      });
    });

    for (const action of ["submitted", "deleted"]) {
      it(`preserves offline edits after a draft is ${action} in another page in ${locale}`, { defaultCommandTimeout: 30000 }, () => {
        cy.task("createEnterpriseE2EUndoScope", { entityDraft: true }).then((scope: any) => {
          cy.loginAs(fixture.users.annotator_a.email, fixture.password, "/user/account/personal-info");
          cy.get('[data-testid="language-preference-select"]').select(locale).should("have.value", locale).and("not.be.disabled");
          let offline = true;
          let writes = 0;
          let submitted: any;
          let removedId: number;
          cy.intercept(draftRoute, req => {
            writes++;
            req.alias = offline ? "missingOfflineWrite" : "missingOnlineWrite";
            if (offline) req.destroy(); else req.continue();
          });
          cy.visit(`/projects/${scope.project_id}/data?task=${scope.task_id}`);
          cy.get('[role="treeitem"]').contains("Entity").should("be.visible").click();
          edit("Offline edit before removal 中文");
          cy.get('[data-testid="draft-save-retry"]').click();
          cy.wait("@missingOfflineWrite").should("have.property", "error");
          // Real server mutation from an independent page snapshot. This does
          // not modify the editor's stale draft ID, content, or revision.
          cy.request(`/api/tasks/${scope.task_id}/`).then(({ body: task }) => {
            const draft = task.drafts[0];
            removedId = draft.id;
            const body = { assignment_id: task.assignment_id, assignment_version: task.assignment_version,
              draft_id: draft.id, draft_revision: draft.revision, result: draft.result };
            cy.getCookie("csrftoken").then(cookie => cy.request({
              method: action === "submitted" ? "POST" : "DELETE",
              url: action === "submitted" ? `/api/tasks/${scope.task_id}/annotations/` : `/api/drafts/${draft.id}`,
              headers: { "X-CSRFToken": cookie!.value }, body,
            })).then(response => {
              expect(response.status).to.eq(action === "submitted" ? 201 : 204);
              if (action === "submitted") submitted = response.body;
            });
          });
          cy.then(() => { offline = false; });
          edit("Latest offline edit after removal 中文");
          cy.wait("@missingOnlineWrite", { timeout: 15000 }).its("response.statusCode").should("eq", 404);
          cy.get('[data-testid="draft-conflict"]').should("be.visible")
            .and("contain", locale === "zh-CN" ? "草稿已不存在" : "Draft is no longer available");
          cy.get(".lsf-modal-ls").should("not.exist");
          state(true, "Latest offline edit after removal 中文");
          cy.get('[data-testid="bottombar-submit-button"]').should("be.disabled");
          cy.get('[data-testid="draft-save-retry"]').should("not.exist");
          cy.get('[data-testid="draft-reload-latest"]').should("be.disabled");
          cy.window().should((win: any) => {
            expect(win.Htx.annotationStore.selected.draftId).to.eq(removedId);
            expect(win.dispatchEvent(new win.Event("beforeunload", { cancelable: true }))).to.eq(false);
          });
          cy.then(() => {
            const stoppedAt = writes;
            edit("Still local after missing draft 中文");
            state(true, "Still local after missing draft 中文");
            cy.wait(5500).then(() => expect(writes, "autosave stopped after 404").to.eq(stoppedAt));
          });
          cy.get('[data-testid="draft-backup"]').click();
          cy.readFile(`${Cypress.config("downloadsFolder")}/task-${scope.task_id}-unsaved-draft.json`).then(backup => {
            expect(backup.draft_id).to.eq(removedId);
            expect(backup.result.find((r: any) => r.from_name === "entity_note").value.text).to.deep.eq(["Still local after missing draft 中文"]);
          });
          cy.intercept({ method: "GET", url: new RegExp(`/api/tasks/${scope.task_id}/?(?:\\?.*)?$`), times: 1 },
            { statusCode: 503, body: { detail: "Synthetic task recovery load failure" } }).as("missingRecoveryFailed");
          cy.get('[data-testid="draft-reload-latest"]').should("contain", locale === "zh-CN" ? "载入任务最新状态" : "Load latest task").click();
          cy.wait("@missingRecoveryFailed").its("response.statusCode").should("eq", 503);
          cy.get('[data-testid="modal-close-button"]').should("be.visible").click();
          state(true, "Still local after missing draft 中文");
          cy.contains(locale === "zh-CN" ? "未能载入任务最新状态。本页修改仍然保留" : "Could not load the latest task. Your edits remain here").should("be.visible");
          cy.get('[data-testid="draft-reload-latest"]').click();
          cy.get('[data-testid="draft-conflict"]').should("not.exist");
          cy.window().should((win: any) => {
            expect(win.Htx.annotationStore.selected.hasUnsavedDraft).to.eq(false);
            expect(win.Htx.annotationStore.selected.draftId || 0).to.eq(0);
          });
          cy.wait(5500); // Loading the latest state must not recreate a draft.
          cy.request(`/api/tasks/${scope.task_id}/`).then(({ body: task }) => {
            expect(task.drafts).to.deep.eq([]);
            expect(task.annotations).to.have.length(action === "submitted" ? 1 : 0);
            if (submitted) {
              expect(task.annotations[0].id).to.eq(submitted.id);
              expect(task.annotations[0].result).to.deep.eq(submitted.result);
            }
          });
          if (action === "submitted") state(false);
          else cy.get('[role="treeitem"]').should("not.exist");
        });
      });
    }

    it(`restores a clean entity and protects real edits through failed save and retry in ${locale}`, () => {
      cy.task("createEnterpriseE2EUndoScope", { entityDraft: true }).then((scope: any) => {
        cy.loginAs(fixture.users.annotator_a.email, fixture.password, "/user/account/personal-info");
        cy.get('[data-testid="language-preference-select"]').select(locale).should("have.value", locale).and("not.be.disabled");
        let fail = true;
        let writes = 0;
        cy.intercept(draftRoute, (req) => {
          writes++;
          if (fail) req.reply({ statusCode: 503, body: { detail: "Synthetic save failure" } });
          else req.continue();
        }).as("draftWrite");
        cy.visit(`/projects/${scope.project_id}/data?task=${scope.task_id}`);
        cy.get("html").should("have.attr", "lang", locale);
        cy.get('[role="treeitem"]').contains("Entity").should("be.visible");
        state(false);
        // Wait past the actual autosave interval: initial rendering must not write.
        cy.wait(5500).then(() => expect(writes, "no autosave from initialization").to.eq(0));
        cy.reload();
        cy.get('[role="treeitem"]').contains("Entity").should("be.visible");
        state(false);
        // Select once: clicking an already-selected outliner region toggles it
        // off and hides its per-region note. Keep it selected across the retry.
        cy.get('[role="treeitem"]').contains("Entity").click();
        edit("First edit");
        state(true, "First edit");
        cy.get('[data-testid="draft-save-retry"]').click();
        cy.wait("@draftWrite").its("response.statusCode").should("eq", 503);
        cy.contains(locale === "zh-CN" ? "草稿保存失败" : "Draft not saved").should("be.visible");
        // A server response also opens the existing API error dialog. Follow
        // its normal close action before editing the still-unsaved draft.
        cy.get(".lsf-modal-ls__body").should("be.visible");
        cy.get('[data-testid="modal-close-button"]').click();
        cy.get(".lsf-modal-ls").should("not.exist");
        state(true, "First edit");
        edit("Latest edit 中文");
        state(true, "Latest edit 中文");
        cy.then(() => { fail = false; });
        cy.get('[data-testid="draft-save-retry"]').click();
        cy.wait("@draftWrite").its("response.statusCode").should("be.oneOf", [200, 201]);
        state(false, "Latest edit 中文");
        cy.reload();
        cy.get('[role="treeitem"]').contains("Entity").should("be.visible");
        state(false, "Latest edit 中文");
        cy.get('[data-testid="draft-save-retry"]').should("not.exist");
      });
    });
  }
});
