/// <reference types="cypress" />

describe("Keyboard annotation and composition input", () => {
  let fixture: any;
  const choice = (name: string) => cy.get(`#label-studio-dm input[name="${name}"]`);
  const result = (name: string, values: string[]) => cy.window().should((win: any) => {
    const results = win.Htx.annotationStore.selected.serializeAnnotation();
    expect(results.filter((r: any) => r.from_name === name).flatMap((r: any) => r.value.text)).to.deep.eq(values);
  });
  const key = (element: JQuery<HTMLElement>, init: KeyboardEventInit) => {
    const win = element[0].ownerDocument.defaultView!;
    element[0].dispatchEvent(new win.KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init }));
  };
  // Keyboard activation produces click(detail=0). Use the browser's activation
  // behavior here; native Tab/Space are also exercised independently in Edge.
  // Raw CDP keyboard requests can target Cypress's runner instead of its AUT.
  const activate = () => cy.focused().then({ timeout: 10000 }, ($input) => $input[0].click());
  const open = (scope: any, locale: string) => {
    cy.loginAs(fixture.users.annotator_a.email, fixture.password, "/user/account/personal-info");
    cy.get('[data-testid="language-preference-select"]').select(locale).should("have.value", locale).and("not.be.disabled");
    cy.visit(`/projects/${scope.project_id}/data?task=${scope.task_id}`);
    cy.window().should((win: any) => {
      expect(Number(win.Htx?.task?.id)).to.eq(scope.task_id);
      expect(win.Htx.isLoading).to.eq(false);
    });
    cy.get("html").should("have.attr", "lang", locale);
  };
  before(() => cy.readFile(".enterprise-e2e.json").then((data) => { fixture = data; }));

  for (const locale of ["en-US", "zh-CN"]) {
    it(`keeps focus for consecutive keyboard choice edits in ${locale}`, () => {
      cy.task("createEnterpriseE2EUndoScope", { keyboard: true }).then((scope: any) => {
        open(scope, locale);
        choice("One").focus();
        activate();
        cy.focused().should("have.attr", "name", "One").and("be.checked");
        activate();
        choice("One").should("be.focused").and("not.be.checked");
        // Label shortcuts remain available with the retained checkbox focus.
        cy.focused().type("4");
        choice("Two").should("be.checked");
        choice("Positive").click();
        // Cypress isFocused intentionally excludes the default body element.
        // Assert the actual focus target after the choice releases focus.
        cy.document().should((doc) => expect(doc.activeElement).to.eq(doc.body));
        cy.get("body").type("2");
        choice("Negative").should("be.checked");
      });
    });

    it(`isolates numpad typing and composition from label shortcuts in ${locale}`, () => {
      cy.task("createEnterpriseE2EUndoScope", { keyboard: true }).then((scope: any) => {
        open(scope, locale);
        choice("Positive").click();
        // Start in editor scope: the first numpad keystroke must establish the
        // input scope before any translated event is dispatched.
        cy.get("body").type("9");
        cy.get('[placeholder="Single-line note"]').focus().then(($input) => key($input, { key: "3", keyCode: 99, code: "Numpad3" }));
        choice("One").should("not.be.checked");
        cy.get('[placeholder="Single-line note"]').type("3").should("have.value", "3");
        cy.get('[placeholder="Single-line note"]').then(($input) => key($input, { key: "2", keyCode: 50, isComposing: true }));
        choice("Positive").should("be.checked");
        choice("Negative").should("not.be.checked");
      });
    });

    it(`keeps composition text until explicit submission and restores it after reload in ${locale}`, () => {
      cy.task("createEnterpriseE2EUndoScope", { keyboard: true }).then((scope: any) => {
        open(scope, locale);
        for (const [placeholder, name, shiftKey] of [["Single-line note", "note", false], ["Multiline note", "paragraph", true]] as const) {
          cy.get(`[placeholder="${placeholder}"]`).type("中文候选").then(($input) => key($input, { key: "Enter", keyCode: 13, shiftKey, isComposing: true }));
          cy.focused().should("have.value", "中文候选");
          result(name, []);
          cy.get(`[placeholder="${placeholder}"]`).type(shiftKey ? "{shift}{enter}" : "{enter}");
          result(name, ["中文候选"]);
          cy.get('[data-testid="htx-textbox-edit-button"]').last().click();
          cy.get('[data-testid="htx-textbox-input"]').type("修改").then(($input) => key($input, { key: "Escape", keyCode: 27, isComposing: true }));
          cy.focused().should("have.value", "中文候选修改");
          cy.get('[data-testid="htx-textbox-input"]').then(($input) => key($input, { key: "Enter", keyCode: 13, shiftKey, isComposing: true }));
          cy.focused().should("have.value", "中文候选修改");
          cy.get('[data-testid="htx-textbox-input"]').type(shiftKey ? "{shift}{enter}" : "{enter}");
          result(name, ["中文候选修改"]);
        }
        cy.intercept({ method: /POST|PATCH/, url: /\/api\/(?:tasks\/\d+\/drafts|drafts\/\d+)/ }).as("savedDraft");
        cy.contains("button", locale === "zh-CN" ? "保存草稿" : "Save draft").click();
        cy.wait("@savedDraft").its("response.statusCode").should("be.oneOf", [200, 201]);
        cy.reload();
        result("note", ["中文候选修改"]);
        result("paragraph", ["中文候选修改"]);
      });
    });
  }
});
