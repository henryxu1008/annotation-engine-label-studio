/// <reference types="cypress" />

describe("frozen text classification arbitration", () => {
  let fixture: any;
  before(() => cy.readFile(".classification-arbitrations81-e2e.json").then((data) => { fixture = data; }));
  for (const locale of ["en-US", "zh-CN"] as const) {
    it(`dispatches, cancels, adopts and recovers exact commands in ${locale}`, () => {
      const zh = locale === "zh-CN", project = fixture.projects[locale];
      const base = `/api/projects/${project.id}/classification-arbitrations/`;
      const batches = `/api/projects/${project.id}/classification-disagreements/batches/`;
      const name = `Arbitration source ${locale}`;
      let batch: any, firstTask: any, activeTask: any, createKey: any, createBody: any, cancelKey: any, cancelBody: any;
      let decisionKey: any, decisionBody: any, decisionId: number, creates = 0, decisions = 0;
      const workspace = () => cy.get('[data-testid="classification-arbitrations"]').last();
      const visit = (role: string) => {
        cy.loginAs(fixture.users[role].email, fixture.password, "/user/account/personal-info");
        cy.get('[data-testid="language-preference-select"]').should("be.enabled").then(($select) => {
          if ($select.val() !== locale) {
            cy.intercept({ method: "PATCH", pathname: /^\/api\/current-user\/locale\/?$/ }).as("arbitrationLocaleSaved");
            cy.get('[data-testid="language-preference-select"]').select(locale);
            cy.wait("@arbitrationLocaleSaved").its("response.statusCode").should("eq", 200);
          }
        });
        cy.visit(`/projects/${project.id}/data`);
        cy.get("html").should("have.attr", "lang", locale);
      };
      const openQueue = () => cy.get('[data-testid="open-classification-arbitrations"]').click();
      visit("manager");
      cy.request("POST", `${batches}preview/`, { name, scope: { kind: "project" } }).then(({ body }) => {
        cy.request({ method: "POST", url: batches, headers: { "Idempotency-Key": crypto.randomUUID() },
          body: { preview_token: body.preview_token, expected_plan_hash: body.plan_hash } }).then(({ body }) => { batch = body; });
      });
      cy.get('[data-testid="open-classification-batches"]').click();
      cy.contains("button", name).click();
      cy.get(`[data-task="${project.cases.split.task}"]`).click();
      cy.get('[data-testid="dispatch-classification-arbitration"]').click();
      workspace().find("select").select(String(fixture.users.reviewer.id));
      cy.intercept({ method: "POST", pathname: new RegExp(`${base.replace(/\/$/, "")}/?$`) }, (req) => {
        if (++creates === 1) {
          createKey = req.headers["idempotency-key"]; createBody = req.body;
          req.continue((res) => {
            expect(res.statusCode).to.eq(201); firstTask = res.body;
            res.send({ statusCode: 503, body: { detail: "Synthetic lost dispatch acknowledgement" } });
          });
        } else {
          expect(req.headers["idempotency-key"]).to.eq(createKey); expect(req.body).to.deep.eq(createBody);
          req.continue((res) => { expect(res.statusCode).to.eq(200); expect(res.body.id).to.eq(firstTask.id); });
        }
      }).as("arbitrationCreate");
      workspace().contains("button", zh ? "确认派发" : "Confirm dispatch").click();
      cy.wait("@arbitrationCreate").its("response.statusCode").should("eq", 503);
      workspace().contains("button", zh ? "刷新只读视图" : "Refresh read-only view").click();
      workspace().contains("button", zh ? "恢复原命令" : "Recover original command").should("be.enabled").click();
      cy.wait("@arbitrationCreate");
      workspace().find('[data-testid="arbitration-text"]').should("contain.text", "Synthetic 分类 split");
      cy.then(() => expect(creates).to.eq(2));
      cy.intercept({ method: "POST", pathname: new RegExp(`${base}[0-9]+/cancel/?$`) }, (req) => {
        cancelKey = req.headers["idempotency-key"]; cancelBody = req.body;
      }).as("arbitrationCancel");
      workspace().find("textarea").type(zh ? "重新指定前显式取消" : "Explicit cancellation before redispatch");
      workspace().contains("button", zh ? "确认取消" : "Confirm cancellation").click();
      cy.wait("@arbitrationCancel").its("response.statusCode").should("eq", 201);
      workspace().find('[data-testid="arbitration-saved"]').should("contain.text", zh ? "取消已确认" : "Cancellation");
      cy.then(() => cy.request({ method: "POST", url: `${base}${firstTask.id}/cancel/`, headers: { "Idempotency-Key": cancelKey }, body: cancelBody }))
        .then(({ status, body }) => { expect(status).to.eq(200); expect(body.replayed).to.eq(true); });
      // Explicit new dispatch after cancellation; the input set is unchanged.
      cy.then(() => cy.request({ method: "POST", url: base, headers: { "Idempotency-Key": crypto.randomUUID() }, body: {
        batch_id: batch.id, task_id: project.cases.split.task, expected_manifest_hash: batch.manifest_hash, assignee_id: fixture.users.reviewer.id,
      } })).then(({ body }) => { activeTask = body; expect(body.id).not.to.eq(firstTask.id); });

      visit("reviewer"); openQueue();
      // A late authorized detail must not override a newer read denial.
      cy.intercept({ method: "GET", pathname: new RegExp(`^${base}capability/?$`), times: 1 }, (req) => {
        req.continue((res) => { res.body.can_read = false; });
      }).as("arbitrationReadRevoked");
      cy.then(() => workspace().find(`[data-arbitration="${activeTask.id}"]`).click());
      cy.wait("@arbitrationReadRevoked");
      workspace().find('[role="alert"]').should("exist");
      workspace().find('[data-testid="arbitration-text"]').should("not.exist");
      workspace().contains("button", zh ? "刷新只读视图" : "Refresh read-only view").click();
      workspace().find('[data-testid="arbitration-text"]').should("contain.text", "Synthetic 分类 split");
      // Keep real data but simulate a capability from a different mapped viewer.
      cy.intercept({ method: "GET", pathname: new RegExp(`^${base}capability/?$`), times: 1 }, (req) => {
        req.continue((res) => { res.body.viewer_hash = "0".repeat(64); });
      }).as("arbitrationViewerChanged");
      workspace().contains("button", zh ? "刷新只读视图" : "Refresh read-only view").click();
      cy.wait("@arbitrationViewerChanged");
      workspace().find('[role="alert"]').should("exist");
      workspace().find('[data-testid="arbitration-text"]').should("not.exist");
      workspace().contains("button", zh ? "刷新只读视图" : "Refresh read-only view").click();
      workspace().find('[data-testid="arbitration-text"]').should("contain.text", "Synthetic 分类 split");
      cy.then(() => cy.request(`${base}${activeTask.id}/`)).then(({ body }) => {
        expect(body.origin_manifest_hash).to.eq(undefined); expect(body.available).to.eq(true);
        expect(body.report.inputs.map((p: any) => p.submission_id).sort()).to.deep.eq([...project.cases.split.sources].sort());
      });
      cy.then(() => cy.request({ method: "POST", url: `${base}${activeTask.id}/decision/`, failOnStatusCode: false,
        headers: { "Idempotency-Key": crypto.randomUUID() }, body: { expected_version: 2, expected_input_set_hash: activeTask.input_set_hash,
          winner_submission_id: project.cases.split.sources[0], reason: "Synthetic stale CAS" } })).its("status").should("eq", 409);
      cy.then(() => cy.request({ method: "POST", url: `${base}${firstTask.id}/decision/`, failOnStatusCode: false,
        headers: { "Idempotency-Key": crypto.randomUUID() }, body: { expected_version: 1, expected_input_set_hash: firstTask.input_set_hash,
          winner_submission_id: project.cases.split.sources[0], reason: "Synthetic late cancelled submission" } })).its("status").should("eq", 409);
      for (const reason of ["\ud800", "\u0000"]) {
        cy.then(() => cy.request({ method: "POST", url: `${base}${activeTask.id}/decision/`, failOnStatusCode: false,
          headers: { "Idempotency-Key": crypto.randomUUID() }, body: { expected_version: 1, expected_input_set_hash: activeTask.input_set_hash,
            winner_submission_id: project.cases.split.sources[0], reason } })).its("status").should("eq", 400);
      }
      cy.intercept({ method: "POST", pathname: new RegExp(`${base}[0-9]+/decision/?$`) }, (req) => {
        if (++decisions === 1) {
          decisionKey = req.headers["idempotency-key"]; decisionBody = req.body;
          req.continue((res) => { expect(res.statusCode).to.eq(201); decisionId = res.body.decision_id;
            res.send({ statusCode: 503, body: { detail: "Synthetic lost adoption acknowledgement" } }); });
        } else {
          expect(req.headers["idempotency-key"]).to.eq(decisionKey); expect(req.body).to.deep.eq(decisionBody);
          req.continue((res) => { expect(res.statusCode).to.eq(200); expect(res.body.decision_id).to.eq(decisionId); });
        }
      }).as("arbitrationDecision");
      workspace().find(`input[type="radio"][value="${project.cases.split.sources[0]}"]`).check();
      workspace().find("textarea").type(zh ? "根据冻结正文采纳现有修订" : "Adopt the existing revision using frozen question evidence");
      workspace().contains("button", zh ? "采纳此冻结修订" : "Adopt this frozen revision").click();
      cy.wait("@arbitrationDecision").its("response.statusCode").should("eq", 503);
      workspace().contains("button", zh ? "刷新只读视图" : "Refresh read-only view").click();
      workspace().find('[data-testid="arbitration-decision"]').should("contain.text", String(project.cases.split.sources[0]));
      workspace().contains("button", zh ? "恢复原命令" : "Recover original command").should("be.enabled").click();
      cy.wait("@arbitrationDecision");
      workspace().find('[data-testid="arbitration-saved"]').should("contain.text", zh ? "采纳已确认" : "Adoption");
      workspace().should("have.attr", "aria-busy", "false");
      workspace().find('[data-testid="arbitration-decision"]').should("contain.text", String(project.cases.split.sources[0]));
      workspace().find('[data-testid="arbitration-text"]').should("contain.text", "Synthetic 分类 split");
      cy.screenshot(`classification-arbitration-resolved-${locale}`, { capture: "viewport" });

      visit("reviewer2");
      cy.request(`${base}?state=all`).its("body.count").should("eq", 0);
      cy.then(() => cy.request({ url: `${base}${activeTask.id}/`, failOnStatusCode: false })).its("status").should("eq", 404);
      visit("a");
      cy.get('[data-testid="open-classification-arbitrations"]').should("not.exist");
      cy.request({ url: base, failOnStatusCode: false }).its("status").should("be.oneOf", [403, 404]);
      visit("manager");
      cy.request("DELETE", `/api/tasks/${project.cases.split.task}/`);
      openQueue(); workspace().find("select").select("resolved");
      cy.then(() => workspace().find(`[data-arbitration="${activeTask.id}"]`).click());
      workspace().find('[data-testid="arbitration-unavailable"]').should("be.visible");
      workspace().find('[data-testid="arbitration-text"]').should("not.exist");
      workspace().find('[data-testid="arbitration-decision"]').should("exist");
      cy.request(`/api/projects/${project.id}/members/?limit=100`).then(({ body }) => {
        const member = body.results.find((m: any) => m.user.id === fixture.users.reviewer.id);
        cy.request("DELETE", `/api/projects/${project.id}/members/${member.id}/`);
      });
      visit("reviewer");
      cy.get('[data-testid="open-classification-arbitrations"]').should("not.exist");
      cy.then(() => cy.request({ url: `${base}${activeTask.id}/`, failOnStatusCode: false })).its("status").should("be.oneOf", [403, 404]);
    });
  }
});
