/// <reference types="cypress" />

describe("deterministic review sampling and rework", () => {
  let fixture: any;
  before(() => cy.readFile(".review84-e2e.json").then((data) => { fixture = data; }));

  for (const locale of ["en-US", "zh-CN"] as const) {
    it(`freezes a sample, rejects, reworks and reviews a new revision in ${locale}`, () => {
      const zh = locale === "zh-CN";
      const scope = fixture.projects[locale];
      const path = `/projects/${scope.id}/data`;
      const base = `/api/projects/${scope.id}/review-batches/`;
      let batch: number;
      let rejected: any;
      let oldSnapshot: any;
      let newSubmission: number;
      const visit = (role: string, destination = path) => {
        cy.loginAs(fixture.users[role].email, fixture.password, "/user/account/personal-info");
        cy.get('[data-testid="language-preference-select"]').select(locale);
        cy.get("html").should("have.attr", "lang", locale);
        cy.visit(destination);
      };
      const open = () => cy.get('[data-testid="open-review-batches"]', { timeout: 30000 }).click();
      visit("manager");
      open();
      cy.contains("label", zh ? "审核批次名称" : "Batch name").find("input").type(`Sample ${locale}`);
      cy.contains("label", zh ? "抽样比例" : "Sample percentage").find("input").clear().type("50");
      cy.contains("button", zh ? "预览抽样" : "Preview sampling").click();
      cy.get('[data-testid="review-batch-preview"]').should("contain.text", zh ? "4 条提交中抽中 2 条" : "Selected 2 of 4");
      cy.intercept({ method: "POST", pathname: new RegExp(`/api/projects/${scope.id}/review-batches/?$`) }).as("create");
      cy.contains("button", zh ? "创建／重试" : "Create / retry").click();
      cy.wait("@create").then(({ response }) => {
        expect(response?.statusCode).to.eq(201);
        batch = response!.body.id;
      });
      cy.contains("h3", `Sample ${locale}`).scrollIntoView().should("be.visible");
      cy.get('[data-testid="review-batch-progress"]').within(() => {
        cy.contains("dt", zh ? "抽中 · 待审核" : "Selected · pending").siblings("dd").should("have.text", "2");
        cy.contains("dt", zh ? "未抽中" : "Unselected").siblings("dd").should("have.text", "2");
      });
      cy.contains(zh ? "未抽中，不代表批准" : "Unselected — not approval").scrollIntoView().should("be.visible");
      cy.then(() => cy.request(`${base}${batch}/`)).then(({ body }) => {
        expect(body.selected_count).to.eq(2);
        rejected = scope.sources.find((s: any) => s.id === body.items.find((i: any) => i.selected).submission_id);
        cy.request(`/api/submissions/${rejected.id}/`).then(({ body: source }) => { oldSnapshot = source; });
      });
      visit("reviewer");
      open();
      cy.contains("button", `Sample ${locale}`).click();
      cy.contains("button", zh ? "审核本批样本" : "Review this sample").click();
      cy.then(() => cy.get(`[data-testid="review-submission-${rejected.id}"]`).click());
      cy.get('[data-testid="review-reject-reason"]').type("Fix sentiment 修正情感");
      cy.get('[data-testid="review-reject"]').click();
      cy.get('[data-testid="review-batch-progress"]').within(() => {
        cy.contains("dt", zh ? "抽中 · 已驳回" : "Selected · rejected").siblings("dd").should("have.text", "1");
        cy.contains("dt", zh ? "抽中 · 待审核" : "Selected · pending").siblings("dd").should("have.text", "1");
      });
      cy.get('[data-testid="review-approve"]', { timeout: 20000 }).click();
      cy.get('[data-testid="review-queue-empty"]').scrollIntoView().should("be.visible");
      cy.get('[data-testid="review-batch-progress"]').within(() => {
        cy.contains("dt", zh ? "抽中 · 已批准" : "Selected · approved").siblings("dd").should("have.text", "1");
        cy.contains("dt", zh ? "抽中 · 待审核" : "Selected · pending").siblings("dd").should("have.text", "0");
      });
      cy.get('[data-testid="review-history-empty"]').should("not.exist");
      cy.then(() => cy.request({ method: "POST", url: `/api/submissions/${rejected.id}/review/?review_batch=${batch}`,
        body: { decision: "approved" }, failOnStatusCode: false })).its("status").should("eq", 400);

      visit("annotator");
      cy.get('[data-testid="open-review-batches"]').should("not.exist");
      cy.get('[data-testid="open-my-work"]').click();
      cy.contains("Fix sentiment 修正情感").should("be.visible");
      cy.then(() => cy.visit(`${path}?task=${rejected.task_id}`));
      cy.then(() => cy.get(`[data-annotation-id="${rejected.annotation_id}"]`, { timeout: 30000 }).click());
      cy.window().should((win: any) => {
        expect(String(win.Htx?.annotationStore?.selected?.pk)).to.eq(String(rejected.annotation_id));
        expect(win.Htx?.isLoading).to.eq(false);
        expect(win.Htx?.annotationStore?.selected?.autosave).to.be.a("function");
      });
      cy.get('#label-studio-dm input[name="Positive"]').should("be.checked");
      // Change and restore the answer: a new revision can still contain no correction.
      for (const name of ["Negative", "Positive"]) {
        cy.window().then((win) => win.document.querySelector<HTMLInputElement>(`#label-studio-dm input[name="${name}"]`)!.click());
      }
      cy.get('#label-studio-dm input[name="Positive"]').should("be.checked");
      cy.intercept({ method: "PATCH", pathname: /\/api\/annotations\/\d+\/?$/ }).as("resubmit");
      cy.get('[data-testid="bottombar-update-button"]').click();
      cy.wait("@resubmit").its("request.body.submit_for_review").should("eq", true);
      cy.then(() => cy.request(`/api/submissions/?project=${scope.id}`)).then(({ body }) => {
        const newer = body.find((s: any) => s.assignment === rejected.assignment_id && s.revision === 2);
        expect(newer.status).to.eq("pending");
        newSubmission = newer.id;
      });
      visit("reviewer");
      open();
      cy.contains("button", `Sample ${locale}`).click();
      cy.contains("button", zh ? "返工相关" : "Rework follow-up").click();
      cy.then(() => cy.contains("tr", `#${rejected.id}`)
        .should("contain.text", zh ? "最新修订状态: 待审核" : "Latest revision state: Pending review")
        .and("contain.text", "Fix sentiment 修正情感"));
      cy.contains("button", zh ? "对照返工修订" : "Compare rework revisions").click();
      cy.get('[data-testid="rework-original"]').should("contain.text", "Fix sentiment 修正情感");
      cy.contains(zh ? "标注结果无变化。新修订不代表已完成纠正。" : "Annotation results unchanged. A new revision does not prove a correction.").should("be.visible");
      cy.contains("button", zh ? "返回批次" : "Back to batch").click();
      visit("reviewer");
      cy.get('[data-testid="open-review-workspace"]').click();
      cy.then(() => cy.get(`[data-testid="review-submission-${newSubmission}"]`).click());
      cy.intercept({ method: "POST", pathname: /\/api\/submissions\/\d+\/review\/?$/ }).as("rejectNew");
      cy.get('[data-testid="review-reject-reason"]').type("Still unchanged 仍未修正");
      cy.get('[data-testid="review-reject"]').click();
      cy.wait("@rejectNew").its("response.statusCode").should("eq", 200);
      visit("annotator");
      cy.then(() => cy.visit(`${path}?task=${rejected.task_id}`));
      cy.then(() => cy.get(`[data-annotation-id="${rejected.annotation_id}"]`, { timeout: 30000 }).click());
      cy.window().should((win: any) => {
        expect(String(win.Htx?.annotationStore?.selected?.pk)).to.eq(String(rejected.annotation_id));
        expect(win.Htx?.isLoading).to.eq(false);
      });
      cy.window().then((win) => win.document.querySelector<HTMLInputElement>('#label-studio-dm input[name="Negative"]')!.click());
      cy.get('#label-studio-dm input[name="Negative"]').should("be.checked");
      cy.get('[data-testid="bottombar-update-button"]').click();
      cy.wait("@resubmit").its("request.body.submit_for_review").should("eq", true);
      cy.then(() => cy.request(`/api/submissions/?project=${scope.id}`)).then(({ body }) => {
        newSubmission = body.find((s: any) => s.assignment === rejected.assignment_id && s.revision === 3).id;
      });
      visit("reviewer");
      open();
      cy.contains("button", `Sample ${locale}`).click();
      cy.contains("button", zh ? "对照返工修订" : "Compare rework revisions").click();
      cy.get('[data-testid="rework-selected"]').should("contain.text", zh ? "修订 3" : "revision 3");
      cy.contains(zh ? "标注结果已变化。" : "Annotation results changed.").should("be.visible");
      cy.get('[data-testid="rework-diff"]').should("contain.text", "Negative").and("contain.text", "Positive");
      cy.get('[data-testid="rework-original"]').parent().should(($columns) => {
        const style = $columns[0].ownerDocument.defaultView!.getComputedStyle($columns[0]);
        expect(style.display).to.eq("grid");
        expect(style.gridTemplateColumns.split(" ")).to.have.length(2);
      });
      cy.screenshot(`rework-comparison-${locale}`);
      cy.viewport(700, 660);
      cy.get('[data-testid="rework-original"]').parent().should(($columns) => {
        const style = $columns[0].ownerDocument.defaultView!.getComputedStyle($columns[0]);
        expect(style.gridTemplateColumns.split(" ")).to.have.length(1);
      });
      cy.screenshot(`rework-comparison-narrow-${locale}`);
      cy.viewport(1000, 660);
      cy.get('[data-testid="rework-comparison"] select').select(1);
      cy.get('[data-testid="rework-selected"]').should("contain.text", "Still unchanged 仍未修正");
      cy.contains(zh ? "标注结果无变化。新修订不代表已完成纠正。" : "Annotation results unchanged. A new revision does not prove a correction.").should("be.visible");
      cy.contains("button", zh ? "返回批次" : "Back to batch").click();
      visit("reviewer");
      cy.get('[data-testid="open-review-workspace"]').click();
      cy.then(() => cy.get(`[data-testid="review-submission-${newSubmission}"]`).click());
      cy.intercept({ method: "POST", pathname: /\/api\/submissions\/\d+\/review\/?$/ }).as("approveNew");
      cy.get('[data-testid="review-approve"]').click();
      cy.wait("@approveNew").its("response.statusCode").should("eq", 200);
      cy.then(() => cy.request(`/api/submissions/${newSubmission}/`)).its("body.status").should("eq", "approved");
      cy.get('[data-testid="review-error"]').should("not.exist");
      visit("manager");
      open();
      cy.contains("button", `Sample ${locale}`).click();
      cy.contains("button", zh ? "返工相关" : "Rework follow-up").click();
      cy.then(() => cy.contains("tr", `#${rejected.id}`)
        .should("contain.text", zh ? "最新修订状态: 已批准" : "Latest revision state: Approved")
        .and("contain.text", zh ? "已驳回" : "Rejected"));
      cy.get('[data-testid="review-batch-progress"]').within(() => {
        cy.contains("dt", zh ? "抽中 · 已批准" : "Selected · approved").siblings("dd").should("have.text", "1");
        cy.contains("dt", zh ? "抽中 · 已驳回" : "Selected · rejected").siblings("dd").should("have.text", "1");
      });
      cy.contains(zh ? "修订 3" : "revision 3").scrollIntoView().should("be.visible");
      cy.then(() => cy.request(`/api/submissions/${rejected.id}/`)).then(({ body }) => {
        expect(body.result_hash).to.eq(oldSnapshot.result_hash);
        expect(body.result_snapshot).to.deep.eq(oldSnapshot.result_snapshot);
        expect(body.status).to.eq("rejected");
      });
      cy.then(() => cy.request(`${base}${batch}/`)).its("body.candidate_count").should("eq", 4);
      // Real server-side revocation, followed by authorized restoration in this synthetic project.
      cy.request(`/api/projects/${scope.id}/members/?limit=100`).then(({ body }) => {
        const member = (body.results ?? body).find((m: any) => m.user.id === fixture.users.reviewer.id);
        cy.request("DELETE", `/api/projects/${scope.id}/members/${member.id}/`);
      });
      visit("reviewer");
      cy.get('[data-testid="open-review-batches"]').should("not.exist");
      cy.then(() => cy.request({ url: `${base}${batch}/rework/${rejected.id}/`, failOnStatusCode: false }))
        .its("status").should("be.oneOf", [403, 404]);
      visit("manager");
      cy.request("POST", `/api/projects/${scope.id}/members/`, { user_id: fixture.users.reviewer.id, role: "reviewer", enabled: true });
      open();
      cy.contains("button", `Sample ${locale}`).click();
      cy.contains("button", zh ? "对照返工修订" : "Compare rework revisions").click();
      cy.get('[data-testid="rework-original"] h5').scrollIntoView().should("be.visible");
      // Delete only this synthetic source task while its comparison is open.
      cy.then(() => cy.request("DELETE", `/api/tasks/${rejected.task_id}/`));
      cy.contains("button", zh ? "刷新对照" : "Refresh comparison").click();
      cy.get('[data-testid="rework-comparison"] [role="alert"]').should("be.visible");
      cy.get('[data-testid="rework-original"]').should("not.exist");
      cy.get('[data-testid="rework-selected"]').should("not.exist");

    });
  }
});
