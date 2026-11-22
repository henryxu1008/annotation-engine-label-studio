/// <reference types="cypress" />

// Synthetic internal sampler and workspace; public mixed dispatch remains off.
describe('project membership revocation in a blind editor', () => {
  let fixture: any;
  before(() => cy.readFile(Cypress.env('blindFixturePath') || '.classification82-blind-e2e.json', { log: false })
    .then((value) => { fixture = value; }));

  for (const locale of ['en-US', 'zh-CN'] as const) {
    for (const kind of ['ordinary', 'quality'] as const) {
      it(`closes a stale ${kind} editor after membership revocation in ${locale}`, () => {
        const scope = fixture.projects[locale];
        const item = scope.revocations[kind];
        let handle: number;
        cy.loginAs(item.email, fixture.password, '/user/account/personal-info');
        cy.setUserLocale(locale);
        cy.visit(`/projects/${scope.id}/data`);
        cy.request(`/api/projects/${scope.id}/blind-work/`).then(({ body }) => {
          expect(body.results).to.have.length(1);
          expect(body.results[0].data.text).to.eq(item.text);
          handle = body.results[0].id;
        });
        cy.window({ timeout: 30000 }).its('DM.SDK.blindWorkspace').should('be.a', 'function');
        cy.window().then((win: any) => {
          const current = win.DM.SDK, workspace = current.blindWorkspace;
          current.destroy();
          const root = win.document.createElement('div'); root.id = 'synthetic-blind-root'; win.document.body.appendChild(root);
          win.syntheticBlindManager = new win.DataManager({ root, projectId: scope.id, apiGateway: `${win.APP_SETTINGS.hostname}/api/dm`, apiVersion: 2,
            project: { id: scope.id }, settings: { blindProtocol: true }, blindWorkspace: workspace,
            interfaces: { labelingHeader: true, backButton: true }, labelStudio: {}, polling: false, locale });
        });
        cy.get('#synthetic-blind-root', { timeout: 30000 }).contains(item.text).parent()
          .contains('button', locale === 'zh-CN' ? '打开标注' : 'Open annotation').click();
        cy.window({ timeout: 30000 }).should((win: any) => expect(win.syntheticBlindManager.store.taskStore.selected?.id).to.eq(handle));

        cy.request({ method: 'PATCH', url: `/api/projects/${scope.id}/members/${item.membership}/`,
          body: { enabled: false }, failOnStatusCode: false }).its('status').should('be.oneOf', [403, 404]);
        // A separate synthetic manager token preserves the member's browser
        // session while exercising the real server-side membership endpoint.
        cy.request({ method: 'PATCH', url: `/api/projects/${scope.id}/members/${item.membership}/`,
          headers: { Authorization: `Token ${fixture.manager_token}` }, body: { enabled: false }, log: false })
          .its('status').should('eq', 200);
        cy.then(() => cy.request({ url: `/api/tasks/blind/${handle}/`, failOnStatusCode: false }))
          .its('status').should('be.oneOf', [403, 404]);
        cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]', { timeout: 30000 })
          .should('not.be.disabled').check({ force: true });
        cy.intercept('POST', '**/api/tasks/blind/*/annotations*').as('revokedSubmit');
        cy.get('[data-testid="bottombar-submit-button"]').should('not.be.disabled').click();
        cy.wait('@revokedSubmit').its('response.statusCode').should('be.oneOf', [403, 404]);
        cy.window({ timeout: 30000 }).should((win: any) => {
          expect(Boolean(win.syntheticBlindManager.store.taskStore.selected)).to.eq(false);
          expect(win.syntheticBlindManager.mode).to.eq('explorer');
        });
        cy.get('#synthetic-blind-root').should('not.contain.text', item.text);
        cy.window().then((win: any) => win.syntheticBlindManager.destroy());
      });
    }
  }
});
