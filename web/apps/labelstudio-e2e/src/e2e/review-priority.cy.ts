/// <reference types="cypress" />

describe("optional frozen conflict-priority sampling", () => {
  let fixture: any;
  let releasePending: (() => void) | undefined;
  before(() => cy.readFile(".review-priority-e2e.json").then((value) => { fixture = value; }));
  afterEach(() => { releasePending?.(); releasePending = undefined; });
  for (const locale of ["en-US", "zh-CN"] as const) {
    it(`freezes selection, preserves revisions and recovers the original key in ${locale}`, () => {
      const zh = locale === "zh-CN";
      const scope = fixture.projects[locale];
      const base = `/api/projects/${scope.id}/review-batches/`;
      const name = `Priority ${locale}`;
      let saved: number;
      let key: any;
      let body: any;
      let posts = 0;
      let oldPlan: any;
      const originals: any = {};
      let selectedSources: number[] = [];
      const visit = (role: string) => {
        cy.loginAs(fixture.users[role].email, fixture.password, "/user/account/personal-info");
        cy.setUserLocale(locale);
        cy.visit(`/projects/${scope.id}/data`);
        cy.get("html").should("have.attr", "lang", locale);
      };
      visit("manager");
      // Create a real legacy batch, without the optional strategy.
      cy.request("POST", `${base}preview/`, { name: "Legacy", rate: 5, seed: "legacy" }).then(({ body }) => {
        oldPlan = body;
        cy.request({ method: "POST", url: base, body: { name: "Legacy", rate: 5, seed: "legacy", expected_plan_hash: body.plan_hash },
          headers: { "Idempotency-Key": crypto.randomUUID() } });
      });
      for (const source of scope.cases.split.sources) {
        cy.request(`/api/submissions/${source}/`).then(({ body }) => { originals[source] = body; });
      }
      cy.get('[data-testid="open-review-batches"]', { timeout: 30000 }).click();
      cy.contains("label", zh ? "审核批次名称" : "Batch name").find("input").type(name);
      cy.contains("label", zh ? "抽样比例" : "Sample percentage").find("input").clear().type("5");
      cy.contains("label", zh ? "优先抽审冻结分类分歧" : "Prioritize frozen classification conflicts").find("input").check();
      cy.contains("label", zh ? "冻结分类批次" : "Frozen classification batch").find("select").select(String(scope.analysis.id));
      // Hold a real preview response until after blur: no rendered preview or
      // creation intent exists yet, so invalidation must fence the pending read.
      let previewHeld = false;
      cy.intercept({ method: "POST", pathname: new RegExp(`^${base}preview/?$`), times: 1 }, (req) => {
        req.continue((res) => {
          expect(res.statusCode).to.eq(200);
          previewHeld = true;
          return new Cypress.Promise<void>((resolve) => { releasePending = resolve; });
        });
      }).as("blurredPriorityPreview");
      cy.contains("button", zh ? "预览抽样" : "Preview sampling").click();
      cy.wrap(null).should(() => expect(previewHeld).to.eq(true));
      cy.window().then((win) => { win.dispatchEvent(new win.Event("blur")); });
      cy.contains("label", zh ? "审核批次名称" : "Batch name").should("not.exist");
      cy.then(() => { releasePending?.(); releasePending = undefined; });
      cy.wait("@blurredPriorityPreview").its("response.statusCode").should("eq", 200);
      cy.get('[data-testid="review-batch-preview"]').should("not.exist");
      cy.get('[data-testid="review-batch-refresh"]').click();
      cy.contains("label", zh ? "优先抽审冻结分类分歧" : "Prioritize frozen classification conflicts").find("input").check();
      cy.contains("label", zh ? "冻结分类批次" : "Frozen classification batch").find("select").select(String(scope.analysis.id));
      cy.contains("button", zh ? "预览抽样" : "Preview sampling").click();
      cy.get('[data-testid="review-priority-summary"]').should("contain.text", zh ? "冻结分歧：3 · 抽中：2" : "Frozen conflicts: 3 · selected: 2");
      cy.get('[data-testid="review-batch-preview"]').should("contain.text", zh ? "未覆盖全部分歧" : "does not cover every conflict");
      cy.intercept({ method: "POST", pathname: new RegExp(`^${base.replace(/\/$/, "")}/?$`) }, (req) => {
        posts += 1;
        if (posts === 1) {
          key = req.headers["idempotency-key"]; body = req.body;
          req.continue((res) => {
            expect(res.statusCode).to.eq(201);
            saved = res.body.id;
            res.send({ statusCode: 503, body: { detail: "Synthetic lost acknowledgement" } });
          });
        } else {
          expect(req.headers["idempotency-key"]).to.eq(key);
          expect(req.body).to.deep.eq(body);
          req.continue((res) => { expect(res.statusCode).to.eq(200); expect(res.body.id).to.eq(saved); expect(res.body.replayed).to.eq(true); });
        }
      }).as("priorityCreate");
      cy.contains("button", zh ? "创建／重试" : "Create / retry").click();
      cy.wait("@priorityCreate").its("response.statusCode").should("eq", 503);
      cy.contains("button", zh ? "恢复原批次创建" : "Recover original batch creation").should("be.visible");
      cy.get('[data-testid="review-batch-refresh"]').click();
      cy.contains("button", zh ? "恢复原批次创建" : "Recover original batch creation").click();
      cy.wait("@priorityCreate").its("response.statusCode").should("eq", 200);
      cy.contains("h3", name).scrollIntoView().should("be.visible");
      cy.contains(zh ? "修订与冻结分析不匹配" : "revision differs from frozen analysis").should("exist");
      cy.then(() => cy.request(`${base}${saved}/`)).then(({ body }) => {
        expect(body.selected_count).to.eq(2);
        expect(body.items.filter((i: any) => i.selected).every((i: any) => scope.cases.split.sources.includes(i.submission_id))).to.eq(true);
        expect(body.priority_source.manifest_hash).to.eq(scope.analysis.manifest_hash);
        expect(body.items.find((i: any) => i.submission_id === scope.newer).priority_reason).to.eq("revision_mismatch");
        expect(posts).to.eq(2);
        selectedSources = body.items.filter((i: any) => i.selected).map((i: any) => i.submission_id);
      });
      for (const source of scope.cases.split.sources) {
        cy.request(`/api/submissions/${source}/`).then(({ body }) => expect(body).to.deep.eq(originals[source]));
      }
      cy.request("POST", `${base}preview/`, { name: "Legacy", rate: 5, seed: "legacy" }).then(({ body }) => expect(body).to.deep.eq(oldPlan));
      cy.screenshot(`review-priority-${locale}`, { capture: "viewport" });
      visit("reviewer");
      cy.then(() => cy.request(`${base}${saved}/`)).then(({ body }) => {
        expect(body.priority_source).to.eq(undefined);
        expect(JSON.stringify(body)).not.to.contain(scope.analysis.manifest_hash);
        expect(body.items.every((i: any) => i.status === "pending")).to.eq(true);
      });
      cy.get('[data-testid="open-review-batches"]').click();
      cy.contains("button", name).click();
      cy.contains("button", zh ? "审核本批样本" : "Review this sample").click();
      cy.get('[data-testid^="review-submission-"]').should("have.length", 2);
      visit("c");
      cy.then(() => cy.request(`${base}${saved}/`)).then(({ body }) => {
        expect(body.selected_count).to.eq(0);
        expect(body.priority_summary.conflict_candidates).to.eq(0);
        expect(body.items.every((i: any) => ![scope.cases.agreement.task, scope.cases.split.task, scope.cases.missing.task].includes(i.task_id))).to.eq(true);
      });
      cy.then(() => cy.request(`${base}${saved}/queue/`)).its("body.count").should("eq", 0);
      cy.then(() => {
        const nonSelf = selectedSources.find((id) => id !== scope.cases.split.sources[2]);
        return cy.request({ method: "POST", url: `/api/submissions/${nonSelf}/review/?review_batch=${saved}`,
          body: { decision: "approved" }, failOnStatusCode: false });
      }).its("status").should("eq", 403);
      visit("a");
      cy.then(() => cy.request({ url: `${base}${saved}/`, failOnStatusCode: false })).its("status").should("be.oneOf", [403, 404]);
    });
  }
});
