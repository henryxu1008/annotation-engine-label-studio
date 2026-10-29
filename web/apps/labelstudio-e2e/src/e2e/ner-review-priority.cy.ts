/// <reference types="cypress" />

describe("optional frozen NER conflict-priority review", () => {
  let fixture: any;
  before(() => cy.readFile(".ner-review-priority-e2e.json").then((value) => { fixture = value; }));
  for (const locale of ["en-US", "zh-CN"] as const) {
    it(`preserves the full review/rework workflow and original receipt in ${locale}`, () => {
      const zh = locale === "zh-CN";
      const scope = fixture.projects[locale];
      const base = `/api/projects/${scope.id}/review-batches/`;
      const name = `NER priority ${locale}`;
      let saved: number, rejected: any, original: any, newer: number;
      let key: any, body: any, posts = 0, legacy: any, frozenHash: string;
      const visit = (role: string) => {
        cy.loginAs(fixture.users[role].email, fixture.password, "/user/account/personal-info");
        cy.setUserLocale(locale);
        cy.visit(`/projects/${scope.id}/data`);
        cy.get("html").should("have.attr", "lang", locale);
      };
      const open = () => cy.get('[data-testid="open-review-batches"]', { timeout: 30000 }).click();
      visit("manager");
      cy.request("POST", `${base}preview/`, { name: "Legacy", rate: 5, seed: "legacy" }).then(({ body }) => {
        legacy = body;
        cy.request({ method: "POST", url: base, body: { name: "Legacy", rate: 5, seed: "legacy", expected_plan_hash: body.plan_hash },
          headers: { "Idempotency-Key": crypto.randomUUID() } });
      });
      open();
      cy.contains("label", zh ? "审核批次名称" : "Batch name").find("input").type(name);
      cy.contains("label", zh ? "抽样比例" : "Sample percentage").find("input").clear().type("10");
      cy.contains("label", zh ? "优先抽审冻结 NER 分歧" : "Prioritize frozen NER conflicts").find("input").check();
      cy.contains("label", zh ? "冻结 NER 批次" : "Frozen NER batch").find("select").select(String(scope.analysis.id));
      cy.contains("button", zh ? "预览抽样" : "Preview sampling").click();
      cy.get('[data-testid="review-priority-summary"]').should("contain.text", zh ? "冻结分歧：3 · 抽中：2" : "Frozen conflicts: 3 · selected: 2");
      cy.intercept({ method: "POST", pathname: new RegExp(`^${base.replace(/\/$/, "")}/?$`) }, (req) => {
        posts += 1;
        if (posts === 1) {
          key = req.headers["idempotency-key"]; body = req.body;
          req.continue((res) => {
            expect(res.statusCode).to.eq(201); saved = res.body.id; frozenHash = res.body.plan_hash;
            res.send({ statusCode: 503, body: { detail: "Synthetic lost acknowledgement" } });
          });
        } else {
          expect(req.headers["idempotency-key"]).to.eq(key); expect(req.body).to.deep.eq(body);
          req.continue((res) => { expect(res.statusCode).to.eq(200); expect(res.body.id).to.eq(saved); expect(res.body.replayed).to.eq(true); });
        }
      }).as("nerPriorityCreate");
      cy.contains("button", zh ? "创建／重试" : "Create / retry").click();
      cy.wait("@nerPriorityCreate").its("response.statusCode").should("eq", 503);
      cy.get('[data-testid="review-batch-refresh"]').click();
      cy.contains("button", zh ? "恢复原批次创建" : "Recover original batch creation").click();
      cy.wait("@nerPriorityCreate").its("response.statusCode").should("eq", 200);
      cy.contains("h3", name).scrollIntoView().should("be.visible");
      cy.then(() => cy.request(`${base}${saved}/`)).then(({ body }) => {
        expect(body.algorithm).to.eq("ner-conflict-ranked/v1"); expect(body.selected_count).to.eq(2);
        expect(body.priority_source.manifest_hash).to.eq(scope.analysis.manifest_hash);
        expect(body.items.find((i: any) => i.submission_id === scope.newer).priority_reason).to.eq("revision_mismatch");
        const selected = body.items.filter((i: any) => i.selected);
        expect(selected.every((i: any) => i.task_id === scope.cases.conflict.task)).to.eq(true);
        rejected = scope.cases.conflict.sources.find((s: any) => selected.some((i: any) => i.submission_id === s.id) && s.owner !== "self");
        expect(rejected).to.exist;
        cy.request(`/api/submissions/${rejected.id}/`).then(({ body }) => { original = body; });
      });
      cy.request("POST", `${base}preview/`, { name: "Legacy", rate: 5, seed: "legacy" }).then(({ body }) => expect(body).to.deep.eq(legacy));
      cy.contains("button", zh ? "发起仲裁" : "Start arbitration").should("not.exist");
      cy.screenshot(`ner-review-priority-manager-${locale}`);
      visit("self");
      cy.then(() => cy.request(`${base}${saved}/`)).then(({ body }) => {
        expect(body.priority_source).to.eq(undefined); expect(body.selected_count).to.eq(0);
        expect(body.items.every((i: any) => i.task_id !== scope.cases.conflict.task)).to.eq(true);
        expect(JSON.stringify(body)).not.to.contain(scope.analysis.manifest_hash);
      });
      cy.then(() => cy.request(`${base}${saved}/queue/`)).its("body.count").should("eq", 0);
      cy.then(() => cy.request({ method: "POST", url: `/api/submissions/${rejected.id}/review/?review_batch=${saved}`,
        body: { decision: "approved" }, failOnStatusCode: false })).its("status").should("eq", 403);
      visit("reviewer"); open();
      cy.contains("button", name).click();
      cy.contains("button", zh ? "审核本批样本" : "Review this sample").click();
      cy.get('[data-testid^="review-submission-"]').should("have.length", 2);
      cy.then(() => cy.get(`[data-testid="review-submission-${rejected.id}"]`).click());
      cy.get('[data-testid="review-reject-reason"]').type("Check entity boundary 核对实体边界");
      cy.get('[data-testid="review-reject"]').click();
      cy.get('[data-testid="review-approve"]').click();
      cy.get('[data-testid="review-queue-empty"]').scrollIntoView().should("be.visible");
      cy.then(() => cy.request({ method: "POST", url: `/api/submissions/${rejected.id}/review/?review_batch=${saved}`,
        body: { decision: "approved" }, failOnStatusCode: false })).its("status").should("eq", 400);
      cy.then(() => visit(rejected.owner));
      cy.get('[data-testid="open-review-batches"]').should("not.exist");
      cy.then(() => cy.request({ url: `${base}${saved}/`, failOnStatusCode: false })).its("status").should("be.oneOf", [403, 404]);
      cy.then(() => cy.visit(`/projects/${scope.id}/data?task=${rejected.task_id}`));
      cy.then(() => cy.get(`[data-annotation-id="${rejected.annotation_id}"]`, { timeout: 30000 }).click());
      cy.window().should((win: any) => { expect(String(win.Htx?.annotationStore?.selected?.pk)).to.eq(String(rejected.annotation_id)); expect(win.Htx?.isLoading).to.eq(false); });
      cy.contains('[role="treeitem"]', "PER").scrollIntoView().click();
      cy.get('.lsf-label').contains('ORG').click();
      cy.window().should((win: any) => {
        const result = win.Htx.annotationStore.selected.serializeAnnotation();
        expect(result).to.have.length(1); expect(result[0].value.labels).to.deep.eq(['ORG']);
      });
      // The ordinary editor creates the next revision. No mutation of frozen sources.
      cy.intercept({ method: "PATCH", pathname: /\/api\/annotations\/\d+\/?$/ }).as("resubmitNER");
      cy.get('[data-testid="bottombar-update-button"]').click();
      cy.wait("@resubmitNER").its("request.body.submit_for_review").should("eq", true);
      cy.request(`/api/submissions/?project=${scope.id}`).then(({ body }) => {
        const value = body.find((s: any) => s.assignment === rejected.assignment_id && s.revision === 2);
        expect(value.status).to.eq("pending"); newer = value.id;
      });
      visit("reviewer"); open(); cy.contains("button", name).click();
      cy.contains("button", zh ? "返工相关" : "Rework follow-up").click();
      cy.contains("button", zh ? "对照返工修订" : "Compare rework revisions").click();
      cy.get('[data-testid="rework-original"]').should("contain.text", "Check entity boundary 核对实体边界");
      cy.contains(zh ? "标注结果已变化。" : "Annotation results changed.").should("be.visible");
      cy.screenshot(`ner-review-priority-rework-${locale}`);
      cy.contains("button", zh ? "返回批次" : "Back to batch").click();
      visit("reviewer");
      cy.get('[data-testid="open-review-workspace"]').click();
      cy.then(() => cy.get(`[data-testid="review-submission-${newer}"]`).click());
      cy.get('[data-testid="review-approve"]').click();
      visit("manager");
      cy.then(() => cy.request(`${base}${saved}/`)).then(({ body }) => {
        expect(body.plan_hash).to.eq(frozenHash); expect(body.candidate_count).to.eq(11);
        expect(body.items.find((i: any) => i.submission_id === rejected.id).status).to.eq("rejected");
      });
      cy.then(() => cy.request(`/api/submissions/${rejected.id}/`)).then(({ body }) => {
        expect(body.result_hash).to.eq(original.result_hash); expect(body.result_snapshot).to.deep.eq(original.result_snapshot);
      });
      cy.then(() => cy.request(`/api/submissions/${newer}/`)).its("body.status").should("eq", "approved");
      cy.request(`/api/projects/${scope.id}/members/?limit=100`).then(({ body }) => {
        const member = (body.results ?? body).find((m: any) => m.user.id === fixture.users.reviewer.id);
        cy.request("DELETE", `/api/projects/${scope.id}/members/${member.id}/`);
      });
      visit("reviewer");
      cy.get('[data-testid="open-review-batches"]').should("not.exist");
      cy.then(() => cy.request({ url: `${base}${saved}/`, failOnStatusCode: false })).its("status").should("be.oneOf", [403, 404]);
    });
  }
});
