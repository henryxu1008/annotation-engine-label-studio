/// <reference types="cypress" />

describe("frozen review to arbitration handoff", () => {
  let fixture: any;
  before(() => cy.readFile(".review-handoff-e2e.json").then((data) => { fixture = data; }));
  for (const locale of ["en-US", "zh-CN"] as const) {
    it(`dispatches, recovers, adopts and links without changing review in ${locale}`, () => {
      const zh = locale === "zh-CN", scope = fixture.projects[locale];
      const base = `/api/projects/${scope.id}/review-batches/`;
      const arb = `/api/projects/${scope.id}/classification-arbitrations/`;
      const name = `Handoff ${locale}`;
      let batch: any, saved: any, key: any, command: any, posts = 0;
      const originals: any = {};
      const workspace = () => cy.get('[data-testid="classification-arbitrations"]').last();
      const visit = (role: string) => {
        cy.loginAs(fixture.users[role].email, fixture.password, "/user/account/personal-info");
        cy.setUserLocale(locale); cy.visit(`/projects/${scope.id}/data`);
        cy.get("html").should("have.attr", "lang", locale);
      };
      const openReview = () => {
        cy.get('[data-testid="open-review-batches"]').click();
        cy.contains("button", name).click();
      };
      visit("manager");
      for (const source of scope.cases.split.sources) {
        cy.request(`/api/submissions/${source}/`).then(({ body }) => { originals[source] = body; });
      }
      const form = { name, rate: 100, seed: "handoff", priority: { kind: "classification_conflict",
        batch_id: scope.analysis.id, manifest_hash: scope.analysis.manifest_hash } };
      cy.request("POST", `${base}preview/`, form).then(({ body }) => {
        cy.request({ method: "POST", url: base, headers: { "Idempotency-Key": crypto.randomUUID() },
          body: { ...form, expected_plan_hash: body.plan_hash } }).then(({ body }) => { batch = body; });
      });
      openReview();
      cy.contains("button", zh ? "抽中 · 待审核" : "Selected · pending").click();
      cy.get(`[data-handoff-source="${scope.cases.split.sources[0]}"]`).click();
      cy.get('[data-testid="handoff-start"]').click();
      workspace().find("select").select(String(fixture.users.reviewer.id));
      cy.intercept({ method: "POST", pathname: new RegExp(`^${base}[0-9]+/sources/[0-9]+/arbitration-handoffs/?$`), times: 2 }, (req) => {
        if (++posts === 1) {
          key = req.headers["idempotency-key"]; command = req.body;
          req.continue((res) => { expect(res.statusCode).to.eq(201); saved = res.body;
            res.send({ statusCode: 503, body: { detail: "Synthetic lost handoff acknowledgement" } }); });
        } else {
          expect(req.headers["idempotency-key"]).to.eq(key); expect(req.body).to.deep.eq(command);
          req.continue((res) => { expect(res.statusCode).to.eq(200); expect(res.body.id).to.eq(saved.id); });
        }
      }).as("handoffWrite");
      workspace().contains("button", zh ? "确认派发" : "Confirm dispatch").click();
      cy.wait("@handoffWrite").its("response.statusCode").should("eq", 503);
      workspace().contains("button", zh ? "返回交接历史" : "Back to handoff history").should("be.disabled");
      workspace().contains("button", zh ? "刷新只读视图" : "Refresh read-only view").click();
      workspace().contains("button", zh ? "恢复原命令" : "Recover original command").click();
      cy.wait("@handoffWrite").its("response.statusCode").should("eq", 200);
      workspace().find('[data-testid="arbitration-text"]').should("contain.text", "Synthetic 分类 split");
      workspace().find('[data-testid="handoff-source-status"]').should("contain.text", zh ? "待审核" : "Pending");
      cy.then(() => expect(posts).to.eq(2));
      workspace().contains("button", zh ? "返回交接历史" : "Back to handoff history").click();
      cy.get('[data-testid="review-handoff"]').contains("button", zh ? "返回审核批次" : "Back to review batch").click();
      cy.contains("h3", name).should("exist");
      cy.contains("button", zh ? "抽中 · 待审核" : "Selected · pending").should("have.attr", "aria-pressed", "true");
      cy.get(`[data-handoff-source="${scope.cases.split.sources[0]}"]`).should("have.focus");

      visit("reviewer");
      openReview();
      cy.get(`[data-handoff-source="${scope.cases.split.sources[0]}"]`).click();
      cy.get('[data-testid="handoff-start"]').should("not.exist");
      cy.then(() => cy.get(`[data-handoff="${saved.id}"]`).click());
      workspace().find('input[type="radio"]').first().check();
      workspace().find("textarea").type(zh ? "采纳已有冻结修订" : "Adopt existing frozen revision");
      workspace().contains("button", zh ? "采纳此冻结修订" : "Adopt this frozen revision").click();
      workspace().find('[data-testid="arbitration-decision"]').should("exist");
      visit("manager"); openReview();
      // The other review source has the identical semantic cohort: associate
      // explicitly with the resolved task rather than silently dispatching.
      cy.get(`[data-handoff-source="${scope.cases.split.sources[1]}"]`).click();
      cy.get('[data-testid="handoff-start"]').click();
      workspace().find("select").should("not.exist");
      cy.intercept({ method: "POST", pathname: new RegExp(`^${base}[0-9]+/sources/[0-9]+/arbitration-handoffs/?$`) }).as("handoffLink");
      workspace().contains("button", zh ? "确认关联" : "Confirm association").click();
      cy.wait("@handoffLink").then(({ request, response }) => {
        expect(request.body.mode).to.eq("link_existing"); expect(request.body.arbitration_id).to.eq(saved.arbitration_id);
        expect(response?.statusCode).to.eq(201);
      });
      workspace().find('[data-testid="arbitration-decision"]').should("exist");
      workspace().find('[data-testid="handoff-source-status"]').should("contain.text", zh ? "待审核" : "Pending");
      // Joint source evidence failure suppresses the independent task's answers.
      cy.intercept({ method: "GET", pathname: new RegExp(`^${base}[0-9]+/arbitration-handoffs/[0-9]+/?$`), times: 1 }, (req) => {
        req.continue((res) => { res.body.available = false; res.body.reason = "source_unavailable"; });
      }).as("sourceUnavailable");
      workspace().contains("button", zh ? "刷新只读视图" : "Refresh read-only view").click();
      cy.wait("@sourceUnavailable");
      workspace().find('[data-testid="arbitration-text"]').should("not.exist");
      workspace().find('[data-testid="arbitration-decision"]').should("not.exist");
      workspace().contains("button", zh ? "刷新只读视图" : "Refresh read-only view").click();
      workspace().find('[data-testid="arbitration-decision"]').should("exist");
      cy.screenshot(`review-handoff-${locale}`, { capture: "viewport" });
      for (const source of scope.cases.split.sources) {
        cy.request(`/api/submissions/${source}/`).then(({ body }) => expect(body).to.deep.eq(originals[source]));
      }
      // These are real server denials, not UI-only hiding.
      for (const role of ["a", "c"]) {
        visit(role);
        cy.then(() => cy.request({ url: `${base}${batch.id}/sources/${scope.cases.split.sources[0]}/arbitration-context/`, failOnStatusCode: false }))
          .its("status").should("be.oneOf", [403, 404]);
        cy.then(() => cy.request({ url: `${base}${batch.id}/sources/${scope.cases.split.sources[0]}/arbitration-handoffs/`, failOnStatusCode: false }))
          .then(({ status, body }) => {
            if (status === 200) { expect(body.count).to.eq(0); expect(body.results).to.deep.eq([]); }
            else expect(status).to.be.oneOf([403, 404]);
          });
        cy.get('[data-testid="arbitration-text"]').should("not.exist");
      }
      visit("manager");
      cy.then(() => cy.request(`${arb}${saved.arbitration_id}/`)).then(({ body }) => expect(body.status).to.eq("resolved"));
    });
  }
});
