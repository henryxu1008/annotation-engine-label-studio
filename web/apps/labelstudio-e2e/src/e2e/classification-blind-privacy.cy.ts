/// <reference types="cypress" />

// Partial privacy coverage for ordinary assigned work in a blind-configured
// project. It does not exercise sample identity or blind dispatch.
describe('participant privacy in classification projects', () => {
  let fixture: any;
  before(() => cy.readFile('.classification81-e2e.json', { log: false }).then((value) => { fixture = value; }));

  for (const locale of ['en-US', 'zh-CN'] as const) {
    it(`blocks raw participant identifiers and preserves ordinary review in ${locale}`, () => {
      const scope = fixture.projects[locale], task = scope.cases.split.task;
      const reviewCase = Cypress.env('reviewCase') || 'agreement';
      const reviewSubmission = scope.cases[reviewCase].sources[0];
      let prediction: number;
      const login = (role: string) => {
        cy.loginAs(fixture.users[role].email, fixture.password, '/user/account/personal-info');
        cy.setUserLocale(locale);
        cy.visit(`/projects/${scope.id}/data`);
        cy.get('html').should('have.attr', 'lang', locale);
      };

      login('manager');
      cy.request({ url: '/api/predictions/', qs: { task } }).then(({ body }) => {
        const existing = (Array.isArray(body) ? body : body.results).find((row: any) => row.model_version === 'synthetic-offline');
        if (existing) prediction = existing.id;
        else cy.request('POST', '/api/predictions/', { task, result: [], model_version: 'synthetic-offline' })
          .then((response) => { expect(response.status).to.eq(201); prediction = response.body.id; });
      });
      cy.request(`/api/tasks/${task}/`).its('body.created_at').should('be.a', 'string');

      login('a');
      cy.request({ url: `/api/tasks/${task}/`, failOnStatusCode: false }).its('status').should('eq', 404);
      cy.request({ url: `/api/submissions/${reviewSubmission}/`, failOnStatusCode: false })
        .its('status').should('eq', 404);
      cy.then(() => cy.request({ url: `/api/predictions/${prediction}/`, failOnStatusCode: false })
        .its('status').should('eq', 404));
      cy.request(`/api/projects/${scope.id}/model-versions`).its('body').should('deep.equal', {});
      cy.request(`/api/projects/${scope.id}/annotators/`).its('body').should('deep.equal', []);
      cy.request(`/api/projects/${scope.id}/summary/`).then(({ body }) => {
        expect(body.created_annotations).to.deep.equal({});
        expect(body.created_labels).to.deep.equal({});
      });
      const orgId = Number(Object.keys(fixture.modes).find((key: string) => key.endsWith(`:${scope.id}`))!.split(':')[0]);
      cy.request(`/api/organizations/${orgId}/memberships/${fixture.users.a.id}/?contributed_to_projects=true`)
        .then(({ body }) => expect(body.contributed_to_projects.map((project: any) => project.id)).not.to.include(scope.id));
      cy.request({ url: '/api/tasks/', qs: { project: scope.id, fields: 'all' }, failOnStatusCode: false })
        .its('status').should('eq', 404);
      cy.request({ url: '/api/dm/views/', qs: { project: scope.id } }).its('body').should('deep.equal', []);
      cy.request(`/api/projects/${scope.id}/blind-work/`).then(({ body }) => {
        expect(body.results).not.to.be.empty;
        expect(body.results.every((row: any) => row.id >= 2 ** 51 && row.task_id === row.id)).to.eq(true);
      });
      login('reviewer');
      cy.get('[data-testid="open-review-workspace"]', { timeout: 30000 }).click();
      cy.get('[data-testid="reviewer-workspace"]').should('exist');
      cy.get(`[data-testid="review-submission-${reviewSubmission}"]`).click();
      cy.get('[data-testid="review-result-snapshot"]').should('contain.text', 'Positive');
      cy.get('[data-testid="review-approve"]').click();
      cy.get('[data-testid="review-status"]').should('contain.text', locale === 'zh-CN' ? '已通过' : 'Approved');
    });
  }
});
