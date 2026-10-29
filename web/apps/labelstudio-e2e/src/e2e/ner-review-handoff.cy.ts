/// <reference types="cypress" />

describe("frozen NER review to arbitration handoff", () => {
  let fixture: any;
  before(() => cy.readFile(".ner-review-handoff-e2e.json", { log: false }).then(v => { fixture = v; }));
  for (const locale of ["en-US", "zh-CN"] as const) {
    it(`dispatches, recovers, links, cancels and adopts exact frozen revisions in ${locale}`, () => {
      const zh = locale === "zh-CN", scope = fixture.projects[locale];
      const base = `/api/projects/${scope.id}/review-batches/`;
      const arb = `/api/projects/${scope.id}/ner-arbitrations/`;
      const ids = scope.cases.conflict.sources.map((s: any) => s.id);
      const empty = scope.cases.empty_winner.sources.map((s: any) => s.id);
      const name = `NER handoff ${locale}`, originals: any = {};
      let batch: any, saved: any, key: any, command: any, emptyHandoff: any, posts = 0;
      const workspace = () => cy.get('[data-testid="ner-arbitrations"]').last();
      const login = (role: string) => {
        cy.loginAs(fixture.users[role].email, fixture.password, "/user/account/personal-info");
        cy.setUserLocale(locale); cy.visit(`/projects/${scope.id}/data`);
        cy.get("html").should("have.attr", "lang", locale);
      };
      const openReview = () => {
        cy.get('[data-testid="open-review-batches"]').click();
        cy.contains("button", name).click();
      };
      const openSource = (source: number) => {
        openReview(); cy.get(`[data-handoff-source="${source}"]`).click();
      };
      const action = (en: string, cn: string) => workspace().contains("button", zh ? cn : en);
      const transition = (operation: string) => cy.task("nerHandoffFixture", { operation, locale });
      login("manager");
      for (const id of [...ids, ...empty]) cy.request(`/api/submissions/${id}/`).then(({ body }) => { originals[id] = body; });
      const form = { name, rate: 100, seed: "handoff", priority: { kind: "ner_conflict",
        batch_id: scope.analysis.id, manifest_hash: scope.analysis.manifest_hash } };
      cy.request("POST", base + "preview/", form).then(({ body }) =>
        cy.request({ method: "POST", url: base, headers: { "Idempotency-Key": crypto.randomUUID() },
          body: { ...form, expected_plan_hash: body.plan_hash } })).then(r => { batch = r.body; });
      openReview();
      cy.contains("button", zh ? "抽中 · 待审核" : "Selected · pending").click();
      cy.get(`[data-handoff-source="${ids[0]}"]`).click();
      cy.get('[data-testid="handoff-start"]').click();
      workspace().find("select").select(String(fixture.users.reviewer.id));
      cy.intercept({ method: "POST", pathname: new RegExp(`^${base}[0-9]+/sources/[0-9]+/ner-arbitration-handoffs/?$`), times: 2 }, req => {
        if (++posts === 1) {
          key = req.headers["idempotency-key"]; command = req.body;
          req.continue(res => { expect(res.statusCode).to.eq(201); saved = res.body;
            res.send({ statusCode: 503, body: { detail: "Synthetic lost handoff ACK after commit" } }); });
        } else {
          expect(req.headers["idempotency-key"]).to.eq(key); expect(req.body).to.deep.eq(command);
          req.continue(res => { expect(res.statusCode).to.eq(200); expect(res.body.id).to.eq(saved.id); });
        }
      }).as("handoffWrite");
      action("Confirm dispatch", "确认派发").click();
      cy.wait("@handoffWrite").its("response.statusCode").should("eq", 503);
      action("Back to handoff history", "返回交接历史").should("be.disabled");
      action("Refresh read-only view", "刷新只读视图").click();
      action("Recover original command", "恢复原命令").click();
      cy.wait("@handoffWrite").its("response.statusCode").should("eq", 200);
      workspace().find('[data-testid="ner-left-text"]').should("contain.text", "Alice");
      workspace().find('[data-testid="handoff-source-status"]').should("contain.text", zh ? "待审核" : "Pending");
      action("Back to handoff history", "返回交接历史").click();
      cy.get('[data-testid="review-handoff"]').contains("button", zh ? "返回审核批次" : "Back to review batch").click();
      cy.get(`[data-handoff-source="${ids[0]}"]`).should("have.focus");
      cy.contains("button", zh ? "抽中 · 待审核" : "Selected · pending").should("have.attr", "aria-pressed", "true");
      cy.then(() => expect(posts).to.eq(2));

      login("reviewer"); openSource(ids[0]);
      cy.get('[data-testid="handoff-start"]').should("not.exist");
      cy.then(() => cy.get(`[data-handoff="${saved.id}"]`).click());
      workspace().find('input[type="radio"]').first().check();
      workspace().find("textarea").type(zh ? "采纳完整冻结实体修订" : "Adopt the complete frozen entity revision");
      action("Adopt this frozen revision", "采纳此冻结修订").click();
      workspace().find('[data-testid="arbitration-decision"]').should("exist");

      login("manager"); openSource(ids[1]);
      cy.get('[data-testid="handoff-start"]').click();
      workspace().find("select").should("not.exist");
      cy.intercept({ method: "POST", pathname: new RegExp(`^${base}[0-9]+/sources/[0-9]+/ner-arbitration-handoffs/?$`) }).as("link");
      action("Confirm association", "确认关联").click();
      cy.wait("@link").then(({ request, response }) => {
        expect(request.body.mode).to.eq("link_existing");
        expect(request.body.ner_arbitration_id).to.eq(saved.arbitration_id);
        expect(response?.statusCode).to.eq(201);
      });
      workspace().find('[data-testid="arbitration-decision"]').should("exist");
      transition("corrupt_source");
      action("Refresh read-only view", "刷新只读视图").click();
      workspace().find('[data-testid="ner-left-text"]').should("not.exist");
      workspace().find('[data-testid="arbitration-decision"]').should("not.exist");
      transition("restore_source");
      action("Refresh read-only view", "刷新只读视图").click();
      workspace().find('[data-testid="arbitration-decision"]').should("exist");
      cy.viewport(390, 844);
      cy.screenshot(`ner-handoff-${locale}-narrow`, { capture: "viewport" });
      cy.viewport(1280, 900);

      login("manager"); openSource(empty[0]);
      cy.get('[data-testid="handoff-start"]').click();
      workspace().find("select").select(String(fixture.users.reviewer.id));
      action("Confirm dispatch", "确认派发").click();
      cy.get('[data-testid="arbitration-saved"]').should("exist");
      workspace().find("textarea").type("Cancel before adopting");
      action("Confirm cancellation", "确认取消").click();
      workspace().contains(zh ? '已取消' : 'Cancelled').should('exist');
      action("Back to handoff history", "返回交接历史").click();
      cy.get('[data-testid="handoff-start"]').click();
      workspace().find("select").select(String(fixture.users.reviewer.id));
      action("Confirm dispatch", "确认派发").click();
      cy.get('[data-testid="arbitration-saved"]').should("exist");
      cy.then(() => cy.request(`${base}${batch.id}/sources/${empty[0]}/ner-arbitration-handoffs/`))
        .then(r => { expect(r.body.count).to.eq(2); emptyHandoff = r.body.results[0]; });
      login("reviewer"); openSource(empty[0]);
      cy.then(() => cy.get(`[data-handoff="${emptyHandoff.id}"]`).click());
      workspace().find(`input[type="radio"][value="${empty[1]}"]`).check();
      workspace().find("textarea").type("Adopt a genuine empty entity revision");
      action("Adopt this frozen revision", "采纳此冻结修订").click();
      workspace().find('[data-testid="arbitration-decision"]').should("exist");
      transition("revoke_reviewer");
      action("Refresh read-only view", "刷新只读视图").click();
      workspace().find('[data-testid="ner-left-text"]').should("not.exist");
      cy.then(() => cy.request({ url: `${arb}${emptyHandoff.arbitration_id}/`, failOnStatusCode: false }))
        .its("status").should("be.oneOf", [403, 404]);

      login("manager");
      for (const id of [...ids, ...empty]) cy.request(`/api/submissions/${id}/`).then(({ body }) => expect(body).to.deep.eq(originals[id]));
      for (const role of ["a", "self"]) {
        login(role);
        cy.then(() => cy.request({ url: `${base}${batch.id}/sources/${ids[0]}/ner-arbitration-context/`, failOnStatusCode: false }))
          .its("status").should("be.oneOf", [403, 404]);
        cy.then(() => cy.request({ method: "POST", url: `${base}${batch.id}/sources/${ids[0]}/ner-arbitration-handoffs/`,
          body: command, headers: { "Idempotency-Key": key }, failOnStatusCode: false })).its("status").should("be.oneOf", [403, 404]);
      }
    });
  }
});
