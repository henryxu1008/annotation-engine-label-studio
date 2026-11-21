/// <reference types="cypress" />

// Synthetic harness: explicitly constructs the still-default-off participant
// SDK. This does not enable dispatch or change the deployment configuration.
describe('restricted classification editor', () => {
  let fixture: any;
  before(() => cy.readFile('.classification81-e2e.json', { log: false }).then((value) => { fixture = value; }));

  for (const locale of ['en-US', 'zh-CN'] as const) {
    it(`opens and submits an opaque ordinary assignment in ${locale}`, () => {
      const scope = fixture.projects[locale];
      const text = `Synthetic restricted editor ${locale} ${Cypress.env('scenario') || 'default'}`;
      let rawTask: number;
      let taskHandle: number;
      let nextVersion: number;
      let annotationHandle: number;
      const login = (role: string) => {
        cy.loginAs(fixture.users[role].email, fixture.password, '/user/account/personal-info');
        cy.setUserLocale(locale);
        cy.visit(`/projects/${scope.id}/data`);
      };

      login('manager');
      cy.request({ url: '/api/tasks/', qs: { project: scope.id, fields: 'all', page_size: 100 } }).then(({ body }) => {
        const existing = body.tasks.find((row: any) => row.data?.text === text);
        if (existing) rawTask = existing.id;
        else return cy.request('POST', '/api/tasks/', { project: scope.id, data: { text } }).then(({ status, body }) => {
          expect(status).to.eq(201);
          rawTask = body.id;
        });
      });
      cy.then(() => cy.request({ url: '/api/task-assignments/', qs: { project: scope.id, task: rawTask } }))
        .then(({ body }) => {
          const rows = Array.isArray(body) ? body : body.results;
          if (!rows.some((row: any) => row.assignee === fixture.users.a.id && row.status !== 'cancelled')) {
            return cy.request('POST', '/api/task-assignments/', { task: rawTask, assignee: fixture.users.a.id })
              .its('status').should('eq', 201);
          }
        });

      login('a');
      cy.then(() => cy.request({ url: `/api/tasks/${rawTask}/`, failOnStatusCode: false }))
        .its('status').should('eq', 404);
      cy.window({ timeout: 30000 }).its('DM.SDK.blindWorkspace').should('be.a', 'function');
      cy.window().then((win: any) => {
        const current = win.DM.SDK;
        const workspace = current.blindWorkspace;
        current.destroy();
        const root = win.document.createElement('div');
        root.id = 'synthetic-blind-root';
        win.document.body.appendChild(root);
        win.syntheticBlindManager = new win.DataManager({
          root, projectId: scope.id, apiGateway: `${win.APP_SETTINGS.hostname}/api/dm`, apiVersion: 2,
          project: { id: scope.id }, settings: { blindProtocol: true }, blindWorkspace: workspace,
          interfaces: { labelingHeader: true, backButton: true }, labelStudio: {}, polling: false, locale,
        });
      });
      cy.get('#synthetic-blind-root', { timeout: 30000 }).contains(text).parent()
        .contains('button', locale === 'zh-CN' ? '打开标注' : 'Open annotation').click();
      cy.window({ timeout: 30000 }).should((win: any) => {
        const task = win.syntheticBlindManager.store.taskStore.selected;
        expect(task?.id).to.be.greaterThan(2 ** 51 - 1);
        expect(task?.id).not.to.eq(rawTask);
        expect(task?.data?.text).to.eq(text);
        taskHandle = task.id;
      });
      cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]', { timeout: 30000 })
        .should('not.be.disabled').check({ force: true });
      cy.intercept('POST', '**/api/tasks/blind/*/annotations*').as('blindSubmit');
      cy.get('[data-testid="bottombar-submit-button"]', { timeout: 30000 }).should('not.be.disabled').click();
      cy.wait('@blindSubmit').then(({ request, response }) => {
        expect(response?.statusCode).to.eq(201);
        expect(Number(request.url.match(/blind\/(\d+)\//)?.[1])).to.eq(taskHandle);
        expect(request.body.assignment_id).to.be.greaterThan(2 ** 51 - 1);
        expect(response?.body.id).to.be.greaterThan(2 ** 51 - 1);
        annotationHandle = response!.body.id;
        nextVersion = response!.body.assignment_version;
      });
      cy.get('[data-testid="bottombar-update-button"]', { timeout: 30000 }).should('exist');
      cy.get('#label-studio-dm input[type="checkbox"][name="Negative"]', { timeout: 30000 })
        .should('not.be.disabled').check({ force: true });
      cy.intercept('PATCH', '**/api/tasks/blind/*/annotations/*').as('blindUpdate');
      cy.get('[data-testid="bottombar-update-button"]').should('not.be.disabled').click();
      cy.wait('@blindUpdate').then(({ request, response }) => {
        expect(response?.statusCode).to.eq(200);
        expect(request.body.assignment_version).to.eq(nextVersion);
        expect(response?.body.id).to.eq(annotationHandle);
        expect(response?.body.assignment_version).to.be.greaterThan(nextVersion);
      });
      cy.window().then((win: any) => win.syntheticBlindManager.destroy());
    });
  }
});
