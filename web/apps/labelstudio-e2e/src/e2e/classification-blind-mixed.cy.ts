/// <reference types="cypress" />

// The fixture uses the real internal sampler against a synthetic database.
// The production dispatch API remains disabled; the actual project page selects
// the participant protocol using the server's session-bound work mode.
describe('synthetic blind and ordinary work', () => {
  let fixture: any;
  before(() => cy.readFile('.classification82-blind-e2e.json', { log: false }).then((value) => { fixture = value; }));

  for (const locale of ['en-US', 'zh-CN'] as const) {
    it(`fails closed and retries the real workspace in ${locale}`, () => {
      const scope = fixture.projects[locale];
      let denyMode = true;
      cy.loginAs(scope.email, fixture.password, '/user/account/personal-info');
      cy.setUserLocale(locale);
      cy.intercept('GET', `**/api/projects/${scope.id}/work-mode/`, (req) => {
        if (denyMode) req.reply({ statusCode: 404, body: {} });
        else req.continue();
      }).as('workMode');
      cy.intercept('GET', '**/api/ml?*').as('legacyModels');
      cy.intercept('GET', '**/api/dm/project/**').as('legacyDataManager');
      cy.visit(`/projects/${scope.id}/data`);
      cy.wait('@workMode').its('response.statusCode').should('eq', 404);
      cy.contains(locale === 'zh-CN' ? '无法加载任务工作区' : 'Could not load the task workspace').should('be.visible');
      cy.get('@legacyModels.all').should('have.length', 0);
      cy.get('@legacyDataManager.all').should('have.length', 0);
      cy.then(() => { denyMode = false; });
      cy.contains('button', locale === 'zh-CN' ? '重试' : 'Retry').click();
      cy.wait('@workMode').its('response.statusCode').should('eq', 200);
      cy.window({ timeout: 30000 }).its('DM.SDK.settings.blindProtocol').should('eq', true);
    });

    it(`submits both work types through the same opaque editor in ${locale}`, () => {
      const scope = fixture.projects[locale];
      const openText = locale === 'zh-CN' ? '打开标注' : 'Open annotation';
      const qualityText = 'Synthetic 分类 split';
      const ordinaryText = `Synthetic ordinary item for ${locale} yyyyyyyyyyy`;
      let qualityHandle: number;
      let ordinaryHandle: number;
      cy.loginAs(scope.email, fixture.password, '/user/account/personal-info');
      cy.setUserLocale(locale);
      cy.intercept('GET', '**/api/ml?*').as('legacyModels');
      cy.intercept('GET', '**/api/dm/project/**').as('legacyDataManager');
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
      cy.window({ timeout: 30000 }).its('DM.SDK.settings.blindProtocol').should('eq', true);
      cy.get('@legacyModels.all').should('have.length', 0);
      cy.get('@legacyDataManager.all').should('have.length', 0);
      const submit = (text: string, choice: string, expected: () => number) => {
        let existing = false;
        cy.then(() => cy.request(`/api/tasks/blind/${expected()}/`)).then(({ body }) => {
          existing = body.annotations.length > 0;
          if (existing) expect(JSON.stringify(body.annotations[0].result)).to.contain(choice);
        });
        cy.get('.datamanager', { timeout: 30000 }).contains(text).parent()
          .contains('button', openText).click();
        cy.window({ timeout: 30000 }).should((win: any) => {
          expect(win.dataManager.store.taskStore.selected?.id).to.eq(expected());
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
        cy.get('.datamanager').contains('button', locale === 'zh-CN' ? '返回' : 'Back').click();
        cy.then(() => { if (!existing) cy.wait('@blindRefresh'); });
      };
      submit(qualityText, 'Positive', () => qualityHandle);
      submit(ordinaryText, 'Negative', () => ordinaryHandle);
      // A later metadata read invalidates the unseen-reference proof. An editor
      // that was already open must not survive a denied subsequent write.
      cy.get('.datamanager').contains(qualityText).parent().contains('button', openText).click();
      cy.window({ timeout: 30000 }).should((win: any) => {
        expect(win.dataManager.store.taskStore.selected?.id).to.eq(qualityHandle);
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
        expect(Boolean(win.dataManager.store.taskStore.selected)).to.eq(false);
        expect(win.dataManager.mode).to.eq('explorer');
      });
      cy.get('.datamanager').should('not.contain.text', qualityText);
    });
  }
});

// Reassignment is prepared by the guarded synthetic fixture, not the
// disabled public manager command. All participant requests go to the real app.
describe('participant handoff after synthetic mixed reassignment', () => {
  let fixture: any;
  let reviewerEmail: string;
  before(() => {
    cy.readFile('.classification82-blind-e2e.json', { log: false }).then((value) => { fixture = value; });
    cy.readFile('.classification81-e2e.json', { log: false }).then((value) => {
      reviewerEmail = value.users.reviewer.email;
    });
  });

  for (const locale of ['en-US', 'zh-CN'] as const) {
    for (const kind of ['ordinary', 'quality'] as const) {
      it(`submits the successor and verifies review routing for ${kind} work in ${locale}`, () => {
        const scope = fixture.projects[locale];
        const item = scope.reassignments[kind];
        const listUrl = `/api/projects/${scope.id}/blind-work/`;
        const oldUrl = `/api/tasks/blind/${item.previous_handle}/`;
        let newHandle: number;

        cy.loginAs(item.original_email, fixture.password, '/user/account/personal-info');
        cy.request(listUrl).its('body.results').should('be.empty');
        cy.request({ url: oldUrl, failOnStatusCode: false }).its('status').should('eq', 404);

        cy.loginAs(item.replacement_email, fixture.password, '/user/account/personal-info');
        cy.setUserLocale(locale);
        cy.request({ url: oldUrl, failOnStatusCode: false }).its('status').should('eq', 404);
        cy.request(listUrl).then(({ body }) => {
          expect(body.results).to.have.length(1);
          const work = body.results[0];
          expect(work.data.text).to.eq(item.text);
          expect(work.id).to.be.greaterThan(2 ** 51 - 1);
          expect(work.id).not.to.eq(item.previous_handle);
          expect(work.id).not.to.eq(item.task_id);
          newHandle = work.id;
        });
        cy.then(() => cy.request(`/api/tasks/blind/${newHandle}/`)).its('body.annotations').should('be.empty');

        cy.visit(`/projects/${scope.id}/data`);
        cy.window({ timeout: 30000 }).its('DM.SDK.settings.blindProtocol').should('eq', true);
        cy.get('.datamanager', { timeout: 30000 }).contains(item.text).parent()
          .contains('button', locale === 'zh-CN' ? '打开标注' : 'Open annotation').click();
        cy.window({ timeout: 30000 }).should((win: any) => {
          expect(win.dataManager.store.taskStore.selected?.id).to.eq(newHandle);
        });

        cy.get('#label-studio-dm input[type="checkbox"][name="Positive"]', { timeout: 30000 })
          .should('not.be.disabled').check({ force: true });
        cy.intercept('POST', '**/api/tasks/blind/*/annotations*').as('successorSubmit');
        cy.get('[data-testid="bottombar-submit-button"]').should('not.be.disabled').click();
        cy.wait('@successorSubmit').then(({ request, response }) => {
          expect(response?.statusCode).to.eq(201);
          expect(Number(request.url.match(/blind\/(\d+)\//)?.[1])).to.eq(newHandle);
          expect(request.body.submit_for_review).to.eq(true);
        });
        cy.then(() => cy.request(`/api/tasks/blind/${newHandle}/`)).then(({ body }) => {
          expect(body.annotations).to.have.length(1);
          expect(JSON.stringify(body.annotations[0].result)).to.contain('Positive');
        });
        // The synthetic manager can confirm the stored revision, but only
        // the separate reviewer session can inspect the existing review queue.
        const managerRequest = {
          headers: { Authorization: `Token ${fixture.manager_token}` },
          failOnStatusCode: false,
          log: false,
        };
        const submissionsUrl = `/api/submissions/?project=${scope.id}`;
        cy.request<{
          id: number;
          status: string;
          revision: number;
          result_snapshot: { task: { id: number } };
        }[]>({ ...managerRequest, url: submissionsUrl }).then(({ body, status }) => {
          expect(status).to.eq(200);
          const stored = body.filter((row) => row.result_snapshot?.task?.id === item.task_id);
          expect(stored.length).to.eq(1);
          expect(stored[0].revision).to.eq(1);
          expect(stored[0].status).to.eq('pending');
          cy.loginAs(reviewerEmail, fixture.password, '/user/account/personal-info');
          return cy.request<{ next: string | null; results: { id: number }[] }>({
            failOnStatusCode: false, log: false, url: `${submissionsUrl}&reviewable=true`,
          }).then(({ body: queue, status }) => {
            expect(status).to.eq(200);
            expect(queue.next).to.eq(null);
            expect(queue.results.some((row) => row.id === stored[0].id)).to.eq(kind === 'ordinary');
          });
        });
      });
    }
  }
});
