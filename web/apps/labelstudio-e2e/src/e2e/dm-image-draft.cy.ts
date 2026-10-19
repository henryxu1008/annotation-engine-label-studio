/// <reference types="cypress" />

describe("Image draft recovery after another page submits or deletes", () => {
  let fixture: { password: string; users: { annotator_a: { email: string } } };
  const draftRoute = { method: /POST|PATCH/, url: /\/api\/(?:tasks\/\d+\/drafts|drafts\/\d+)\/?(?:\?.*)?$/ };
  const localBox = { x: 19, y: 23, width: 42, height: 33, rotation: 0, rectanglelabels: ["Other"] };
  const remoteBox = { x: 55, y: 40, width: 20, height: 18, rotation: 0, rectanglelabels: ["Object"] };
  const regionTab = (locale: string) => cy.contains('#label-studio-dm .lsf-panel-tabs__tab', locale === "zh-CN" ? "区域" : "Regions")
    .scrollIntoView().should("be.visible").click();
  const selectBox = (locale: string) => {
    // Narrow workspaces move these tabs below the image. Follow the normal
    // scroll -> Regions -> select row -> Info workflow, without forced clicks.
    regionTab(locale);
    cy.contains('[role="treeitem"]', "Object").scrollIntoView().should("be.visible").click();
    cy.contains('#label-studio-dm .lsf-panel-tabs__tab', locale === "zh-CN" ? "信息" : "Info")
      .scrollIntoView().should("be.visible").click();
  };
  const input = (name: string) => cy.contains("label.lsf-region-editor__property", new RegExp(`^${name}$`)).find("input");
  const editBox = (values: Record<string, number>) => {
    for (const [name, value] of Object.entries(values)) {
      input(name).clear().type(String(value)).blur();
      input(name).should("have.value", String(value));
    }
  };
  const assertBox = (expected: typeof localBox) => {
    for (const [name, key] of [["X", "x"], ["Y", "y"], ["W", "width"], ["H", "height"]] as const) {
      input(name).should("have.value", String(expected[key]));
    }
    cy.window().should((win: any) => {
      const results = win.Htx.annotationStore.selected.serializeAnnotation();
      expect(results).to.have.length(1);
      expect(results[0]).to.include({ original_width: 400, original_height: 300 });
      expect(results[0].value).to.deep.eq(expected);
    });
  };
  before(() => cy.readFile(".enterprise-e2e.json").then(data => { fixture = data; }));

  for (const locale of ["en-US", "zh-CN"]) {
    for (const action of ["submitted", "deleted"]) {
      it(`preserves rectangle geometry and labels after ${action} in ${locale}`, { defaultCommandTimeout: 30000 }, () => {
        cy.task("createEnterpriseE2EUndoScope", { imageDraft: true }).then((scope: any) => {
          cy.loginAs(fixture.users.annotator_a.email, fixture.password, "/user/account/personal-info");
          cy.get('[data-testid="language-preference-select"]').select(locale).should("have.value", locale).and("not.be.disabled");
          let offline = true;
          let writes = 0;
          let removedId: number;
          let submitted: any;
          cy.intercept(draftRoute, req => {
            writes++;
            req.alias = offline ? "imageOfflineWrite" : "imageOnlineWrite";
            if (offline) req.destroy(); else req.continue();
          });
          cy.visit(`/projects/${scope.project_id}/data?task=${scope.task_id}`);
          cy.get("html").should("have.attr", "lang", locale);
          cy.get('#label-studio-dm img[alt="image"]').should($image => {
            expect(($image[0] as HTMLImageElement).naturalWidth).to.eq(400);
          });
          selectBox(locale);
          editBox({ X: 18, Y: 22, W: 41, H: 32 });
          cy.get("#label-studio-dm").contains(/^Other$/).click();
          cy.get('[data-testid="draft-save-retry"]').click();
          cy.wait("@imageOfflineWrite").should("have.property", "error");

          // Independent server snapshot, with a distinguishable rectangle.
          // Native Edge acceptance separately uses two real tabs and per-tab offline transport.
          cy.request(`/api/tasks/${scope.task_id}/`).then(({ body: task }) => {
            const draft = task.drafts[0];
            removedId = draft.id;
            const result = draft.result.map((r: any) => ({ ...r, value: remoteBox }));
            cy.getCookie("csrftoken").then(cookie => cy.request({
              method: "PATCH", url: `/api/drafts/${draft.id}`,
              headers: { "X-CSRFToken": cookie!.value },
              body: { assignment_id: task.assignment_id, assignment_version: task.assignment_version,
                draft_revision: draft.revision, result },
            })).then(({ body: saved, status }) => {
              expect(status).to.eq(200);
              expect(saved.result[0].value).to.deep.eq(remoteBox);
              cy.getCookie("csrftoken").then(cookie => cy.request({
                method: action === "submitted" ? "POST" : "DELETE",
                url: action === "submitted" ? `/api/tasks/${scope.task_id}/annotations/` : `/api/drafts/${draft.id}`,
                headers: { "X-CSRFToken": cookie!.value },
                body: { assignment_id: task.assignment_id, assignment_version: task.assignment_version,
                  draft_id: draft.id, draft_revision: saved.revision, result },
              })).then(response => {
                expect(response.status).to.eq(action === "submitted" ? 201 : 204);
                if (action === "submitted") submitted = response.body;
              });
            });
          });
          editBox({ X: 19, Y: 23, W: 42, H: 33 });
          assertBox(localBox);
          cy.then(() => { offline = false; });
          cy.wait("@imageOnlineWrite", { timeout: 15000 }).its("response.statusCode").should("eq", 404);
          cy.get('[data-testid="draft-conflict"]').should("be.visible")
            .and("contain", locale === "zh-CN" ? "草稿已不存在" : "Draft is no longer available");
          cy.get('[data-testid="bottombar-submit-button"]').should("be.disabled");
          cy.get('[data-testid="draft-save-retry"]').should("not.exist");
          assertBox(localBox);
          cy.get('[data-testid="draft-reload-latest"]').should("be.disabled");
          cy.get('[data-testid="draft-backup"]').click();
          cy.readFile(`${Cypress.config("downloadsFolder")}/task-${scope.task_id}-unsaved-draft.json`).then(backup => {
            expect(Number(backup.task_id)).to.eq(scope.task_id);
            expect(backup.draft_id).to.eq(removedId);
            expect(backup.result).to.have.length(1);
            expect(backup.result[0]).to.include({ original_width: 400, original_height: 300 });
            expect(backup.result[0].value).to.deep.eq(localBox);
          });
          cy.intercept({ method: "GET", url: new RegExp(`/api/tasks/${scope.task_id}/?(?:\\?.*)?$`), times: 1 },
            { statusCode: 503, body: { detail: "Synthetic image recovery load failure" } }).as("imageRecoveryFailed");
          cy.get('[data-testid="draft-reload-latest"]').should("contain", locale === "zh-CN" ? "载入任务最新状态" : "Load latest task").click();
          cy.wait("@imageRecoveryFailed").its("response.statusCode").should("eq", 503);
          cy.get('[data-testid="modal-close-button"]').should("be.visible").click();
          cy.contains(locale === "zh-CN" ? "未能载入任务最新状态。本页修改仍然保留" : "Could not load the latest task. Your edits remain here").should("be.visible");
          assertBox(localBox);
          cy.then(() => {
            const stopped = writes;
            cy.wait(5500).then(() => expect(writes, "no automatic writes after missing draft").to.eq(stopped));
          });
          cy.get('[data-testid="draft-reload-latest"]').click();
          cy.get('[data-testid="draft-conflict"]').should("not.exist");
          cy.window().should((win: any) => {
            expect(win.Htx.annotationStore.selected.hasUnsavedDraft).to.eq(false);
            expect(win.Htx.annotationStore.selected.draftId || 0).to.eq(0);
          });
          if (action === "submitted") {
            selectBox(locale);
            assertBox(remoteBox);
          } else {
            regionTab(locale);
            cy.get('[role="treeitem"]').should("not.exist");
            cy.window().should((win: any) => expect(win.Htx.annotationStore.selected.serializeAnnotation()).to.deep.eq([]));
          }
          cy.wait(5500);
          cy.request(`/api/tasks/${scope.task_id}/`).then(({ body: task }) => {
            expect(task.drafts).to.deep.eq([]);
            expect(task.annotations).to.have.length(action === "submitted" ? 1 : 0);
            if (submitted) {
              expect(task.annotations[0].id).to.eq(submitted.id);
              expect(task.annotations[0].result).to.deep.eq(submitted.result);
              expect(task.annotations[0].result[0].value).to.deep.eq(remoteBox);
            }
          });
        });
      });
    }
  }
});
