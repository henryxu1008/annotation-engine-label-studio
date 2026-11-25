/// <reference types="cypress" />

// This spec runs only against the explicitly guarded synthetic test server.
// The production public-mix gate remains closed until its release criteria pass.
describe('synthetic public mixed assignment and reassignment', () => {
  let fixture: any;
  let reviewerEmail: string;
  before(() => {
    cy.readFile(Cypress.env('ls82BlindFixture') || '.classification82-blind-e2e.json', { log: false })
      .then((value) => { fixture = value; });
    cy.readFile(Cypress.env('ls82SourceFixture') || '.classification81-e2e.json', { log: false }).then((value) => {
      reviewerEmail = value.users.reviewer.email;
    });
  });

  for (const locale of ['en-US', 'zh-CN'] as const) {
    for (const kind of ['ordinary', 'quality'] as const) {
      it(`dispatches, reassigns and submits ${kind} work in ${locale}`, () => {
        const scope = fixture.projects[locale];
        const item = scope.public_dispatch[kind];
        const manager = { Authorization: `Token ${fixture.manager_token}` };
        const url = `/api/projects/${scope.id}/classification-disagreements/gold/mix/`;
        const key = crypto.randomUUID();
        const body = { task_id: item.task_id, assignee_id: item.original_id,
          references: kind === 'quality' ? [item.reference] : [], ratio_basis_points: kind === 'quality' ? 10000 : 0 };
        const text = kind === 'quality' ? 'Synthetic 分类 split' : `Synthetic public ordinary task for ${locale}`;
        let decision: number;
        let assignment: number;
        let oldHandle: number;
        let newHandle: number;

        cy.loginAs(item.original_email, fixture.password, '/user/account/personal-info');
        cy.request({ method: 'POST', url, body, headers: { 'Idempotency-Key': key }, failOnStatusCode: false })
          .its('status').should('be.oneOf', [403, 404]);
        cy.request({ method: 'POST', url, body, headers: { ...manager, 'Idempotency-Key': key }, log: false })
          .then(({ body: result }) => {
            expect(Object.keys(result).sort()).to.deep.equal(['assignment_id', 'decision_id']);
            decision = result.decision_id;
            assignment = result.assignment_id;
          });
        cy.request({ method: 'POST', url, body, headers: { ...manager, 'Idempotency-Key': key }, log: false })
          .then(({ body: result }) => { expect(result).to.deep.equal({ decision_id: decision, assignment_id: assignment }); });
        cy.request(`/api/projects/${scope.id}/blind-work/`).then(({ body: result }) => {
          expect(result.results).to.have.length(1);
          expect(result.results[0].data.text).to.eq(text);
          oldHandle = result.results[0].id;
          expect(oldHandle).to.be.greaterThan(2 ** 51 - 1);
          expect(oldHandle).not.to.eq(item.task_id);
        });
        cy.then(() => cy.request({ method: 'DELETE', url: `/api/task-assignments/${assignment}/`, headers: manager,
          log: false })).its('status').should('eq', 204);
        cy.then(() => cy.request({ url: `/api/tasks/blind/${oldHandle}/`, failOnStatusCode: false }))
          .its('status').should('eq', 404);

        const replacementKey = crypto.randomUUID();
        const replacement = { task_id: item.replacement_task_id, assignee_id: item.replacement_id };
        let successor: number;
        cy.then(() => cy.request({ method: 'POST', url: `${url}${decision}/reassign/`, body: replacement,
          headers: { ...manager, 'Idempotency-Key': replacementKey }, log: false })).then(({ body: result }) => {
            successor = result.assignment_id;
            expect(successor).not.to.eq(assignment);
          });
        cy.then(() => cy.request({ method: 'POST', url: `${url}${decision}/reassign/`, body: replacement,
          headers: { ...manager, 'Idempotency-Key': replacementKey }, log: false }))
          .then(({ body: result }) => { expect(result.assignment_id).to.eq(successor); });

        cy.loginAs(item.replacement_email, fixture.password, '/user/account/personal-info');
        cy.setUserLocale(locale);
        cy.then(() => cy.request({ url: `/api/tasks/blind/${oldHandle}/`, failOnStatusCode: false }))
          .its('status').should('eq', 404);
        cy.request(`/api/projects/${scope.id}/blind-work/`).then(({ body: result }) => {
          expect(result.results).to.have.length(1);
          expect(result.results[0].data.text).to.eq(text);
          newHandle = result.results[0].id;
          expect(newHandle).not.to.eq(oldHandle);
        });
        cy.intercept({ method: 'GET', pathname: new RegExp(`/api/projects/${scope.id}/work-mode/?$`) })
          .as('publicWorkMode');
        cy.visit(`/projects/${scope.id}/data`);
        cy.wait('@publicWorkMode', { requestTimeout: 30000, responseTimeout: 60000 })
          .its('response.statusCode').should('eq', 200);
        cy.window().its('DM.SDK.settings.blindProtocol', { timeout: 30000 }).should('eq', true);
        cy.get('.lsf-datamanager', { timeout: 30000 }).contains(text).parent()
          .contains('button', locale === 'zh-CN' ? '打开标注' : 'Open annotation').click();
        cy.window({ timeout: 30000 }).should((win: any) => {
          expect(win.dataManager.store.taskStore.selected?.id).to.eq(newHandle);
        });
        cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]', { timeout: 30000 })
          .should('not.be.disabled').check({ force: true });
        cy.intercept('POST', '**/api/tasks/blind/*/annotations*').as('submitPublicSuccessor');
        cy.get('[data-testid="bottombar-submit-button"]').should('not.be.disabled').click();
        cy.wait('@submitPublicSuccessor').its('response.statusCode').should('eq', 201);

        cy.request<{ id: number; assignment: number; revision: number; result_hash: string }[]>({
          url: `/api/submissions/?project=${scope.id}`,
          headers: manager, log: false }).then(({ body: submissions }) => {
          const stored = submissions.filter((row) => row.assignment === successor);
          expect(stored).to.have.length(1);
          if (kind === 'quality') {
            const scoreUrl = `/api/projects/${scope.id}/classification-disagreements/gold/blind-scores/`;
            const waitForScore = (attempt: number): Cypress.Chainable<any> => cy.request<{
              items: { id: number; submission_id: number }[];
            }>({ url: scoreUrl, headers: manager, log: false }).then(({ body: scores }) => {
              const score = scores.items.find((row) => row.submission_id === stored[0].id);
              if (score) {
                return cy.request<{ manifest: { submission_id: number; revision: number; submission_hash: string;
                  reference_hash: string; result: { status: string; matched_controls: number; total_controls: number } } }>({
                  url: `${scoreUrl}${score.id}/`, headers: manager, log: false,
                }).then(({ body: detail }) => {
                  expect(detail.manifest.submission_id).to.eq(stored[0].id);
                  expect(detail.manifest.revision).to.eq(stored[0].revision);
                  expect(detail.manifest.submission_hash).to.eq(stored[0].result_hash);
                  expect(detail.manifest.reference_hash).to.eq(item.reference.hash);
                  expect(detail.manifest.result.status).to.eq('scored');
                  expect(detail.manifest.result.matched_controls).to.eq(1);
                  expect(detail.manifest.result.total_controls).to.eq(1);
                });
              }
              expect(attempt, 'score worker completed the queued submission').to.be.lessThan(40);
              return cy.wait(500, { log: false }).then(() => waitForScore(attempt + 1));
            });
            waitForScore(0);
          }
          cy.loginAs(reviewerEmail, fixture.password, '/user/account/personal-info');
          cy.request<{ results: { id: number }[] }>({ url: `/api/submissions/?project=${scope.id}&reviewable=true` })
            .then(({ body: queue }) => {
              expect(queue.results.some((row) => row.id === stored[0].id)).to.eq(kind === 'ordinary');
            });
          cy.request({ method: 'POST', url, body, headers: { 'Idempotency-Key': crypto.randomUUID() },
            failOnStatusCode: false }).its('status').should('be.oneOf', [403, 404]);
        });
      });
    }
  }
});
