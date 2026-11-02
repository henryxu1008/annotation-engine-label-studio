/// <reference types="cypress" />
const nativeEnter = () => cy.then(async () => {
  for (const type of ['keyDown', 'keyUp']) await Cypress.automation('remote:debugger:protocol', {
    command: 'Input.dispatchKeyEvent', params: { type, key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
      ...(type === 'keyDown' ? { text: '\r', unmodifiedText: '\r' } : {}) },
  });
});
describe('independent supervisory_only second review V1', () => {
  let fixture: any;
  before(() => cy.readFile('.second-review-e2e.json', { log: false }).then((v) => { fixture = v; }));
  for (const locale of ['en-US', 'zh-CN'] as const) {
    it(`dispatches, recovers, records opinions and preserves release boundaries in ${locale}`, () => {
      cy.viewport(1280, 900);
      const zh = locale === 'zh-CN', project = fixture.projects[locale];
      const api = `/api/projects/${project.id}/second-reviews/`;
      const source = project.sources[0], rejected = project.sources[1], cancelled = project.sources[2], historical = project.sources[3];
      let firstTask: string, secondTask: string, originalBody: any, originalKey: any, posts = 0, trusted = false;
      const visit = (role: string) => {
        cy.visit('/user/account/personal-info');
        cy.loginAs(fixture.users[role].email, fixture.password, '/user/account/personal-info');
        cy.setUserLocale(locale); cy.visit(`/projects/${project.id}/data`);
        cy.get('html').should('have.attr', 'lang', locale);
      };
      const batch = () => {
        cy.get('[data-testid="open-review-batches"]', { timeout: 30000 }).click();
        cy.contains('button', `Supervision sample ${locale}`).click();
        cy.get('[data-testid="review-filter-selected-reviewed"]').click();
      };
      const choose = (id: number) => {
        cy.get(`[data-second-source="${id}"]`).click();
        cy.get('[data-testid="second-assignee"]').select(String(fixture.users.supervisor.id));
      };
      visit('manager'); batch(); choose(source.id);
      cy.intercept({ method: 'POST', pathname: new RegExp(`^${api.replace(/\/$/, '')}/?$`) }, (req) => {
        posts++;
        if (posts === 1) {
          originalBody = req.body; originalKey = req.headers['idempotency-key'];
          req.continue((res) => { expect(res.statusCode).to.eq(201); firstTask = res.body.receipt.task_id;
            res.send({ statusCode: 503, body: { detail: 'Synthetic lost acknowledgement' } }); });
        } else if (posts === 2) {
          expect(req.body).to.deep.eq(originalBody); expect(req.headers['idempotency-key']).to.eq(originalKey);
          req.continue((res) => { expect(res.statusCode).to.eq(200); expect(res.body.receipt.task_id).to.eq(firstTask); });
        }
      }).as('dispatch');
      cy.get('[data-testid="second-dispatch"]').click(); cy.wait('@dispatch').its('response.statusCode').should('eq', 503);
      cy.get('[data-testid="second-recover"]').should('be.focused').then(($button) => {
        $button[0].addEventListener('click', (event) => { trusted = event.isTrusted; });
      });
      nativeEnter(); cy.wait('@dispatch').its('response.statusCode').should('eq', 200);
      cy.then(() => expect(trusted, 'trusted native Enter recovery').to.eq(true));
      cy.get('[data-testid="review-result-snapshot"]').should('contain.text', 'Frozen 静态文本');
      cy.contains('button', zh ? '返回审核批次' : 'Return to review batches').click();
      cy.get(`[data-second-source="${source.id}"]`).should('be.focused');
      choose(rejected.id); cy.get('[data-testid="second-dispatch"]').click();
      cy.wait('@dispatch').then(({ response }) => { expect(response?.statusCode).to.eq(201); secondTask = response?.body.receipt.task_id; });
      cy.get('[data-testid="review-result-snapshot"]').scrollIntoView().should('be.visible');
      cy.contains('button', zh ? '返回审核批次' : 'Return to review batches').click();
      choose(cancelled.id); cy.get('[data-testid="second-dispatch"]').click(); cy.wait('@dispatch');
      cy.get('[data-testid="second-reason"]').type('Cancel 错派'); cy.get('[data-testid="second-cancel"]').click();
      cy.contains(zh ? '已取消，无监督意见' : 'Cancelled — no supervisory opinion').should('exist');
      cy.screenshot(`second-review-manager-${locale}`);
      visit('supervisor');
      cy.get('[data-testid="open-second-reviews"]', { timeout: 30000 }).click();
      cy.then(() => cy.get(`[data-second-task="${firstTask}"]`).click());
      cy.contains(zh ? '初审结论' : 'Initial review').should('be.visible');
      cy.get('[data-testid="second-reason"]').type('Concern 独立异议'); cy.get('[data-testid="second-concern"]').click();
      cy.contains(zh ? '复核有异议' : 'Concern recorded').scrollIntoView().should('be.visible');
      cy.contains('button', zh ? '返回受派队列' : 'Return to assigned queue').click();
      cy.then(() => cy.get(`[data-second-task="${secondTask}"]`).click());
      cy.get('[data-testid="second-confirm"]').click(); cy.contains(zh ? '复核无异议' : 'No concern recorded').scrollIntoView().should('be.visible');
      cy.contains('button', zh ? '返回受派队列' : 'Return to assigned queue').click();
      cy.get(`[data-second-task="${historical.second_task_id}"]`).click();
      cy.contains(zh ? '历史修订：' : 'Historical revision:').should('be.visible');
      cy.viewport(390, 844);
      cy.get('[data-testid="second-review-workspace"]').should(($root) => { expect($root[0].scrollWidth).to.be.at.most($root[0].clientWidth + 2); });
      cy.get('[data-testid="second-confirm"]').scrollIntoView().focus(); nativeEnter();
      cy.contains(zh ? '复核无异议' : 'No concern recorded').scrollIntoView().should('be.visible');
      cy.screenshot(`second-review-supervisor-${locale}`); cy.viewport(1280, 900);
      visit('manager');
      cy.request(`/api/submissions/${source.id}/release/`).its('status').should('eq', 200);
      cy.request({ url: `/api/submissions/${rejected.id}/release/`, failOnStatusCode: false }).its('status').should('eq', 400);
      cy.request(api).then(({ body }) => { expect(body.count).to.eq(4); expect(body.results.filter((r: any) => r.status === 'completed')).to.have.length(3); expect(body.results.filter((r: any) => r.status === 'cancelled')).to.have.length(1); });
      visit('initial'); cy.request(api).its('body.count').should('eq', 0);
      visit('a'); cy.get('[data-testid="open-second-reviews"]').should('not.exist');
      cy.request({ url: api, failOnStatusCode: false }).its('status').should('be.oneOf', [403, 404]);
    });
  }
});
