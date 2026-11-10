/// <reference types="cypress" />
describe('frozen bbox batch and arbitration', () => {
  let fixture: any;
  before(() => cy.readFile('.bbox-e2e.json', { log: false }).then((value) => { fixture = value; }));
  for (const locale of ['en-US', 'zh-CN'] as const) {
    it(`freezes an image and adopts an independent submission in ${locale}`, () => {
      const zh = locale === 'zh-CN', scope = fixture.projects[locale];
      const login = (role: string) => {
        cy.loginAs(fixture.users[role].email, fixture.password, '/user/account/personal-info');
        cy.setUserLocale(locale); cy.visit(`/projects/${scope.id}/data`);
        cy.get('html').should('have.attr', 'lang', locale);
      };
      const button = (en: string, cn: string) => cy.contains('button', zh ? cn : en);
      let arbitration: number;
      login('manager');
      cy.get('[data-testid="open-bbox-batches"]', { timeout: 30000 }).click();
      button('New comparison batch', '新建比较批次').click();
      cy.get('[data-testid="bbox-batches"]').find('input').first().type('BBox ' + locale);
      button('Preview comparison', '预览比较').click();
      button('Create frozen batch', '创建冻结批次').click();
      cy.get(`[data-task="${scope.task}"]`).click();
      cy.get('[data-testid="bbox-batches"] svg rect', { timeout: 30000 }).should('have.length', 2);
      cy.get('[data-testid="dispatch-bbox-arbitration"]').click();
      cy.get('[data-testid="bbox-arbitrations"]').find('select').select(String(fixture.users.reviewer.id));
      cy.intercept({ method: 'POST', pathname: new RegExp(`/api/projects/${scope.id}/bbox-arbitrations/?$`) }).as('dispatch');
      button('Confirm dispatch', '确认派发').click();
      cy.wait('@dispatch').then(({ response }) => { expect(response?.statusCode).to.eq(201); arbitration = response!.body.id; });
      cy.get('[data-testid="arbitration-saved"]').should('be.visible');
      login('reviewer');
      cy.get('[data-testid="open-bbox-arbitrations"]', { timeout: 30000 }).click();
      cy.then(() => cy.get(`[data-arbitration="${arbitration}"]`).click());
      cy.get('[data-testid="bbox-arbitrations"] svg rect', { timeout: 30000 }).should('have.length', 2);
      cy.get('[data-testid="bbox-arbitrations"] input[type="radio"]').first().check();
      cy.get('[data-testid="bbox-arbitrations"] textarea').type('Synthetic decision');
      button('Adopt this frozen revision', '采纳此冻结修订').click();
      cy.get('[data-testid="arbitration-decision"]').should('contain.text', 'Synthetic decision');
      cy.then(() => cy.request(`/api/projects/${scope.id}/bbox-arbitrations/${arbitration}/`).then((response) => {
        expect(response.body.status).to.eq('resolved');
        expect(response.body.decision.winner_submission_id).to.eq(scope.sources.a);
        expect(response.body).not.to.have.property('origin_manifest_hash');
      }));
      cy.screenshot(`bbox-arbitration-${locale}`, { capture: 'fullPage' });
      cy.task('bboxRevokeReviewer', locale);
      button('Refresh read-only view', '刷新只读视图').click();
      cy.get('[data-testid="bbox-arbitrations"] [role="alert"]').should('be.visible');
      cy.get('[data-testid="bbox-arbitrations"] svg').should('not.exist');
      login('a');
      cy.get('[data-testid="open-bbox-arbitrations"]').should('not.exist');
      cy.then(() => cy.request({ url: `/api/projects/${scope.id}/bbox-arbitrations/${arbitration}/`, failOnStatusCode: false }))
        .its('status').should('be.oneOf', [403, 404]);
      cy.visit(`/projects/${scope.id}/data?task=${scope.task}&assignment=${scope.assignments.a}`);
      cy.get('#label-studio-dm img[alt="image"]', { timeout: 30000 }).should(($image) => {
        expect(($image[0] as HTMLImageElement).naturalWidth).to.eq(100);
        expect(($image[0] as HTMLImageElement).src).to.contain('/bbox-media/');
      });
    });
  }
});
