/// <reference types="cypress" />

// The fixture uses the real internal sampler against a synthetic database.
// The production dispatch API and participant-mode switch remain disabled.
describe('synthetic blind and ordinary work', () => {
  let fixture: any;
  before(() => cy.readFile('.classification82-blind-e2e.json', { log: false }).then((value) => { fixture = value; }));

  for (const locale of ['en-US', 'zh-CN'] as const) {
    it(`submits both work types through the same opaque editor in ${locale}`, () => {
      const scope = fixture.projects[locale];
      const openText = locale === 'zh-CN' ? '打开标注' : 'Open annotation';
      const qualityText = 'Synthetic 分类 split';
      const ordinaryText = `Synthetic ordinary item for ${locale} yyyyyyyyyyy`;
      let qualityHandle: number;
      let ordinaryHandle: number;
      cy.loginAs(scope.email, fixture.password, '/user/account/personal-info');
      cy.setUserLocale(locale);
      cy.visit(`/projects/${scope.id}/data`);
      cy.request(`/api/projects/${scope.id}/blind-work/`).then(({ body }) => {
        expect(body.results).to.have.length(2);
        const quality = body.results.find((row: any) => row.data.text === qualityText);
        const ordinary = body.results.find((row: any) => row.data.text === ordinaryText);
        expect(quality).to.exist;
        expect(ordinary).to.exist;
        expect(Object.keys(quality).sort()).to.deep.equal(Object.keys(ordinary).sort());
        expect(quality.id).to.be.greaterThan(2 ** 51 - 1);
        expect(ordinary.id).to.be.greaterThan(2 ** 51 - 1);
        expect([quality.id, ordinary.id]).not.to.include(scope.quality_task);
        expect([quality.id, ordinary.id]).not.to.include(scope.ordinary_task);
        qualityHandle = quality.id;
        ordinaryHandle = ordinary.id;
      });
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
      const submit = (text: string, choice: string, expected: () => number) => {
        let existing = false;
        cy.then(() => cy.request(`/api/tasks/blind/${expected()}/`)).then(({ body }) => {
          existing = body.annotations.length > 0;
          if (existing) expect(JSON.stringify(body.annotations[0].result)).to.contain(choice);
        });
        cy.get('#synthetic-blind-root', { timeout: 30000 }).contains(text).parent()
          .contains('button', openText).click();
        cy.window({ timeout: 30000 }).should((win: any) => {
          expect(win.syntheticBlindManager.store.taskStore.selected?.id).to.eq(expected());
        });
        cy.then(() => {
          if (existing) {
            cy.get(`#label-studio-dm input[type="checkbox"][name="${choice}"]`, { timeout: 30000 }).should('be.checked');
          } else {
            cy.intercept({ method: 'GET', pathname: new RegExp(`/api/tasks/blind/${expected()}/?$`) }, (req) => {
              req.on('response', (response) => response.setDelay(1500));
            }).as('blindRefresh');
            cy.get(`#label-studio-dm input[type="checkbox"][name="${choice}"]`, { timeout: 30000 })
              .should('not.be.disabled').check({ force: true });
            cy.intercept('POST', '**/api/tasks/blind/*/annotations*').as('blindSubmit');
            cy.get('[data-testid="bottombar-submit-button"]', { timeout: 30000 }).should('not.be.disabled').click();
            cy.wait('@blindSubmit').then(({ request, response }) => {
              expect(response?.statusCode).to.eq(201);
              expect(Number(request.url.match(/blind\/(\d+)\//)?.[1])).to.eq(expected());
              expect(request.body.assignment_id).to.be.greaterThan(2 ** 51 - 1);
              expect(response?.body.id).to.be.greaterThan(2 ** 51 - 1);
            });
          }
        });
        // Click as soon as the response arrives, while the editor refresh may
        // still be in flight. Navigation must wait for that refresh to finish.
        cy.get('#synthetic-blind-root').contains('button', locale === 'zh-CN' ? '返回' : 'Back').click();
        cy.then(() => { if (!existing) cy.wait('@blindRefresh'); });
      };
      submit(qualityText, 'Positive', () => qualityHandle);
      submit(ordinaryText, 'Negative', () => ordinaryHandle);
      // A later metadata read invalidates the unseen-reference proof. An editor
      // that was already open must not survive a denied subsequent write.
      cy.get('#synthetic-blind-root').contains(qualityText).parent().contains('button', openText).click();
      cy.window({ timeout: 30000 }).should((win: any) => {
        expect(win.syntheticBlindManager.store.taskStore.selected?.id).to.eq(qualityHandle);
      });
      cy.request('/api/label_links/').its('status').should('eq', 200);
      cy.then(() => cy.request({ url: `/api/tasks/blind/${qualityHandle}/`, failOnStatusCode: false }))
        .its('status').should('eq', 404);
      cy.get('#label-studio-dm input[type="checkbox"][name="Negative"]', { timeout: 30000 })
        .should('not.be.disabled').check({ force: true });
      cy.intercept('PATCH', '**/api/tasks/blind/*/annotations/*').as('revokedWrite');
      cy.get('[data-testid="bottombar-update-button"]').should('not.be.disabled').click();
      cy.wait('@revokedWrite').its('response.statusCode').should('eq', 404);
      cy.window({ timeout: 30000 }).should((win: any) => {
        expect(Boolean(win.syntheticBlindManager.store.taskStore.selected)).to.eq(false);
        expect(win.syntheticBlindManager.mode).to.eq('explorer');
      });
      cy.get('#synthetic-blind-root').should('not.contain.text', qualityText);
      cy.window().then((win: any) => win.syntheticBlindManager.destroy());
    });
  }
});
