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
    cy.get('[role="treeitem"]').contains("Entity").click();
    cy.get('[data-testid="htx-textbox-edit-button"]').click();
    cy.get('[data-testid="htx-textbox-input"]').clear().type(`${text}{enter}`);
  };
  before(() => cy.readFile(".enterprise-e2e.json").then((data) => { fixture = data; }));

  for (const locale of ["en-US", "zh-CN"]) {
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
