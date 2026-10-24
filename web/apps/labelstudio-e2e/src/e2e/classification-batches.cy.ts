/// <reference types="cypress" />

describe("persistent classification disagreement queues", () => {
  let fixture: any;
  before(() => cy.readFile(".classification-batches81-e2e.json").then((value) => { fixture = value; }));
  for (const locale of ["en-US", "zh-CN"] as const) {
    it(`persists exact inputs, recovers the original key and enforces visibility in ${locale}`, () => {
      const zh = locale === "zh-CN", scope = fixture.projects[locale];
      const base = `/api/projects/${scope.id}/classification-disagreements/batches/`;
      const name = `Synthetic queue ${locale}`;
      let batchId: number, key: any, body: any, manifestHash: string, posts = 0;
      const visit = (role: string) => {
        cy.loginAs(fixture.users[role].email, fixture.password, "/user/account/personal-info");
        cy.get('[data-testid="language-preference-select"]').should("be.enabled").then(($select) => {
          if ($select.val() !== locale) {
            cy.intercept({ method: "PATCH", pathname: /^\/api\/current-user\/locale\/?$/ }).as("batchLocaleSaved");
            cy.get('[data-testid="language-preference-select"]').select(locale);
            cy.wait("@batchLocaleSaved").its("response.statusCode").should("eq", 200);
          }
        });
        cy.get('[data-testid="language-preference-select"]').should("have.value", locale).and("not.be.disabled");
        cy.visit(`/projects/${scope.id}/data`);
        cy.get("html").should("have.attr", "lang", locale);
      };
      const open = () => cy.get('[data-testid="open-classification-batches"]', { timeout: 30000 }).click();
      const workspace = () => cy.get('[data-testid="classification-batches"]');
      const filter = (state: string) => workspace().find("select").select(state);
      const viewTask = (task: number) => workspace().find(`[data-task="${task}"]`).click();
      const back = () => cy.contains("button", zh ? "返回队列" : "Back to queue").click();

      visit("manager"); open();
      cy.contains("button", zh ? "新建比较批次" : "New comparison batch").click();
      workspace().find("input").first().type(name);
      cy.contains("button", zh ? "预览比较" : "Preview comparison").click();
      cy.get('[data-testid="classification-batch-preview"]').should("contain.text", "9");
      cy.intercept({ method: "POST", pathname: new RegExp(`${base.replace(/\/$/, "")}/?$`) }, (req) => {
        posts += 1;
        if (posts === 1) {
          key = req.headers["idempotency-key"]; body = req.body;
          req.continue((res) => {
            expect(res.statusCode).to.eq(201);
            batchId = res.body.id;
            // The database committed; only the browser acknowledgement is lost.
            res.send({ statusCode: 503, body: { detail: "Synthetic acknowledgement loss" } });
          });
        } else {
          expect(req.headers["idempotency-key"]).to.eq(key);
          expect(req.body).to.deep.eq(body);
          req.continue((res) => {
            expect(res.statusCode).to.eq(200);
            expect(res.body.id).to.eq(batchId);
            expect(res.body.replayed).to.eq(true);
          });
        }
      }).as("createFrozenBatch");
      cy.contains("button", zh ? "创建冻结批次" : "Create frozen batch").click();
      cy.wait("@createFrozenBatch").its("response.statusCode").should("eq", 503);
      cy.contains("button", zh ? "恢复原请求" : "Recover original request").should("be.visible");
      cy.contains("button", zh ? "返回批次列表" : "Back to batches").click();
      cy.contains("button", name).should("be.visible");
      cy.contains("button", zh ? "刷新只读视图" : "Refresh read-only view").click();
      cy.contains("button", name).should("be.visible");
      cy.then(() => expect(posts).to.eq(1));
      cy.contains("button", zh ? "恢复原请求" : "Recover original request").click();
      cy.wait("@createFrozenBatch");
      workspace().find("h3").should("contain.text", name);
      cy.screenshot(`classification-batches-queue-${locale}`, { capture: "viewport" });
      cy.then(() => cy.request(`${base}${batchId}/`)).then(({ body }) => {
        expect(body.visible_task_count).to.eq(9);
        expect(body.display_counts).to.deep.eq({ agreement: 2, disagreement: 1, unavailable: 6 });
        manifestHash = body.manifest_hash;
      });
      viewTask(scope.cases.split.task);
      cy.get('[data-testid="classification-agreement"]').should("contain.text", "33.33%");
      cy.get('[data-testid="classification-location"]').should("contain.text", "Positive").and("contain.text", "Negative");
      cy.screenshot(`classification-batches-comparison-${locale}`, { capture: "viewport" });
      cy.then(() => cy.request(`${base}${batchId}/items/${scope.cases.split.task}/`)).then(({ body }) => {
        expect(body.item.inputs.map((pin: any) => pin.submission_id).sort()).to.deep.eq([...scope.cases.split.sources].sort());
      });
      back(); workspace().find("select").should("have.value", "disagreement");
      filter("unavailable");
      viewTask(scope.cases.unsubmitted.task);
      cy.get('[data-testid="classification-unavailable"]').should("contain.text", zh ? "尚未提交" : "has not submitted");
      cy.get('[data-testid="classification-agreement"]').should("not.exist");
      back(); workspace().find("select").should("have.value", "unavailable");
      viewTask(scope.cases.config.task);
      cy.get('[data-testid="classification-unavailable"]').should("contain.text", zh ? "标签配置不同" : "configurations differ");
      cy.reload(); open(); cy.contains("button", name).click();
      cy.then(() => cy.request(`${base}${batchId}/`)).then(({ body }) => expect(body.manifest_hash).to.eq(manifestHash));

      visit("a");
      cy.get('[data-testid="open-classification-batches"]').should("not.exist");
      cy.request({ url: base, failOnStatusCode: false }).its("status").should("be.oneOf", [403, 404]);
      visit("reviewer"); open(); cy.contains("button", name).click();
      cy.contains("button", zh ? "新建比较批次" : "New comparison batch").should("not.exist");
      cy.then(() => cy.request(`${base}${batchId}/`)).then(({ body }) => {
        expect(body.visible_task_count).to.eq(8);
        expect(body.manifest_hash).to.eq(undefined);
        expect(body.projection_hash).to.be.a("string");
        expect(body.display_counts).to.deep.eq({ agreement: 1, disagreement: 1, unavailable: 6 });
      });
      cy.then(() => cy.request({ url: `${base}${batchId}/items/${scope.cases.self.task}/`, failOnStatusCode: false }))
        .its("status").should("eq", 404);
      filter("all");
      workspace().find(`[data-task="${scope.cases.self.task}"]`).should("not.exist");
      viewTask(scope.cases.split.task);
      cy.get('[data-testid="classification-agreement"]').should("contain.text", "33.33%");

      visit("manager");
      cy.request(`/api/projects/${scope.id}/members/?limit=100`).then(({ body }) => {
        const member = (body.results ?? body).find((row: any) => row.user.id === fixture.users.reviewer.id);
        cy.request("DELETE", `/api/projects/${scope.id}/members/${member.id}/`);
      });
      visit("reviewer");
      cy.get('[data-testid="open-classification-batches"]').should("not.exist");
      cy.then(() => cy.request({ url: `${base}${batchId}/items/${scope.cases.split.task}/`, failOnStatusCode: false }))
        .its("status").should("be.oneOf", [403, 404]);

      visit("manager"); open(); cy.contains("button", name).click();
      cy.request("DELETE", `/api/tasks/${scope.cases.split.task}/`);
      cy.contains("button", zh ? "刷新只读视图" : "Refresh read-only view").click();
      filter("unavailable"); viewTask(scope.cases.split.task);
      cy.get('[data-testid="classification-unavailable"]').should("be.visible");
      cy.get('[data-testid="classification-agreement"]').should("not.exist");
      cy.get('[data-testid="classification-location"]').should("not.exist");
      cy.then(() => cy.request(`${base}${batchId}/`)).then(({ body }) => {
        expect(body.manifest_hash).to.eq(manifestHash);
        expect(body.visible_task_count).to.eq(9);
        expect(body.display_counts).to.deep.eq({ agreement: 2, disagreement: 0, unavailable: 7 });
      });
    });
  }
});
