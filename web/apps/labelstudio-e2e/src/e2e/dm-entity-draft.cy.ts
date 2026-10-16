/// <reference types="cypress" />

describe("Restored text entity drafts", () => {
  let fixture: any;
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
    it(`preserves a newer remote draft and recovers conflicted offline edits in ${locale}`, () => {
      cy.task("createEnterpriseE2EUndoScope", { entityDraft: true }).then((scope: any) => {
        cy.loginAs(fixture.users.annotator_a.email, fixture.password, "/user/account/personal-info");
        cy.get('[data-testid="language-preference-select"]').select(locale).should("have.value", locale).and("not.be.disabled");
        let offline = true;
        cy.intercept(draftRoute, (req) => {
          if (offline) req.destroy();
          else req.continue();
        }).as("conflictWrite");
        cy.visit(`/projects/${scope.project_id}/data?task=${scope.task_id}`);
        cy.get('[role="treeitem"]').contains("Entity").should("be.visible").click();
        state(false);
        edit("My offline edit");
        cy.get('[data-testid="draft-save-retry"]').click();
        cy.wait("@conflictWrite").should("have.property", "error");
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
        cy.wait("@conflictWrite", { timeout: 15000 }).its("response.statusCode").should("eq", 409);
        cy.get('[data-testid="draft-conflict"]').should("be.visible")
          .and("contain", locale === "zh-CN" ? "草稿已在另一页面更新" : "Draft changed in another page");
        state(true, "My latest local edit 中文");
        cy.get('[data-testid="draft-save-retry"]').should("not.exist");
        cy.get('[data-testid="draft-reload-latest"]').should("be.disabled");
        cy.request(`/api/tasks/${scope.task_id}/`).its("body.drafts.0.result").then((result) => {
          expect(result.find((r: any) => r.from_name === "entity_note").value.text).to.deep.eq(["Newer remote edit"]);
        });
        cy.get('[data-testid="draft-backup"]').click();
        cy.readFile(`cypress/downloads/task-${scope.task_id}-unsaved-draft.json`).then((backup) => {
          expect(String(backup.task_id)).to.eq(String(scope.task_id));
          expect(backup.result.find((r: any) => r.from_name === "entity_note").value.text).to.deep.eq(["My latest local edit 中文"]);
        });
        cy.get('[data-testid="draft-reload-latest"]').click();
        cy.get('[role="treeitem"]').contains("Entity").should("be.visible");
        state(false, "Newer remote edit");
        cy.get('[data-testid="draft-conflict"]').should("not.exist");
        cy.get('[role="treeitem"]').contains("Entity").click();
        edit("Reapplied local edit 中文");
        cy.get('[data-testid="draft-save-retry"]').click();
        cy.wait("@conflictWrite").its("response.statusCode").should("eq", 200);
        state(false, "Reapplied local edit 中文");
        cy.reload();
        cy.get('[role="treeitem"]').contains("Entity").should("be.visible");
        state(false, "Reapplied local edit 中文");
      });
    });

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
